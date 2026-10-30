"""Repository ingestion: register and sync repositories from GitHub, Azure DevOps, any git URL, or a local path.

Security notes
- Tokens are passed to git through environment variables (http.extraheader), never on the command line,
  never written to disk, never stored in the registry, and are redacted from error messages.
- Embedded credentials in URLs are rejected (use the token field).
- Local paths are only accepted when ``allow_local`` is true (single-user / development mode).
- ``allowed_hosts`` (optional) restricts which remote hosts may be cloned (SSRF control for SaaS).
- Git is only ever used to read; TIA never pushes.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from . import gitutil


class IngestError(Exception):
    pass


@dataclass
class Repo:
    name: str
    url: str                  # remote URL, or the local path for kind == "local"
    provider: str             # github | ado | git | local
    kind: str                 # remote | local | upload (TIA-owned bare repository created from an uploaded zip/folder)
    path: str                 # directory TIA reads from
    default_branch: str = "HEAD"
    last_synced: Optional[float] = None
    last_commit: Optional[str] = None
    last_error: Optional[str] = None
    branches: list = field(default_factory=list)
    # scan state (FR-005): a scan is a fetch plus an extraction of the default-branch head
    scan_status: str = "never"                  # never | ok | partial (some files unparseable) | failed
    scan_error: Optional[str] = None
    analysed_commit: Optional[str] = None       # revision of the last extraction that completed
    last_successful_scan: Optional[float] = None
    scan_files_failed: int = 0

    def public(self) -> dict:
        d = asdict(self)
        return d


# ------------------------------------------------------------------------------------------------
def detect_provider(url: str) -> str:
    u = urlsplit(url if "://" in url else "ssh://" + url.replace(":", "/", 1))
    host = (u.hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com") or "github" in host.split(".")[0]:
        return "github"
    if host in ("dev.azure.com", "ssh.dev.azure.com") or host.endswith(".visualstudio.com") or "/_git/" in u.path:
        return "ado"
    return "git"


def derive_name(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url
    seg = [s for s in re.split(r"[/\\]", path) if s]
    name = seg[-1] if seg else "repo"
    name = re.sub(r"\.git$", "", name)
    return re.sub(r"[^A-Za-z0-9._-]", "-", name) or "repo"


def _auth_env(provider: str, token: Optional[str]) -> dict:
    if not token:
        return {}
    user = {"github": "x-access-token", "ado": "", "git": "oauth2"}.get(provider, "oauth2")
    b64 = base64.b64encode(f"{user}:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {b64}"}


def _redact(text: str, token: Optional[str]) -> str:
    if not token:
        return text
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    for secret in (token, b64, base64.b64encode(f":{token}".encode()).decode(),
                   base64.b64encode(f"oauth2:{token}".encode()).decode()):
        text = text.replace(secret, "***")
    return text


def _git(args: list, cwd: Optional[str] = None, provider: str = "git", token: Optional[str] = None,
         timeout: int = 900) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", **_auth_env(provider, token)}
    try:
        r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise IngestError(f"git {args[0]} timed out after {timeout}s")
    except FileNotFoundError:
        raise IngestError("git is not installed on the server")
    if r.returncode != 0:
        raise IngestError(_friendly(_redact(r.stderr.decode("utf-8", "replace").strip(), token), bool(token), args[0]))
    return r.stdout.decode("utf-8", "replace")


def _friendly(stderr: str, had_token: bool, op: str) -> str:
    low = stderr.lower()
    lines = [l for l in stderr.splitlines() if l and not l.startswith("Cloning into")]
    if "could not read username" in low or "authentication failed" in low or "http 401" in low or "error: 401" in low:
        return ("Authentication failed: the token was rejected or lacks access to this repository." if had_token else
                "Authentication required, or the repository does not exist: provide an access token (PAT) for private repositories.")
    if "repository not found" in low or "not found" in low and "fatal" in low or "returned error: 404" in low:
        return "Repository not found, or the token has no access to it."
    if "could not resolve host" in low or "unable to access" in low and "connect" in low:
        return "Cannot reach the git host from the server (network, DNS, or proxy)."
    if "ssl" in low and ("certificate" in low or "verify" in low):
        return "TLS certificate verification failed for the git host (corporate CA not trusted by the server?)."
    return "\n".join(lines) or f"git {op} failed"


# ------------------------------------------------------------------------------------------------
class RepoService:
    def __init__(self, store, workspace: str, allow_local: bool = False, allowed_hosts: Optional[list] = None):
        self.store = store
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allow_local = allow_local
        self.allowed_hosts = [h.lower() for h in (allowed_hosts or [])]

    # ---- registration ---------------------------------------------------------------------------
    def register(self, url: Optional[str] = None, path: Optional[str] = None, name: Optional[str] = None,
                 token: Optional[str] = None, branch: Optional[str] = None) -> Repo:
        if bool(url) == bool(path):
            raise IngestError("provide exactly one of 'url' or 'path'")
        repo = self._register_local(path, name) if path else self._register_remote(url, name, token, branch)
        self.store.put_repo(repo)
        self.store.audit("repository_registered", name=repo.name, provider=repo.provider, kind=repo.kind,
                         url=repo.url, commit=repo.last_commit)
        return repo

    def commit_upload(self, name: str, stage: Path, label: str, files: int, size: int, skipped: int) -> tuple:
        """Record a staged upload as the next revision of the repository `name` (created on first upload).
        Returns (Repo, commit, created)."""
        from .upload import commit_tree          # local import: upload builds on this module
        rname = _clean(name)
        existing = self.store.get_repo(rname)
        if existing and existing.kind != "upload":
            raise IngestError(f"a repository named '{rname}' is already registered from {existing.provider}; "
                              "choose another name for the upload")
        dest = self.workspace / rname
        if not existing and dest.exists():
            _rmtree(dest)
        sha, created = commit_tree(dest, stage, label)
        repo = existing or Repo(name=rname, url=f"upload://{rname}", provider="upload", kind="upload", path=str(dest))
        repo = self._refresh_meta(repo, "main")
        repo.last_error = None
        self.store.put_repo(repo)
        self.store.audit("repository_uploaded", name=rname, commit=sha, files=files, bytes=size, skipped=skipped,
                         unchanged=not created, first=existing is None)
        return repo, sha, created

    def revisions(self, name: str, limit: int = 50) -> list:
        """Upload history (newest first): the revisions between which a change can be analysed."""
        repo = self.get(name)
        out = gitutil._git(repo.path, "log", f"--max-count={int(limit)}", "--format=%H%x09%ct%x09%s", repo.default_branch)
        rows = []
        for line in out.splitlines():
            sha, ts, subject = line.split("\t", 2)
            rows.append({"commit": sha, "time": int(ts), "label": subject})
        return rows

    def _register_local(self, path: str, name: Optional[str]) -> Repo:
        if not self.allow_local:
            raise IngestError("local paths are disabled on this server (set TIA_ALLOW_LOCAL_PATHS=true)")
        p = Path(path).expanduser()
        if not p.is_dir():
            raise IngestError(f"local path not found: {path}")
        try:
            gitutil._git(str(p), "rev-parse", "--git-dir")
        except gitutil.GitError:
            raise IngestError(f"not a git repository: {path}")
        repo = Repo(name=_clean(name or derive_name(str(p.resolve()))), url=str(p.resolve()), provider="local",
                    kind="local", path=str(p.resolve()))
        return self._refresh_meta(repo)

    def _register_remote(self, url: str, name: Optional[str], token: Optional[str], branch: Optional[str]) -> Repo:
        u = urlsplit(url)
        if u.scheme not in ("https", "http", "ssh", "file"):
            raise IngestError("only https, http, ssh, file URLs or local paths are supported")
        if u.scheme == "file" and not self.allow_local:
            raise IngestError("file:// URLs are disabled on this server")
        if u.username or u.password:
            if u.scheme != "ssh":
                raise IngestError("do not embed credentials in the URL; use the token field")
        host = (u.hostname or "").lower()
        if self.allowed_hosts and u.scheme != "file" and host not in self.allowed_hosts:
            raise IngestError(f"host '{host}' is not in the server's allowed git hosts")
        provider = detect_provider(url)
        rname = _clean(name or derive_name(url))
        if self.store.get_repo(rname):
            raise IngestError(f"a repository named '{rname}' is already registered (use sync, or pick another name)")
        dest = self.workspace / rname
        if dest.exists():
            _rmtree(dest)
        _git(["clone", "--bare", "--no-tags", url, str(dest)], provider=provider, token=token)
        repo = Repo(name=rname, url=url, provider=provider, kind="remote", path=str(dest))
        if branch:
            repo.default_branch = branch
        try:
            self._fetch_heads(repo, token)
        except IngestError:
            pass
        return self._refresh_meta(repo, branch)
    # ---- sync / refs ----------------------------------------------------------------------------------
    def _fetch_heads(self, repo: Repo, token: Optional[str]):
        _git(["fetch", "--prune", "--no-tags", "origin", "+refs/heads/*:refs/heads/*"], cwd=repo.path,
             provider=repo.provider, token=token)

    def _follow_remote_default(self, repo: Repo, token: Optional[str]):
        """Point the bare clone's HEAD at the remote's current default branch (it can change after registration)."""
        out = _git(["ls-remote", "--symref", "origin", "HEAD"], cwd=repo.path, provider=repo.provider, token=token)
        for line in out.splitlines():
            if line.startswith("ref: refs/heads/") and line.rstrip().endswith("\tHEAD"):
                branch = line[len("ref: refs/heads/"):].split("\t")[0]
                gitutil._git(repo.path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
                return

    def sync(self, name: str, token: Optional[str] = None) -> Repo:
        repo = self.get(name)
        try:
            if repo.kind == "remote":
                self._fetch_heads(repo, token)
                try:
                    self._follow_remote_default(repo, token)
                except (IngestError, gitutil.GitError):
                    pass                # keep the last known default rather than fail the sync
            repo = self._refresh_meta(repo, None)
            repo.last_error = None
        except IngestError as e:
            repo.last_error = str(e)
            repo.scan_status, repo.scan_error = "failed", str(e)
            self.store.put_repo(repo)
            self.store.audit("repository_sync_failed", name=repo.name, error=str(e)[:300])
            raise
        self.store.put_repo(repo)
        self.store.audit("repository_synced", name=repo.name, commit=repo.last_commit)
        return repo

    def record_scan(self, name: str, commit: Optional[str] = None, files_failed: int = 0, error: Optional[str] = None):
        """Record the outcome of an extraction (called after the fetch): ok, partial or failed.
        A failed fetch is not cleared by extracting the old revision again; only a successful sync clears it."""
        repo = self.get(name)
        if error is None and repo.last_error:
            error = repo.last_error
        before = (repo.scan_status, repo.scan_error, repo.analysed_commit, repo.scan_files_failed)
        if error is not None:
            repo.scan_status, repo.scan_error = "failed", error
        else:
            repo.scan_status = "partial" if files_failed else "ok"
            repo.scan_error, repo.analysed_commit, repo.scan_files_failed = None, commit, files_failed
        if (repo.scan_status, repo.scan_error, repo.analysed_commit, repo.scan_files_failed) == before:
            return
        if error is None:
            repo.last_successful_scan = time.time()
        self.store.put_repo(repo)

    def _refresh_meta(self, repo: Repo, branch: Optional[str] = None) -> Repo:
        try:
            db = branch or gitutil._git(repo.path, "symbolic-ref", "--short", "-q", "HEAD").strip() or "HEAD"
        except gitutil.GitError:
            db = branch or "HEAD"
        repo.default_branch = db
        try:
            repo.last_commit = gitutil.resolve_rev(repo.path, db)
            out = gitutil._git(repo.path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
            repo.branches = sorted(b for b in out.splitlines() if b)
        except gitutil.GitError as e:
            raise IngestError(f"repository has no readable commits on '{db}': {e}")
        repo.last_synced = time.time()
        return repo

    def fetch_pull_request(self, name: str, number: int, token: Optional[str] = None) -> str:
        """Fetch a pull request head into refs/tia/pr/<n>; returns the commit sha."""
        repo = self.get(name)
        if repo.kind != "remote":
            raise IngestError("pull-request refs are only available for remote repositories")
        last = None
        for src in (f"refs/pull/{number}/head", f"refs/pull/{number}/merge"):
            try:
                _git(["fetch", "--no-tags", "origin", f"+{src}:refs/tia/pr/{number}"], cwd=repo.path,
                     provider=repo.provider, token=token)
                sha = gitutil.resolve_rev(repo.path, f"refs/tia/pr/{number}")
                self.store.audit("pull_request_fetched", name=name, pr=number, commit=sha)
                return sha
            except IngestError as e:
                last = e
        raise IngestError(f"could not fetch pull request {number}: {last}")

    def pin_commits(self, name: str, shas) -> int:
        """Keep analysed commits reachable (refs/tia/pin/<sha>) so evidence survives force-pushes and garbage collection.
        Only TIA's own clones are touched; a local repository is read in place and never modified."""
        repo = self.get(name)
        if repo.kind == "local":
            return 0
        n = 0
        for sha in sorted(set(shas)):
            try:
                gitutil._git(repo.path, "update-ref", f"refs/tia/pin/{sha}", sha)
                n += 1
            except gitutil.GitError:
                continue
        return n

    def resolve(self, name: str, ref: Optional[str]) -> str:
        repo = self.get(name)
        for cand in ([ref] if ref else [repo.default_branch]) + ([f"refs/heads/{ref}", f"origin/{ref}"] if ref else []):
            try:
                return gitutil.resolve_rev(repo.path, cand)
            except gitutil.GitError:
                continue
        raise IngestError(f"ref '{ref}' not found in repository '{name}'")

    def merge_base(self, name: str, a: str, b: str) -> str:
        repo = self.get(name)
        try:
            return gitutil._git(repo.path, "merge-base", a, b).strip()
        except gitutil.GitError:
            return a            # unrelated histories: fall back to the given base

    # ---- registry ------------------------------------------------------------------------------------
    def get(self, name: str) -> Repo:
        r = self.store.get_repo(name)
        if not r:
            raise IngestError(f"repository '{name}' is not registered")
        return r

    def list(self) -> list:
        return self.store.list_repos()

    def remove(self, name: str):
        r = self.get(name)
        if r.kind in ("remote", "upload"):
            _rmtree(Path(r.path))       # only TIA's own storage; never a user's local repo
        self.store.delete_repo(name)
        self.store.audit("repository_removed", name=name, kind=r.kind)

    # ---- discovery (ingest a whole ADO project / GitHub org or user) -------------------------------------
    def discover(self, provider: str, org: str, project: Optional[str], token: str,
                 api_base: Optional[str] = None) -> list[dict]:
        if provider == "ado":
            base = (api_base or "https://dev.azure.com").rstrip("/")
            if not project:
                raise IngestError("Azure DevOps discovery needs 'project'")
            url = f"{base}/{quote(org)}/{quote(project)}/_apis/git/repositories?api-version=7.1"
            auth = "Basic " + base64.b64encode(f":{token}".encode()).decode()
            data = _http_json(url, auth)
            return [{"name": r["name"], "url": r["remoteUrl"], "default_branch": (r.get("defaultBranch") or "").replace("refs/heads/", "")}
                    for r in data.get("value", []) if not r.get("isDisabled")]
        if provider == "github":
            base = (api_base or "https://api.github.com").rstrip("/")
            auth = "Bearer " + token
            out, page = [], 1
            while True:
                chunk = None
                for kind in ("orgs", "users"):
                    try:
                        chunk = _http_json(f"{base}/{kind}/{quote(org)}/repos?per_page=100&page={page}", auth)
                        break
                    except IngestError as e:
                        if "404" not in str(e):
                            raise
                if chunk is None:
                    raise IngestError(f"GitHub organisation/user '{org}' not found or not accessible")
                out += [{"name": r["name"], "url": r["clone_url"], "default_branch": r.get("default_branch", "")}
                        for r in chunk if not r.get("archived") and not r.get("disabled")]
                if len(chunk) < 100:
                    return out
                page += 1
        raise IngestError("provider must be 'github' or 'ado'")


def _http_json(url: str, auth: str):
    req = urllib.request.Request(url, headers={"Authorization": auth, "Accept": "application/json", "User-Agent": "tia"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise IngestError(f"{url.split('?')[0]} returned HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise IngestError(f"cannot reach {urlsplit(url).netloc}: {e}")


def _clean(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-.")
    if not n:
        raise IngestError("invalid repository name")
    return n


def _rmtree(path: Path):
    """rmtree that also removes git's read-only pack files (needed on Windows)."""
    def _retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=_retry)
