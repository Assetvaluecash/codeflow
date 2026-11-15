"""Azure DevOps connector (UC1-06 service hooks, UC1-15 pull-request thread and status).

Receiving: an Azure DevOps *service hook* subscription (Web Hooks consumer) for "Pull request created" and "Pull request updated" posts to
/api/v1/webhooks/ado. It is authenticated by the basic-authentication user name and password set on the subscription
(TIA_ADO_HOOK_USERNAME / TIA_ADO_HOOK_PASSWORD), compared in constant time. The event says which organisation, project, repository and pull
request; the work is queued and the HTTP answer is immediate.

Publishing: one comment THREAD per pull request, found again by TIA's marker and updated in place (closed, so a "resolve all comments" branch
policy is never blocked), plus a pull-request STATUS in the "tia/impact-analysis" context whose state is always `notApplicable`: advisory,
never a gate. Calls carry the organisation's personal access token (TIA_ADO_TOKEN, or the tenant's stored secret `ado/token`) and are made
only to Azure DevOps hosts (dev.azure.com, *.visualstudio.com) or the host configured in TIA_ADO_API_URL (Azure DevOps Server).

Permission mirror (PL-002, FR-039): who may read a repository, from the Git Repositories security namespace - the access control lists at
all-repositories, project and repository level (the most specific explicit setting wins; a deny beats an allow), groups expanded to their
users through the identities service. Users are matched to TIA users by their Azure DevOps account (user principal name / e-mail).
Organisations are routed to tenants by the operator's installation mapping (`python -m tia.tenants map-installation <tenant> ado <collection id>`).
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .policy import ado_state
from . import metrics
from .report import MARKER
from . import airgap

API = "api-version=7.1"
STATUS_CONTEXT = {"name": "impact-analysis", "genre": "tia"}
PR_EVENTS = {"git.pullrequest.created", "git.pullrequest.updated"}


class AdoError(Exception):
    def __init__(self, message: str, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class AdoConfig:
    hook_username: Optional[str] = None
    hook_password: Optional[str] = None
    token: Optional[str] = None
    api_url: Optional[str] = None                 # Azure DevOps Server (on-premises) base URL; its host is allowed in addition to the cloud hosts

    @classmethod
    def from_env(cls) -> "AdoConfig":
        from .credentials import env_secret
        return cls(hook_username=os.environ.get("TIA_ADO_HOOK_USERNAME") or None, hook_password=env_secret("TIA_ADO_HOOK_PASSWORD") or None,
                   token=env_secret("TIA_ADO_TOKEN") or None, api_url=(os.environ.get("TIA_ADO_API_URL") or "").rstrip("/") or None)

    @property
    def receiving(self) -> bool:
        return bool(self.hook_username and self.hook_password)


def verify_basic(cfg: AdoConfig, header: Optional[str]) -> bool:
    if not cfg.receiving or not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8").partition(":")
    except Exception:
        return False
    ok_user = hmac.compare_digest(user.encode(), cfg.hook_username.encode())
    ok_pw = hmac.compare_digest(pw.encode(), cfg.hook_password.encode())
    return ok_user and ok_pw


def allowed_host(cfg: AdoConfig, url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if cfg.api_url and host == (urllib.parse.urlsplit(cfg.api_url).hostname or "").lower():
        return u.scheme in ("https", "http")
    return u.scheme == "https" and (host in ("dev.azure.com", "vssps.dev.azure.com") or host.endswith(".visualstudio.com"))


def parse_pull_request_event(payload: dict) -> Optional[dict]:
    """Facts needed to analyse, or None when the event needs no analysis (not a PR event, abandoned/completed, a draft). ValueError if malformed."""
    if payload.get("eventType") not in PR_EVENTS:
        return None
    try:
        r = payload["resource"]
        if r.get("status") not in (None, "active") or r.get("isDraft"):
            return None
        repo = r["repository"]
        coll = (payload.get("resourceContainers") or {}).get("collection") or (payload.get("resourceContainers") or {}).get("account") or {}
        base = (coll.get("baseUrl") or "").rstrip("/")
        if not base:
            raise ValueError("the event carries no organisation base URL (resourceContainers.collection.baseUrl)")
        return {"pr": int(r["pullRequestId"]), "head_sha": (r.get("lastMergeSourceCommit") or {}).get("commitId"),
                "base_ref": str(r["targetRefName"]).removeprefix("refs/heads/"), "source_ref": str(r.get("sourceRefName", "")).removeprefix("refs/heads/"),
                "repository_id": str(repo["id"]), "repository": repo.get("name"), "project": (repo.get("project") or {}).get("name"),
                "remote_url": repo.get("remoteUrl") or "", "base_url": base, "collection_id": str(coll.get("id") or "") or None,
                "event": payload.get("eventType")}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"malformed pull-request event: {e!r}")


# ---- REST calls ------------------------------------------------------------------------------------------------------------------
def _call(cfg: AdoConfig, method: str, url: str, token: str, payload=None):
    if not allowed_host(cfg, url):
        raise AdoError(f"refusing to send the Azure DevOps token to {urllib.parse.urlsplit(url).hostname}", permanent=True)
    auth = base64.b64encode(f":{token}".encode()).decode()
    req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "tia"})
    outcome = "success"
    try:
        with urllib.request.urlopen(airgap.checked(req, "ado"), timeout=20) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        outcome = f"http_{e.code // 100}xx"
        raise AdoError(f"Azure DevOps answered HTTP {e.code}" + (" (the token needs Code: read and Pull request threads: read & write)" if e.code in (401, 403) else ""),
                       permanent=400 <= e.code < 500 and e.code != 429)
    except OSError:
        outcome = "unreachable"
        raise AdoError("Azure DevOps could not be reached", permanent=False)
    finally:
        metrics.CONNECTOR.labels(provider="ado", operation="publish", outcome=outcome).inc()


def _pr_base(base_url: str, project: str, repository_id: str, pr: int) -> str:
    return f"{base_url}/{urllib.parse.quote(project)}/_apis/git/repositories/{urllib.parse.quote(repository_id)}/pullRequests/{int(pr)}"


def upsert_thread(cfg: AdoConfig, token: str, base_url: str, project: str, repository_id: str, pr: int, body: str) -> str:
    base = _pr_base(base_url, project, repository_id, pr)
    threads = (_call(cfg, "GET", f"{base}/threads?{API}", token) or {}).get("value", [])
    for t in threads:
        comments = t.get("comments") or []
        if comments and MARKER in (comments[0].get("content") or ""):
            _call(cfg, "PATCH", f"{base}/threads/{t['id']}/comments/{comments[0]['id']}?{API}", token, {"content": body})
            return "updated"
    _call(cfg, "POST", f"{base}/threads?{API}", token, {"comments": [{"parentCommentId": 0, "content": body, "commentType": 1}], "status": "closed"})
    return "created"


def post_status(cfg: AdoConfig, token: str, base_url: str, project: str, repository_id: str, pr: int, report: dict, link: Optional[str]) -> str:
    s = report.get("summary") or {}
    pol = report.get("policy") and {**report["policy"], "results": report.get("policy_results", [])}
    state = ado_state(pol)
    desc = (f"Advisory: {s.get('material_findings', 0)} material finding(s), report {report.get('status')}" if state == "notApplicable" else
            f"Policy {report['policy'].get('outcome')}: {s.get('material_findings', 0)} material finding(s), report {report.get('status')}")
    _call(cfg, "POST", f"{_pr_base(base_url, project, repository_id, pr)}/statuses?api-version=7.1-preview.1", token,
          {"state": state, "description": desc[:250], "context": STATUS_CONTEXT, **({"targetUrl": link} if link else {})})
    return "created"


def publish(cfg: AdoConfig, token: str, report: dict, view: dict, ev: dict, public_url: Optional[str]) -> dict:
    """The comment thread is the contract (its failure raises); the status is best effort."""
    from .report import permalink, render_comment
    link = permalink(public_url, report["analysis_id"])
    out = {"comment": upsert_thread(cfg, token, ev["base_url"], ev["project"], ev["repository_id"], ev["pr"], render_comment(view, link=link)),
           "status": None, "permalink": link}
    try:
        out["status"] = post_status(cfg, token, ev["base_url"], ev["project"], ev["repository_id"], ev["pr"], view, link)
    except AdoError as e:
        out["status"] = {"skipped": str(e)}
    return out


# ---- permission mirror -------------------------------------------------------------------------------------------------------------
GIT_NAMESPACE = "2e9eb7ed-3c0a-47d4-87c1-0ffdd275fd87"         # the "Git Repositories" security namespace
READ, CONTRIBUTE, MANAGE = 2, 4, 8192                            # GenericRead, GenericContribute, ManagePermissions
BITS = (READ, CONTRIBUTE, MANAGE)


def identity_base(base_url: str) -> str:
    """Where the identities service of an organisation lives: vssps for the cloud, the collection itself for Azure DevOps Server."""
    u = urllib.parse.urlsplit(base_url)
    host = (u.hostname or "").lower()
    if host == "dev.azure.com":
        return f"https://vssps.dev.azure.com{u.path.rstrip('/')}"
    if host.endswith(".visualstudio.com") and ".vssps." not in host:
        return f"https://{host.split('.')[0]}.vssps.visualstudio.com"
    return base_url.rstrip("/")


def _effective(cfg: AdoConfig, token: str, base_url: str, project_id: str, repo_id: str) -> dict:
    """descriptor -> {bit: 'allow' | 'deny'} after inheritance: all repositories, then the project, then the repository; a level with an
    explicit setting for a bit replaces what it inherited, and a level that does not inherit starts from nothing."""
    state: dict = {}
    for tok in ("repoV2", f"repoV2/{project_id}", f"repoV2/{project_id}/{repo_id}"):
        d = _call(cfg, "GET", f"{base_url}/_apis/accesscontrollists/{GIT_NAMESPACE}?token={urllib.parse.quote(tok, safe='')}&{API}", token) or {}
        for acl in d.get("value") or []:
            if acl.get("token") != tok:
                continue
            if acl.get("inheritPermissions") is False:
                state = {}
            for desc, ace in (acl.get("acesDictionary") or {}).items():
                allow, deny = int(ace.get("allow") or 0), int(ace.get("deny") or 0)
                cur = state.setdefault(desc, {})
                for bit in BITS:
                    if deny & bit:
                        cur[bit] = "deny"
                    elif allow & bit:
                        cur[bit] = "allow"
    return state


def _identities(cfg: AdoConfig, token: str, base_url: str, descriptors: list) -> list:
    out = []
    ib = identity_base(base_url)
    for i in range(0, len(descriptors), 50):
        chunk = ",".join(descriptors[i:i + 50])
        d = _call(cfg, "GET", f"{ib}/_apis/identities?descriptors={urllib.parse.quote(chunk, safe=',')}&queryMembership=Expanded&{API}", token) or {}
        out += [x for x in (d.get("value") or []) if x]
    return out


def _login(identity: dict) -> Optional[str]:
    props = identity.get("properties") or {}
    acct = (props.get("Account") or {}).get("$value") or (props.get("Mail") or {}).get("$value")
    return str(acct).lower() if acct else None


def list_readers(cfg: AdoConfig, token: str, base_url: str, project: str, repository: str) -> list:
    """[(login, permission in GitHub's vocabulary: pull | push | admin)] of everyone Azure DevOps lets read the repository's code."""
    repo = _call(cfg, "GET", f"{base_url}/{urllib.parse.quote(project)}/_apis/git/repositories/{urllib.parse.quote(repository)}?{API}", token)
    if not repo or not repo.get("id") or not (repo.get("project") or {}).get("id"):
        raise AdoError(f"repository {project}/{repository} was not found", permanent=True)
    state = _effective(cfg, token, base_url, repo["project"]["id"], repo["id"])
    if not state:
        return []
    ids = _identities(cfg, token, base_url, sorted(state))
    by_desc = {x.get("descriptor"): x for x in ids}
    # every user and the descriptors that apply to them: their own, and every group (membership already expanded) they belong to
    applies: dict = {}
    pending_users = set()
    for desc, ident in by_desc.items():
        if ident.get("isContainer"):
            for m in ident.get("members") or []:
                applies.setdefault(m, set()).add(desc)
                pending_users.add(m)
        else:
            applies.setdefault(desc, set()).add(desc)
    unknown = sorted(m for m in pending_users if m not in by_desc)
    for x in _identities(cfg, token, base_url, unknown) if unknown else []:
        by_desc[x.get("descriptor")] = x
    out = {}
    for user, descs in applies.items():
        ident = by_desc.get(user)
        if not ident or ident.get("isContainer") or ident.get("isActive") is False:
            continue
        login = _login(ident)
        if not login:
            continue
        granted = set()
        for bit in BITS:
            seen = [state.get(d, {}).get(bit) for d in descs]
            if "allow" in seen and "deny" not in seen:                 # a deny on any of the user's identities wins
                granted.add(bit)
        if READ not in granted:
            continue
        out[login] = "admin" if MANAGE in granted else "push" if CONTRIBUTE in granted else "pull"
    return sorted(out.items())


def parse_remote(url: str) -> Optional[dict]:
    """base_url / project / repository from an Azure DevOps remote: https://dev.azure.com/{org}/{project}/_git/{repo}."""
    u = urllib.parse.urlsplit(url or "")
    parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/")]
    if "_git" not in parts or u.scheme not in ("https", "http"):
        return None
    i = parts.index("_git")
    if i < 1 or i + 1 >= len(parts):
        return None
    host = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    if u.hostname and u.hostname.endswith(".visualstudio.com"):             # {org}.visualstudio.com/{project}/_git/{repo}
        return {"base_url": host, "project": parts[i - 1], "repository": parts[i + 1]}
    return {"base_url": host + "/" + "/".join(parts[: i - 1]), "project": parts[i - 1], "repository": parts[i + 1]}
