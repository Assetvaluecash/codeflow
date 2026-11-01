"""SCM webhook receiving (UC1-05): signature verification, pull-request event parsing, repository matching,
and the server-side connector configuration used by the webhook endpoint and the worker.

Interim credential model: one GitHub token from the environment. It is replaced by per-tenant GitHub App
installation tokens (UC1-04) and the secrets store (UC1-21); nothing here persists it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .credentials import env_secret
from .transport import check_outbound

PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


@dataclass(frozen=True)
class ScmConfig:
    github_token: Optional[str] = None
    github_webhook_secret: Optional[bytes] = None
    github_api_url: str = "https://api.github.com"
    comment_audience: str = "target"        # "target": only the PR's own repository is named; "readers": also the repositories every reader
                                            # of the PR provably may read (access.comment_repos); "all": full cross-repo detail (ignores access control,
                                            # except that with authentication required it is treated as "readers")
    check_run: bool = True                  # also publish an advisory (always neutral) check run; needs a GitHub App token
    public_url: Optional[str] = None        # externally reachable base URL of this TIA: comments and check runs link to its report page
    github_app_id: Optional[str] = None     # the platform's GitHub App (UC1-04): installation tokens replace personal access tokens
    github_app_key: Optional[str] = None    # its RSA private key (PEM)
    github_app_client_id: Optional[str] = None       # to verify who may link an installation (user authorisation)
    github_app_client_secret: Optional[str] = None
    github_web_url: str = "https://github.com"

    @classmethod
    def from_env(cls) -> "ScmConfig":
        """Secrets may be literals (development) or references: file:..., vault:mount/path#key, or NAME_FILE (see credentials.py).
        The GitHub API URL must be https unless it is this machine."""
        secret = env_secret("TIA_GITHUB_WEBHOOK_SECRET")
        public = os.environ.get("TIA_PUBLIC_URL", "").strip().rstrip("/")
        return cls(github_token=env_secret("TIA_GITHUB_TOKEN") or None,
                   github_webhook_secret=secret.encode() if secret else None,
                   github_api_url=check_outbound(os.environ.get("TIA_GITHUB_API_URL", "https://api.github.com").rstrip("/"),
                                                 "TIA_GITHUB_API_URL"),
                   comment_audience=os.environ.get("TIA_COMMENT_AUDIENCE", "target"),
                   check_run=os.environ.get("TIA_CHECK_RUN", "true").strip().lower() not in ("0", "false", "no", "off"),
                   public_url=public or None, github_app_id=os.environ.get("TIA_GITHUB_APP_ID") or None,
                   github_app_key=env_secret("TIA_GITHUB_APP_PRIVATE_KEY") or None,
                   github_app_client_id=os.environ.get("TIA_GITHUB_APP_CLIENT_ID") or None,
                   github_app_client_secret=env_secret("TIA_GITHUB_APP_CLIENT_SECRET") or None,
                   github_web_url=check_outbound(os.environ.get("TIA_GITHUB_WEB_URL", "https://github.com").rstrip("/"), "TIA_GITHUB_WEB_URL"))


def with_stored_token(scm: "ScmConfig", secrets) -> "ScmConfig":
    """The token configured in the environment wins; otherwise the tenant's own stored secret 'github/token' (encrypted at rest)."""
    if scm.github_token or secrets is None:
        return scm
    token = secrets.get_text("github/token")
    return dataclasses.replace(scm, github_token=token) if token else scm


def with_tenant_config(scm: "ScmConfig", cfg) -> "ScmConfig":
    """The tenant's choice of comment audience (comments.audience), when it made one, over the deployment default."""
    return dataclasses.replace(scm, comment_audience=cfg.comment_audience) if cfg.comment_audience else scm


def verify_signature(secret: bytes, body: bytes, header: Optional[str]) -> bool:
    """GitHub's X-Hub-Signature-256: 'sha256=' + HMAC-SHA256(secret, raw body), compared in constant time."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


def parse_pull_request(payload: dict) -> Optional[dict]:
    """Returns the facts needed to analyse, or None when this event needs no analysis. Raises ValueError if malformed."""
    action = payload.get("action")
    relevant = action in PR_ACTIONS or (action == "edited" and "base" in (payload.get("changes") or {}))
    if not relevant:
        return None
    try:
        pr, repo = payload["pull_request"], payload["repository"]
        return {"action": action, "pr": int(pr["number"]), "head_sha": pr["head"]["sha"], "base_ref": pr["base"]["ref"],
                "full_name": repo["full_name"], "clone_url": repo["clone_url"], "draft": bool(pr.get("draft")),
                "installation_id": (payload.get("installation") or {}).get("id")}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"malformed pull_request event: {e!r}")


def github_full_name(url: Optional[str]) -> Optional[str]:
    """'owner/name' of a GitHub clone URL, or None when the URL is not an https/ssh GitHub style remote."""
    if not url or url.startswith(("file:", "/")) or "://" not in url and "@" not in url:
        return None
    path = urlsplit(url).path if "://" in url else url.split(":", 1)[-1]
    parts = [p for p in path.strip("/").removesuffix(".git").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def repo_key(url: str) -> str:
    u = urlsplit(url)
    path = u.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{(u.hostname or '').lower()}{path.lower()}"


def match_repo(repos: Iterable, clone_url: str) -> Optional[object]:
    """The registered repository whose URL is the event's repository (host + path, ignoring scheme, '.git', case)."""
    key = repo_key(clone_url)
    return next((r for r in repos if r.url and repo_key(r.url) == key), None)
