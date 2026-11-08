"""GitLab and Bitbucket connectors (PRD FR-039, AC-036): the same capabilities as GitHub - receiving merge/pull-request events, revision-aware
analysis, one advisory comment updated in place plus a commit status, and the permission mirror for per-repository access.

GitLab (gitlab.com and self-managed, TIA_GITLAB_URL)
  Webhook  POST /api/v1/webhooks/gitlab, "Merge request events", authenticated by the secret token (X-Gitlab-Token == TIA_GITLAB_WEBHOOK_SECRET).
  Fetch    refs/merge-requests/<iid>/head.
  Publish  a merge-request note (found again by TIA's marker), and a commit status "tia/impact-analysis".
  Access   project members with at least Reporter access (members/all, so inherited group members count).
  Token    TIA_GITLAB_TOKEN or the tenant secret gitlab/token (scopes: api, read_repository).
Bitbucket (Cloud, and Data Center / Server with TIA_BITBUCKET_URL)
  Webhook  POST /api/v1/webhooks/bitbucket, pull request created/updated (Cloud pullrequest:created|updated, Data Center pr:opened|
           pr:from_ref_updated|pr:modified), authenticated by the webhook secret's HMAC-SHA256 (X-Hub-Signature, TIA_BITBUCKET_WEBHOOK_SECRET).
  Fetch    Data Center refs/pull-requests/<id>/from; Cloud has no pull-request refs, so the source branch is fetched.
  Publish  a pull-request comment (found again by TIA's marker), and a commit build status keyed "tia-impact-analysis".
  Access   repository user permissions (Cloud workspace repository permissions; Data Center repository and project permissions).
  Token    TIA_BITBUCKET_TOKEN or the tenant secret bitbucket/token (Cloud: a repository/workspace access token; Data Center: an HTTP access token).
Advisory by default: the status is success ("Advisory ...") unless the tenant's policy is enforcing (then failed / pending as the policy says).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import airgap, metrics
from .policy import check_conclusion
from .report import MARKER

STATUS_NAME = "tia/impact-analysis"
GITLAB_READ = {20: "pull", 30: "push", 40: "maintain", 50: "admin"}          # Reporter, Developer, Maintainer, Owner (Guest cannot read code)
BITBUCKET_READ = {"read": "pull", "write": "push", "admin": "admin", "REPO_READ": "pull", "REPO_WRITE": "push", "REPO_ADMIN": "admin",
                  "PROJECT_READ": "pull", "PROJECT_WRITE": "push", "PROJECT_ADMIN": "admin"}


class ScmHostError(Exception):
    def __init__(self, message: str, permanent: bool = True, code: Optional[int] = None):
        super().__init__(message)
        self.permanent, self.code = permanent, code


@dataclass(frozen=True)
class HostConfig:
    provider: str                        # gitlab | bitbucket
    base_url: str                        # web/API base: https://gitlab.com, https://bitbucket.org, or the self-hosted server
    webhook_secret: Optional[str] = None
    token: Optional[str] = None

    @property
    def cloud(self) -> bool:
        return self.base_url.rstrip("/") in ("https://gitlab.com", "https://bitbucket.org")

    @property
    def api(self) -> str:
        if self.provider == "gitlab":
            return self.base_url.rstrip("/") + "/api/v4"
        return "https://api.bitbucket.org/2.0" if self.cloud else self.base_url.rstrip("/") + "/rest"

    @classmethod
    def from_env(cls, provider: str) -> "HostConfig":
        from .credentials import env_secret
        p = provider.upper()
        default = "https://gitlab.com" if provider == "gitlab" else "https://bitbucket.org"
        return cls(provider, (os.environ.get(f"TIA_{p}_URL") or default).rstrip("/"), env_secret(f"TIA_{p}_WEBHOOK_SECRET") or None,
                   env_secret(f"TIA_{p}_TOKEN") or None)


def allowed(cfg: HostConfig, url: str) -> bool:
    """Calls carrying the token go only to the configured host (https; http only to this machine for development)."""
    u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(cfg.base_url)
    hosts = {(b.hostname or "").lower()} | ({"api.bitbucket.org"} if cfg.provider == "bitbucket" and cfg.cloud else set())
    local = (u.hostname or "") in ("localhost", "127.0.0.1", "::1")
    return (u.hostname or "").lower() in hosts and (u.scheme == "https" or (u.scheme == "http" and local))


# ---- receiving -----------------------------------------------------------------------------------------------------------------------
def verify(cfg: HostConfig, headers, raw: bytes) -> bool:
    if not cfg.webhook_secret:
        return False
    if cfg.provider == "gitlab":
        return hmac.compare_digest((headers.get("x-gitlab-token") or "").encode(), cfg.webhook_secret.encode())
    sig = headers.get("x-hub-signature") or headers.get("x-hub-signature-256") or ""
    good = "sha256=" + hmac.new(cfg.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.encode(), good.encode())


def parse_event(cfg: HostConfig, headers, payload: dict) -> Optional[dict]:
    """-> {pr, head_sha, base_ref, source_ref, remote_url, full_name, project_id, event} or None when no analysis is needed. ValueError if malformed."""
    try:
        if cfg.provider == "gitlab":
            if payload.get("object_kind") != "merge_request":
                return None
            a = payload["object_attributes"]
            if a.get("state") != "opened" or a.get("action") not in ("open", "reopen", "update") or a.get("draft") or a.get("work_in_progress"):
                return None
            if a.get("action") == "update" and not a.get("oldrev"):
                return None                                                   # title/description edits: no new commits
            proj = payload["project"]
            return {"pr": int(a["iid"]), "head_sha": (a.get("last_commit") or {}).get("id"), "base_ref": a["target_branch"],
                    "source_ref": a["source_branch"], "remote_url": proj.get("git_http_url") or proj.get("http_url") or "",
                    "full_name": proj["path_with_namespace"], "project_id": str(proj["id"]), "event": f"merge_request.{a['action']}"}
        key = headers.get("x-event-key") or ""
        if key in ("pullrequest:created", "pullrequest:updated"):                     # Bitbucket Cloud
            pr = payload["pullrequest"]
            if pr.get("state") != "OPEN" or pr.get("draft"):
                return None
            repo = payload["repository"]
            return {"pr": int(pr["id"]), "head_sha": ((pr.get("source") or {}).get("commit") or {}).get("hash"),
                    "base_ref": pr["destination"]["branch"]["name"], "source_ref": pr["source"]["branch"]["name"],
                    "remote_url": f"https://bitbucket.org/{repo['full_name']}.git", "full_name": repo["full_name"], "project_id": None, "event": key}
        if key in ("pr:opened", "pr:from_ref_updated", "pr:modified"):              # Bitbucket Data Center
            pr = payload["pullRequest"]
            if pr.get("state") != "OPEN" or pr.get("draft"):
                return None
            to = pr["toRef"]["repository"]
            clone = next((l["href"] for l in (to.get("links") or {}).get("clone", []) if l.get("name") in ("http", "https")), "")
            return {"pr": int(pr["id"]), "head_sha": pr["fromRef"].get("latestCommit"), "base_ref": pr["toRef"]["displayId"],
                    "source_ref": pr["fromRef"]["displayId"], "remote_url": clone, "full_name": f"{to['project']['key']}/{to['slug']}",
                    "project_id": None, "event": key}
        return None
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"malformed {cfg.provider} event: {e!r}")


# ---- REST ------------------------------------------------------------------------------------------------------------------------------
def _call(cfg: HostConfig, method: str, url: str, token: str, payload=None, operation: str = "publish"):
    if not allowed(cfg, url):
        raise ScmHostError(f"refusing to send the {cfg.provider} token to {urllib.parse.urlsplit(url).hostname}")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "tia"}
    data = None
    if payload is not None:
        data, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    outcome = "success"
    try:
        with urllib.request.urlopen(airgap.checked(req, cfg.provider), timeout=20) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        outcome = f"http_{e.code // 100}xx"
        raise ScmHostError(f"{cfg.provider} answered HTTP {e.code}" + (" (check the token's scopes)" if e.code in (401, 403) else ""),
                           permanent=400 <= e.code < 500 and e.code != 429, code=e.code)
    except OSError:
        outcome = "unreachable"
        raise ScmHostError(f"{cfg.provider} could not be reached", permanent=False)
    finally:
        metrics.CONNECTOR.labels(provider=cfg.provider, operation=operation, outcome=outcome).inc()


def full_name(cfg: HostConfig, url: str) -> Optional[str]:
    """group/subgroup/project (GitLab), workspace/repo (Bitbucket Cloud) or PROJECT/repo (Data Center) from a clone URL."""
    u = urllib.parse.urlsplit(url or "")
    if not u.hostname or u.hostname.lower() != (urllib.parse.urlsplit(cfg.base_url).hostname or "").lower():
        return None
    parts = [p for p in u.path.strip("/").removesuffix(".git").split("/") if p]
    if cfg.provider == "bitbucket" and not cfg.cloud and parts[:1] == ["scm"]:
        parts = parts[1:]
    return "/".join(parts) if len(parts) >= 2 else None


def _enc(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def _state(report: dict, provider: str) -> tuple:
    concl = check_conclusion(report.get("policy") and {**report["policy"], "results": report.get("policy_results", [])})
    s = report.get("summary") or {}
    advisory = concl in ("neutral", None)
    desc = (f"Advisory: {s.get('material_findings', 0)} material finding(s), report {report.get('status')}" if advisory else
            f"Policy {report['policy'].get('outcome')}: {s.get('material_findings', 0)} material finding(s)")
    if provider == "gitlab":
        state = {"failure": "failed", "action_required": "pending"}.get(concl, "success")
    else:
        state = {"failure": "FAILED", "action_required": "INPROGRESS"}.get(concl, "SUCCESSFUL")
    return state, desc[:250]


def upsert_comment(cfg: HostConfig, token: str, full: str, pr: int, body: str, project_id: Optional[str] = None) -> str:
    if cfg.provider == "gitlab":
        base = f"{cfg.api}/projects/{_enc(project_id or full)}/merge_requests/{int(pr)}/notes"
        for n in _call(cfg, "GET", base + "?per_page=100&sort=asc", token) or []:
            if MARKER in (n.get("body") or ""):
                _call(cfg, "PUT", f"{base}/{n['id']}", token, {"body": body})
                return "updated"
        _call(cfg, "POST", base, token, {"body": body})
        return "created"
    if cfg.cloud:
        base = f"{cfg.api}/repositories/{full}/pullrequests/{int(pr)}/comments"
        for c in (_call(cfg, "GET", base + "?pagelen=100", token) or {}).get("values", []):
            if MARKER in ((c.get("content") or {}).get("raw") or ""):
                _call(cfg, "PUT", f"{base}/{c['id']}", token, {"content": {"raw": body}})
                return "updated"
        _call(cfg, "POST", base, token, {"content": {"raw": body}})
        return "created"
    proj, slug = full.split("/", 1)
    base = f"{cfg.api}/api/1.0/projects/{_enc(proj)}/repos/{_enc(slug)}/pull-requests/{int(pr)}"
    for a in (_call(cfg, "GET", base + "/activities?limit=100", token) or {}).get("values", []):
        c = a.get("comment") or {}
        if a.get("action") == "COMMENTED" and MARKER in (c.get("text") or ""):
            _call(cfg, "PUT", f"{base}/comments/{c['id']}", token, {"text": body, "version": c.get("version", 0)})
            return "updated"
    _call(cfg, "POST", f"{base}/comments", token, {"text": body})
    return "created"


def post_status(cfg: HostConfig, token: str, full: str, sha: str, report: dict, link: Optional[str], project_id: Optional[str] = None) -> str:
    state, desc = _state(report, cfg.provider)
    if cfg.provider == "gitlab":
        q = urllib.parse.urlencode({"state": state, "name": STATUS_NAME, "description": desc, **({"target_url": link} if link else {})})
        _call(cfg, "POST", f"{cfg.api}/projects/{_enc(project_id or full)}/statuses/{sha}?{q}", token)
        return state
    body = {"state": state, "key": "tia-impact-analysis", "name": STATUS_NAME, "description": desc, "url": link or cfg.base_url}
    url = (f"{cfg.api}/repositories/{full}/commit/{sha}/statuses/build" if cfg.cloud else f"{cfg.api}/build-status/1.0/commits/{sha}")
    _call(cfg, "POST", url, token, body)
    return state


def publish(cfg: HostConfig, token: str, report: dict, view: dict, full: str, pr: int, sha: str, public_url: Optional[str],
            project_id: Optional[str] = None) -> dict:
    """The comment is the contract (its failure raises); the status is best effort."""
    from .report import permalink, render_comment
    link = permalink(public_url, report["analysis_id"])
    out = {"comment": upsert_comment(cfg, token, full, pr, render_comment(view, link=link), project_id), "status": None, "permalink": link}
    try:
        out["status"] = post_status(cfg, token, full, sha, view, link, project_id)
    except ScmHostError as e:
        out["status"] = {"skipped": str(e)}
    return out


# ---- permission mirror -----------------------------------------------------------------------------------------------------------------
def list_readers(cfg: HostConfig, full: str, token: str) -> list:
    """[(login, permission in GitHub's vocabulary)] of everyone the host says can read the repository's code."""
    out = {}
    if cfg.provider == "gitlab":
        page = 1
        while True:
            batch = _call(cfg, "GET", f"{cfg.api}/projects/{_enc(full)}/members/all?per_page=100&page={page}", token, operation="permissions") or []
            for m in batch:
                lvl = int(m.get("access_level") or 0)
                perm = GITLAB_READ[max(k for k in GITLAB_READ if k <= lvl)] if lvl >= 20 else None
                if perm and m.get("state", "active") == "active":
                    out[m["username"].lower()] = perm
            if len(batch) < 100:
                break
            page += 1
        return sorted(out.items())
    if cfg.cloud:
        ws, slug = full.split("/", 1)
        url = f"{cfg.api}/workspaces/{ws}/permissions/repositories/{slug}?pagelen=100"
        while url:
            d = _call(cfg, "GET", url, token, operation="permissions") or {}
            for v in d.get("values", []):
                u = v.get("user") or {}
                login = (u.get("nickname") or u.get("account_id") or "").lower()
                if login and BITBUCKET_READ.get(v.get("permission")):
                    out[login] = BITBUCKET_READ[v["permission"]]
            url = d.get("next")
        return sorted(out.items())
    proj, slug = full.split("/", 1)
    for url in (f"{cfg.api}/api/1.0/projects/{_enc(proj)}/repos/{_enc(slug)}/permissions/users?limit=1000",
                f"{cfg.api}/api/1.0/projects/{_enc(proj)}/permissions/users?limit=1000"):
        for v in (_call(cfg, "GET", url, token, operation="permissions") or {}).get("values", []):
            login = ((v.get("user") or {}).get("name") or "").lower()
            perm = BITBUCKET_READ.get(v.get("permission"))
            rank = ("pull", "push", "admin")
            if login and perm and (login not in out or rank.index(perm) > rank.index(out[login])):
                out[login] = perm                                             # the strongest of repository and project permissions
    return sorted(out.items())
