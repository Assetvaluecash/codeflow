"""GitHub App connector (UC1-04): the platform's own GitHub App replaces personal access tokens.

  app JWT (RS256, signed with the App's private key, 9 minutes)  ->  installation access token (1 hour, scoped to the repositories the
  customer chose)  ->  clone/fetch, pull-request comments, check runs.

A webhook says which installation it came from; `tenant_installations` says which tenant owns that installation, so the event is
processed for that tenant and no other. An installation is linked to a tenant only by (a) the operator (`python -m tia.tenants
map-installation`) or (b) a tenant administrator who proves, through GitHub's user authorisation (OAuth code), that they can
see that installation - nobody can claim an installation id they merely know.

Nothing here logs, audits or returns a token or the private key. Failures are reported as what they mean (installation removed or
suspended, repository not part of the installation), never as a bare HTTP status.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import github, jwtverify
from .webhooks import ScmConfig, github_full_name

REFRESH_BEFORE = 300.0


class AppError(Exception):
    """code: not_configured | not_installed | installation_gone | no_access | unreachable | bad_key ; the message is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_key(pem: str):
    try:
        key = serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem, password=None)
    except Exception:
        raise AppError("bad_key", "TIA_GITHUB_APP_PRIVATE_KEY is not a usable RSA private key in PEM form")
    if not hasattr(key, "sign") or getattr(key, "key_size", 0) < 2048:
        raise AppError("bad_key", "the GitHub App private key must be an RSA key of at least 2048 bits")
    return key


class GitHubApp:
    def __init__(self, app_id: str, private_key_pem: str, api_url: str = "https://api.github.com", web_url: str = "https://github.com",
                 client_id: Optional[str] = None, client_secret: Optional[str] = None, call: Optional[Callable] = None,
                 oauth_post: Optional[Callable] = None, clock: Callable[[], float] = time.time):
        self.app_id, self.api, self.web = str(app_id), api_url.rstrip("/"), web_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self._key = load_key(private_key_pem)
        self._call = call or github._call
        self._oauth_post = oauth_post or self._default_oauth_post
        self.clock = clock
        self._tokens: dict = {}
        self._lock = threading.Lock()

    # ---- the App's own identity -------------------------------------------------------------------------------------------------
    def jwt(self) -> str:
        now = int(self.clock())
        h = jwtverify.b64e(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        p = jwtverify.b64e(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())      # GitHub: at most 10 minutes
        sig = self._key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{jwtverify.b64e(sig)}"

    def _as_app(self, method: str, path: str, payload: Optional[dict] = None):
        try:
            return self._call(method, self.api + path, self.jwt(), payload)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AppError("not_configured", "GitHub rejected the App credentials (check TIA_GITHUB_APP_ID and the private key)")
            if e.code == 404:
                raise AppError("not_installed", "GitHub has no such installation or repository for this App")
            raise AppError("unreachable", f"GitHub answered HTTP {e.code}")
        except OSError:
            raise AppError("unreachable", "GitHub could not be reached")

    def installation(self, installation_id) -> dict:
        i = self._as_app("GET", f"/app/installations/{int(installation_id)}") or {}
        if i.get("suspended_at"):
            raise AppError("installation_gone", f"GitHub App installation {installation_id} is suspended")
        return {"id": str(i.get("id")), "account": (i.get("account") or {}).get("login"), "account_type": (i.get("account") or {}).get("type", "Organization"),
                "permissions": i.get("permissions") or {},
                "repository_selection": i.get("repository_selection")}

    def installation_for_repo(self, full_name: str) -> str:
        """The installation of this App on a repository; AppError(not_installed) when the App is not installed there."""
        try:
            i = self._as_app("GET", f"/repos/{full_name}/installation") or {}
        except AppError as e:
            if e.code == "not_installed":
                raise AppError("not_installed", f"the GitHub App is not installed on {full_name} for this organisation")
            raise
        if not i.get("id"):
            raise AppError("not_installed", f"the GitHub App is not installed on {full_name}")
        return str(i["id"])

    def installation_token(self, installation_id) -> str:
        iid = str(int(installation_id))
        with self._lock:
            hit = self._tokens.get(iid)
            if hit and hit[1] - self.clock() > REFRESH_BEFORE:
                return hit[0]
        try:
            r = self._as_app("POST", f"/app/installations/{iid}/access_tokens", {}) or {}
        except AppError as e:
            if e.code == "not_installed":
                raise AppError("installation_gone", f"GitHub App installation {iid} no longer exists (uninstalled)")
            raise
        token = r.get("token")
        if not isinstance(token, str):
            raise AppError("no_access", f"GitHub gave no access token for installation {iid}")
        try:
            expires = time.mktime(time.strptime(r["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except (KeyError, ValueError, TypeError):
            expires = self.clock() + 3000
        with self._lock:
            self._tokens[iid] = (token, expires)
        return token

    def forget(self, installation_id=None):
        with self._lock:
            if installation_id is None:
                self._tokens.clear()
            else:
                self._tokens.pop(str(installation_id), None)

    def repositories(self, installation_id) -> list[dict]:
        """The repositories the customer chose to give this installation: [{full_name, name, clone_url, private, default_branch}]."""
        token, out, page = self.installation_token(installation_id), [], 1
        while True:
            try:
                r = self._call("GET", f"{self.api}/installation/repositories?per_page=100&page={page}", token) or {}
            except urllib.error.HTTPError as e:
                self.forget(installation_id)
                raise AppError("no_access", f"GitHub refused the installation's repository list (HTTP {e.code})")
            batch = r.get("repositories") or []
            out += [{"full_name": x["full_name"], "name": x["name"], "clone_url": x["clone_url"], "private": bool(x.get("private")),
                     "default_branch": x.get("default_branch")} for x in batch]
            if len(batch) < 100:
                return out
            page += 1

    # ---- proving who may link an installation ---------------------------------------------------------------------------------
    def _default_oauth_post(self, url: str, form: dict) -> dict:
        req = urllib.request.Request(url, method="POST", data=urllib.parse.urlencode(form).encode(),
                                     headers={"Accept": "application/json", "User-Agent": "tia"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")

    def user_sees_installation(self, oauth_code: str, installation_id, account: Optional[str] = None, account_type: str = "Organization") -> bool:
        """GitHub's own answer to "may this person administer that installation": exchange the code for a user token, require that the
        installation is among those the user can access AND that the user OWNS the account it is installed on (an organisation's owner,
        or the user themself): an outside collaborator on one repository can see an installation without being entitled to link it.
        Needs the App's client id and secret."""
        if not (self.client_id and self.client_secret):
            raise AppError("not_configured", "linking an installation needs TIA_GITHUB_APP_CLIENT_ID and TIA_GITHUB_APP_CLIENT_SECRET")
        try:
            r = self._oauth_post(self.web + "/login/oauth/access_token", {"client_id": self.client_id, "client_secret": self.client_secret,
                                                                          "code": oauth_code})
            ut = r.get("access_token")
            if not isinstance(ut, str):
                return False
            page = 1
            while True:
                j = self._call("GET", f"{self.api}/user/installations?per_page=100&page={page}", ut) or {}
                ins = j.get("installations") or []
                if any(str(i.get("id")) == str(installation_id) for i in ins):
                    return self._owns(ut, account, account_type)
                if len(ins) < 100:
                    return False
                page += 1
        except (urllib.error.URLError, OSError, ValueError):
            raise AppError("unreachable", "GitHub could not be reached to verify the installation")


def _owns_account(self, user_token: str, account: Optional[str], account_type: str) -> bool:
    if not account:
        return False
    try:
        if account_type == "User":
            me = self._call("GET", f"{self.api}/user", user_token) or {}
            return str(me.get("login", "")).lower() == account.lower()
        m = self._call("GET", f"{self.api}/user/memberships/orgs/{urllib.parse.quote(account)}", user_token) or {}
        return m.get("role") == "admin" and m.get("state") == "active"
    except urllib.error.HTTPError:
        return False


GitHubApp._owns = _owns_account


def from_scm(scm: ScmConfig) -> Optional[GitHubApp]:
    if not (scm.github_app_id and scm.github_app_key):
        return None
    return GitHubApp(scm.github_app_id, scm.github_app_key, scm.github_api_url, scm.github_web_url, scm.github_app_client_id,
                     scm.github_app_client_secret)


def my_installations(db, tenant_id: str, provider: str = "github") -> list[dict]:
    with db.tx(tenant_id) as c:
        return [{"installation_id": i, "account": a} for i, a in c.execute("select installation_id, account from tia_my_installations(%s)", (provider,))]


def installation_tenant(db, installation_id, provider: str = "github") -> Optional[str]:
    with db.plain() as c:
        r = c.execute("select tia_installation_tenant(%s, %s)", (provider, str(installation_id))).fetchone()[0]
    return str(r) if r else None


class TokenSource:
    """Which GitHub credential to use for a repository of a tenant: an installation token when the App is configured (and the
    installation belongs to this tenant), otherwise the interim personal/stored token."""

    def __init__(self, scm: ScmConfig, db, app: Optional[GitHubApp] = None):
        self.scm, self.db = scm, db
        self.app = app if app is not None else from_scm(scm)

    @property
    def uses_app(self) -> bool:
        return self.app is not None

    def token_for(self, tenant_id: str, repo_url: Optional[str] = None, full_name: Optional[str] = None, hint=None,
                  fallback: Optional[str] = None) -> Optional[str]:
        if self.app is None:
            return fallback or self.scm.github_token
        full = full_name or (github_full_name(repo_url) if repo_url else None)
        mine = {i["installation_id"] for i in my_installations(self.db, tenant_id)}
        if not mine:
            raise AppError("not_installed", "no GitHub App installation is linked to this organisation yet")
        iid = str(hint) if hint is not None and str(hint) in mine else None
        if iid is None:
            if not full:
                raise AppError("not_installed", "the repository has no GitHub address to find an installation for")
            iid = self.app.installation_for_repo(full)
            if iid not in mine:
                raise AppError("not_installed", f"the GitHub App is not installed on {full} for this organisation")
        return self.app.installation_token(iid)
