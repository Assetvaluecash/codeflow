"""Per-repository access control (PL-002, AC-016): which repositories a principal may read, mirrored from the source-code host.

A repository is governed in one of three ways (repo_access.mode; the default depends on where it came from):
  scm         the people the host says can read it (GitHub collaborators, teams and organisation members included), matched to
              TIA users by the source-code login on their profile (from the identity provider, or set by an administrator);
              hosted repositories default to this, so nothing is visible until the mirror proves it
  restricted  explicit grants only
  tenant      every member of the tenant (uploads and local paths default to this)

Fail closed: no mirror, a mirror older than the freshness limit, a user with no login on that host, an unknown mode - each means
"cannot read". A repository the caller cannot read does not exist as far as the API is concerned (404, not 403).

The comment a pull request receives is visible to everyone who can see that pull request on the host. So it may name another
repository only if every one of the target's readers is provably a reader of that repository too (and the target is private).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, Optional

from . import github
from .identity import Principal

MODES = ("scm", "tenant", "restricted")
READ_PERMS = frozenset(github.ROLE_ORDER)
HOSTS = {"github": "github"}                  # repository provider -> the host name used on user profiles and in the mirror
DEFAULT_MAX_AGE = 3600


def max_age() -> int:
    try:
        return max(60, int(os.environ.get("TIA_ACCESS_MAX_AGE_S", DEFAULT_MAX_AGE)))
    except ValueError:
        return DEFAULT_MAX_AGE


def default_mode(provider: str) -> str:
    """GitHub: the host's own list. Uploads and local paths have no host and belong to the tenant. Any other hosted repository (Azure DevOps,
    a plain git server) has no mirror yet, so nobody reads it until an administrator grants it: fail closed."""
    if provider in HOSTS:
        return "scm"
    return "tenant" if provider in ("upload", "local") else "restricted"


class AccessStore:
    def __init__(self, db, tenant_id: str, age: Optional[int] = None):
        self.db, self.tenant_id = db, str(tenant_id)
        self.age = age or max_age()
        self._cache: dict = {}
        self._lock = threading.Lock()

    # ---- configuration ----------------------------------------------------------------------------------------------------------
    def modes(self) -> dict:
        with self.db.tx(self.tenant_id) as c:
            return {r: m for r, m in c.execute("select repo, mode from repo_access where tenant_id = %s", (self.tenant_id,))}

    def set_mode(self, repo: str, mode: str, actor: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into repo_access (tenant_id, repo, mode, updated_by) values (%s,%s,%s,%s) "
                      "on conflict (tenant_id, repo) do update set mode = excluded.mode, updated_by = excluded.updated_by, updated_at = now()",
                      (self.tenant_id, repo, mode, actor))
        self.forget()

    def grants(self, repo: Optional[str] = None) -> dict:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select repo, user_id from repo_grants where tenant_id = %s and (%s::text is null or repo = %s)",
                             (self.tenant_id, repo, repo)).fetchall()
        out: dict = {}
        for r, u in rows:
            out.setdefault(r, set()).add(str(u))
        return out

    def set_grants(self, repo: str, user_ids: Iterable[str], actor: str) -> None:
        """Replace the grants of one repository."""
        ids = sorted({str(u) for u in user_ids})
        with self.db.tx(self.tenant_id) as c:
            c.execute("delete from repo_grants where tenant_id = %s and repo = %s", (self.tenant_id, repo))
            for u in ids:
                c.execute("insert into repo_grants (tenant_id, repo, user_id, granted_by) values (%s,%s,%s,%s)", (self.tenant_id, repo, u, actor))
        self.forget()

    def forget(self):
        with self._lock:
            self._cache.clear()

    # ---- the mirror ---------------------------------------------------------------------------------------------------------------
    def mirror(self) -> dict:
        """{(host, repo): {'logins': {login: permission}, 'fresh': bool, 'private': bool|None}}; only confirmed, fresh rows count as readers."""
        out: dict = {}
        with self.db.tx(self.tenant_id) as c:
            for host, repo, login, perm, fresh in c.execute(
                    "select host, repo, login, permission, synced_at > now() - make_interval(secs => %s) from scm_permissions where tenant_id = %s",
                    (self.age, self.tenant_id)):
                e = out.setdefault((host, repo), {"logins": {}, "fresh": True, "private": None})
                e["logins"][login] = perm
                e["fresh"] = e["fresh"] and bool(fresh)
            for host, repo, ok, private in c.execute("select host, repo, ok, private from scm_sync_status where tenant_id = %s", (self.tenant_id,)):
                if (host, repo) in out:
                    out[(host, repo)]["private"] = private
        return out

    def status(self) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select host, repo, synced_at, ok, error, collaborators, private, attempted_at, "
                             "synced_at > now() - make_interval(secs => %s) from scm_sync_status where tenant_id = %s order by repo",
                             (self.age, self.tenant_id)).fetchall()
        f = lambda d: d.isoformat(timespec="seconds") if d else None
        return [{"host": r[0], "repo": r[1], "synced_at": f(r[2]), "ok": r[3], "error": r[4], "collaborators": r[5], "private": r[6],
                 "attempted_at": f(r[7]), "fresh": bool(r[8])} for r in rows]

    def sync(self, repos: list, token, api_url: str = "https://api.github.com", lister: Optional[Callable] = None,
             describe: Optional[Callable] = None) -> list[dict]:
        """Refresh the mirror of every hosted repository from the host. One repository failing does not stop the others; its old rows are
        kept (so they age out and stop granting access) and the failure is recorded."""
        from .webhooks import github_full_name
        lister = lister or github.list_collaborators
        describe = describe or (lambda full, tok, api: github._call("GET", f"{api}/repos/{full}", tok))
        results = []
        for r in repos:
            host = HOSTS.get(r.provider)
            full = github_full_name(r.url) if host == "github" else None
            if not full:
                continue
            try:
                tok = token(r) if callable(token) else token                 # with a GitHub App the token differs per installation
                collab = lister(full, tok, api_url)
                meta = describe(full, tok, api_url) or {}
                private = meta.get("private") if isinstance(meta.get("private"), bool) else None
                with self.db.tx(self.tenant_id) as c:
                    c.execute("delete from scm_permissions where tenant_id = %s and host = %s and repo = %s", (self.tenant_id, host, r.name))
                    for login, perm in collab:
                        c.execute("insert into scm_permissions (tenant_id, host, repo, login, permission) values (%s,%s,%s,%s,%s) "
                                  "on conflict (tenant_id, host, repo, login) do update set permission = excluded.permission, synced_at = now()",
                                  (self.tenant_id, host, r.name, login.lower(), perm))
                    c.execute("insert into scm_sync_status (tenant_id, host, repo, synced_at, ok, error, collaborators, private, attempted_at) "
                              "values (%s,%s,%s, now(), true, null, %s, %s, now()) on conflict (tenant_id, host, repo) do update set "
                              "synced_at = now(), ok = true, error = null, collaborators = excluded.collaborators, private = excluded.private, attempted_at = now()",
                              (self.tenant_id, host, r.name, len(collab), private))
                results.append({"repo": r.name, "ok": True, "collaborators": len(collab), "private": private})
            except Exception as e:                                           # noqa: BLE001 - recorded, never raised: one bad repository must not hide the rest
                code = getattr(e, "code", None)
                msg = str(e) if type(e).__name__ == "AppError" else f"the host answered HTTP {code}" if code else "the host could not be reached"
                if code in (403, 404):
                    msg += " (listing collaborators needs a token with push access to the repository)"
                with self.db.tx(self.tenant_id) as c:
                    c.execute("insert into scm_sync_status (tenant_id, host, repo, ok, error, attempted_at) values (%s,%s,%s,false,%s,now()) "
                              "on conflict (tenant_id, host, repo) do update set ok = false, error = excluded.error, attempted_at = now()",
                              (self.tenant_id, host, r.name, msg))
                results.append({"repo": r.name, "ok": False, "error": msg})
        self.forget()
        return results

    # ---- decisions ----------------------------------------------------------------------------------------------------------------
    def readable(self, p: Optional[Principal], repos: list) -> Optional[frozenset]:
        """None means every repository (development mode, or a service token scoped '*'); otherwise exactly the names the principal may read."""
        if p is None or p.kind == "system":
            return None
        names = {r.name for r in repos}
        if p.kind == "service":
            return None if p.repo_scope == "*" else frozenset(names & set(p.repo_scope))
        key = (p.user_id, tuple(sorted(names)))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < 5.0:
                return hit[1]
        modes, grants, mirror = self.modes(), self.grants(), self.mirror()
        ok = set()
        for r in repos:
            mode = modes.get(r.name) or default_mode(r.provider)
            if mode == "tenant":
                ok.add(r.name)
            elif mode == "restricted":
                if p.user_id and p.user_id in grants.get(r.name, ()):
                    ok.add(r.name)
            elif mode == "scm":
                host = HOSTS.get(r.provider)
                login = (p.scm_logins.get(host) or "").lower() if host else ""
                m = mirror.get((host, r.name))
                if login and m and m["fresh"] and m["logins"].get(login) in READ_PERMS:
                    ok.add(r.name)
        out = frozenset(ok)
        with self._lock:
            self._cache[key] = (time.monotonic(), out)
        return out

    def comment_repos(self, target: str, repos: list) -> list[str]:
        """Repositories a comment on `target`'s pull request may name: the target itself, plus any private hosted repository whose readers
        provably include every reader of the target. Anything that cannot be proved is left out."""
        by = {r.name: r for r in repos}
        t = by.get(target)
        out = [target]
        if t is None or HOSTS.get(t.provider) is None:
            return out
        modes, mirror = self.modes(), self.mirror()
        tm = mirror.get((HOSTS[t.provider], target))
        if (modes.get(target) or default_mode(t.provider)) != "scm" or not tm or not tm["fresh"] or tm["private"] is not True or not tm["logins"]:
            return out
        readers = {l for l, perm in tm["logins"].items() if perm in READ_PERMS}
        for r in repos:
            if r.name == target or HOSTS.get(r.provider) is None or (modes.get(r.name) or default_mode(r.provider)) != "scm":
                continue
            m = mirror.get((HOSTS[r.provider], r.name))
            if m and m["fresh"] and readers <= {l for l, perm in m["logins"].items() if perm in READ_PERMS}:
                out.append(r.name)
        return out
