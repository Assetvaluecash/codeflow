"""Authentication, authorisation and the sign-in/administration endpoints (PL-001, PL-002).

Every request is resolved to a Principal before it reaches a handler:
  Authorization: Bearer tia_...   a service-account token (scoped roles and repositories, optional expiry, revocable)
  Authorization: Bearer <JWT>     an OpenID Connect token from a tenant's identity provider (signature, issuer, audience, expiry checked)
  Cookie tia_session              a browser session created by the OIDC or SAML sign-in
The principal carries the TENANT: the request then runs against that tenant's data, workspace and quota, so one deployment serves
many tenants. A principal's roles must grant the permission rbac.ROUTES lists for the route; a route that is not listed is refused.
Cookie sessions need the CSRF header on every state-changing call; bearer tokens, which no browser attaches by itself, do not.

When authentication is not configured (development) everyone is a development administrator on the deployment's one tenant.
"""
from __future__ import annotations

import contextvars
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.routing import Match, Mount

from . import jwtverify, rbac
from .identity import (roles_from_groups, AuthError, IdentityConfigError, IdentityStore, Principal, current_principal, csrf_for, dev_principal, identity_from_claims,
                       login_options, parse_service_token, parse_session_cookie, route_issuer, safe_return_to)
from .oidc import OidcError, ProviderCache, pkce_pair
from .rbac import PUBLIC, AUTHENTICATED

SESSION_COOKIE = "tia_session"
LOGIN_COOKIE = "tia_login"
current_tenant: contextvars.ContextVar = contextvars.ContextVar("tia_tenant", default=None)
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class AuthSettings:
    required: bool = False
    public_url: str = ""
    session_ttl: int = 8 * 3600
    session_idle: int = 30 * 60

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @staticmethod
    def from_env() -> "AuthSettings":
        import os
        e = os.environ
        return AuthSettings(required=e.get("TIA_AUTH_REQUIRED", "").lower() in ("1", "true", "yes"), public_url=(e.get("TIA_PUBLIC_URL") or "").rstrip("/"),
                            session_ttl=int(e.get("TIA_SESSION_TTL_S", 8 * 3600)), session_idle=int(e.get("TIA_SESSION_IDLE_S", 30 * 60)))


class _TTL:
    def __init__(self, ttl: float):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, k):
        with self._lock:
            v = self._d.get(k)
            if v and time.monotonic() - v[0] < self.ttl:
                return v[1]
            self._d.pop(k, None)

    def put(self, k, v):
        with self._lock:
            if len(self._d) > 5000:
                self._d.clear()
            self._d[k] = (time.monotonic(), v)

    def clear(self):
        with self._lock:
            self._d.clear()


class Auth:
    def __init__(self, db, default_tenant: str, settings: AuthSettings, ctx_for: Callable, secrets_for: Callable[[str], object] = lambda t: None,
                 http_get=None, http_post=None):
        self.db, self.default_tenant, self.settings, self.ctx_for, self.secrets_for = db, str(default_tenant), settings, ctx_for, secrets_for
        self.providers = ProviderCache()
        self._ids: dict = {}
        self._http = {k: v for k, v in (("http_get", http_get), ("http_post", http_post)) if v}
        self._route, self._idp, self._user = _TTL(30), _TTL(30), _TTL(30)

    def ids(self, tenant: str) -> IdentityStore:
        s = self._ids.get(tenant)
        if s is None:
            if len(self._ids) > 512:
                self._ids.clear()                                           # cheap objects; a flood of forged tenant ids cannot grow this
            s = self._ids[tenant] = IdentityStore(self.db, tenant, self.settings.session_ttl, self.settings.session_idle)
        return s

    def forget(self, tenant: str):
        self._idp.clear()
        self._user.clear()
        self._route.clear()

    def provider(self, tenant: str, idp: dict):
        def secret():
            ss = self.secrets_for(tenant)
            v = ss.get_text(idp["client_secret_ref"]) if ss else None
            if idp.get("client_secret_ref") and not v:
                raise OidcError("idp_misconfigured", 502, f"client secret '{idp['client_secret_ref']}' is not in the secret store")
            return v
        return self.providers.get(idp, secret, **self._http)

    def idp_cached(self, tenant: str, idp_id: str) -> Optional[dict]:
        k = (tenant, idp_id)
        v = self._idp.get(k)
        if v is None:
            v = self.ids(tenant).get_idp(idp_id)
            if v:
                self._idp.put(k, v)
        return v

    # ---- who is calling ---------------------------------------------------------------------------------------------------
    def authenticate(self, request: Request) -> Principal:
        h = request.headers.get("authorization")
        if h:
            scheme, _, cred = h.partition(" ")
            cred = cred.strip()
            if scheme.lower() != "bearer" or not cred:
                raise AuthError("invalid_token", 401, "unsupported Authorization header")
            return self.from_token(cred)
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            parsed = parse_session_cookie(cookie)
            if not parsed:
                raise AuthError("no_session", 401, "malformed session cookie")
            return self.ids(parsed[0]).session_principal(parsed[1])
        raise AuthError("authentication_required", 401)

    def from_token(self, cred: str) -> Principal:
        if cred.startswith("tia_"):
            parsed = parse_service_token(cred)
            if not parsed:
                raise AuthError("invalid_token", 401, "malformed service token")
            return self.ids(parsed[0]).token_principal(parsed[1], parsed[2])
        try:
            claims = jwtverify.peek(cred)
        except jwtverify.TokenError as e:
            raise AuthError("invalid_token", 401, str(e))
        iss = claims.get("iss")
        aud = claims.get("aud")
        auds = [a for a in (aud if isinstance(aud, list) else [aud]) if isinstance(a, str)]
        if not isinstance(iss, str) or not auds:
            raise AuthError("invalid_token", 401, "token has no issuer or audience")
        route = None
        for a in auds[:5]:
            k = (iss, a)
            r = self._route.get(k)
            if r is None:
                r = route_issuer(self.db, iss, a)
                self._route.put(k, r)
            if r:
                route = r[0]
                break
        if route is None:
            raise AuthError("invalid_token", 401, "no tenant trusts this issuer and audience")
        tenant, idp_id = route
        idp = self.idp_cached(tenant, idp_id)
        if not idp or not idp["enabled"] or idp["protocol"] != "oidc":
            raise AuthError("invalid_token", 401, "identity provider is not available")
        verified = self.provider(tenant, idp).verify(cred)
        return self.principal_from_claims(tenant, idp, verified)

    def principal_from_claims(self, tenant: str, idp: dict, claims: dict, login: bool = False, session_secret: Optional[str] = None) -> Principal:
        ident = identity_from_claims(idp["config"], claims)
        ids = self.ids(tenant)
        key = (tenant, idp["id"], ident["subject"])
        user = None if login else self._user.get(key)
        if user is None:
            existing = ids.find_user(idp["id"], ident["subject"])
            if existing is None and not roles_from_groups(idp["config"], ident["groups"]):
                raise AuthError("no_role", 403, f"user {ident['subject']} has no role in this tenant", tenant)     # no row for someone who gets nothing
            user = ids.upsert_user(idp["id"], ident, login=login)
            self._user.put(key, user)
        if user["disabled"]:
            raise AuthError("user_disabled", 403, f"user {ident['subject']} is disabled", tenant)
        roles = ids.effective_roles(user, idp["config"], ident["groups"])
        if not roles:
            raise AuthError("no_role", 403, f"user {ident['subject']} has no role in this tenant", tenant)
        return Principal(tenant, "user", ident["subject"], frozenset(roles), user["name"] or ident["name"], user["email"] or ident["email"], user["id"],
                         scm_logins=user["scm_logins"], csrf=csrf_for(session_secret) if session_secret else None)

    # ---- authorisation ----------------------------------------------------------------------------------------------------------
    def check_csrf(self, request: Request, p: Principal):
        """Cookie sessions only (nothing else is attached by the browser on its own): a state-changing call must carry the CSRF value the
        page got from /auth/me, and, when the browser says where the request came from, it must be this site."""
        if p.csrf is None or request.method not in UNSAFE:
            return
        if not hmac.compare_digest(request.headers.get("x-tia-csrf", ""), p.csrf):
            raise AuthError("csrf", 403, "missing or wrong CSRF header")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            if not (self.settings.public_url and origin.rstrip("/") == self.settings.public_url):
                raise AuthError("csrf", 403, f"cross-origin request from {origin}")


async def read_capped(request: Request, limit: int) -> bytes:
    """The request body, refusing to read more than `limit` bytes (unauthenticated endpoints must not buffer unbounded input)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise AuthError("login_failed", 413, "request body too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise AuthError("login_failed", 413, "request body too large")
    return bytes(buf)


def route_of(app: FastAPI, request: Request):
    """(template, is_static) of the route this request will hit; (None, False) when nothing matches (the router will answer 404/405)."""
    scope = request.scope
    partial = None
    for r in app.router.routes:
        m, _ = r.matches(scope)
        if m == Match.FULL:
            return (getattr(r, "path", None), isinstance(r, Mount))
        if m == Match.PARTIAL and partial is None:
            partial = r
    return (getattr(partial, "path", None), False) if partial else (None, False)


# ------------------------------------------------------------------------------------------------------------------------------------
class IdpIn(BaseModel):
    name: str
    protocol: str = "oidc"
    config: dict
    enabled: bool = True
    client_secret_ref: Optional[str] = None
    client_secret: Optional[str] = None             # write-only: stored encrypted under the name idp/<name>/client-secret


class UserIn(BaseModel):
    roles: Optional[list[str]] = None               # present and null clears the assignment (roles follow the identity provider's groups again)
    disabled: Optional[bool] = None
    scm_logins: Optional[dict] = None


class TokenIn(BaseModel):
    name: str
    roles: list[str]
    repos: object = "*"
    expires_in_days: Optional[int] = 90


def install(app: FastAPI, auth: Auth, set_tenant: Callable[[str], None]) -> None:
    """The authentication middleware and the /auth, /api/v1/me and /api/v1/admin routes. Call after the tenant-lifecycle middleware has been
    added and before the observability/transport middleware, so the principal is known when the tenant's status is checked and every
    request, refused ones included, is still logged."""
    s = auth.settings

    def fail(e: AuthError, request: Request, store=None):
        if store is not None and e.code in ("forbidden", "csrf", "user_disabled", "no_role"):
            try:
                store.audit("access_denied", code=e.code, method=request.method, path=request.url.path, detail=e.detail[:200])
            except Exception:
                pass
        headers = {"X-TIA-Code": e.code}
        if e.status == 401:
            headers["WWW-Authenticate"] = 'Bearer realm="tia"'
        if e.status == 401 and request.method == "GET" and "text/html" in request.headers.get("accept", "") and request.url.path.startswith("/analyses/"):
            return RedirectResponse("/?return_to=" + safe_return_to(request.url.path), status_code=302)
        return JSONResponse({"detail": _PUBLIC_MESSAGE.get(e.code, "request refused"), "code": e.code}, status_code=e.status, headers=headers)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        resp = await resolve(request, call_next)
        if s.required:                                       # browser hardening for every response; a handler's own, stricter value wins
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
            if request.url.path.startswith(("/api/", "/auth/", "/analyses/")):
                resp.headers.setdefault("Cache-Control", "no-store")     # nothing a signed-in person sees is kept by a shared browser cache
        return resp

    async def resolve(request: Request, call_next):
        if not s.required:
            current_principal.set(dev_principal(auth.default_tenant))
            set_tenant(auth.default_tenant)
            return await call_next(request)
        template, static = route_of(app, request)
        perm = rbac.classify(request.method, template) if template else None
        if static or template is None or perm == PUBLIC or (template in rbac.UNLISTED_OK):
            set_tenant(auth.default_tenant)
            current_principal.set(None)
            return await call_next(request)
        if perm is None:
            return JSONResponse({"detail": "route is not classified; refused", "code": "forbidden"}, status_code=403)
        try:
            p = await run_in_threadpool(auth.authenticate, request)
        except AuthError as e:
            return fail(e, request, auth.ctx_for(e.tenant).store if e.tenant else None)
        except Exception as e:                                          # a provider outage must not read as "unauthenticated server error"
            from .netsafe import FetchError
            return JSONResponse({"detail": "authentication is temporarily unavailable", "code": "auth_unavailable"}, status_code=503) \
                if isinstance(e, FetchError) else _reraise(e)
        set_tenant(p.tenant_id)
        current_principal.set(p)
        ctx = auth.ctx_for(p.tenant_id)
        try:
            auth.check_csrf(request, p)
            if perm != AUTHENTICATED and not rbac.allows(p.roles, perm):
                raise AuthError("forbidden", 403, f"{p.actor} lacks '{perm}' for {request.method} {template}")
        except AuthError as e:
            return fail(e, request, ctx.store)
        return await call_next(request)

    def _reraise(e):
        raise e

    # ---- sign-in ----------------------------------------------------------------------------------------------------------------
    def need_public_url():
        if not s.public_url:
            raise AuthError("idp_misconfigured", 503, "TIA_PUBLIC_URL is not set: browser sign-in needs the address users reach TIA at")

    @app.get("/auth/options", include_in_schema=False)
    def options(org: str = ""):
        """The sign-in methods of an organisation. Unknown, inactive and offboarded organisations look the same: an empty list."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", org or ""):
            return {"providers": []}
        return {"providers": [{"name": o["name"], "protocol": o["protocol"]} for o in login_options(auth.db, org)]}

    @app.get("/auth/login", include_in_schema=False)
    def login(request: Request, org: str, idp: str = "", return_to: str = "/"):
        try:
            need_public_url()
            opts = login_options(auth.db, org) if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", org or "") else []
            pick = [o for o in opts if not idp or o["name"] == idp]
            if not pick:
                raise AuthError("unknown_organisation", 404, "no such organisation or sign-in method")
            o = pick[0]
            ids = auth.ids(o["tenant_id"])
            row = ids.get_idp(o["idp_id"])
            browser = secrets.token_urlsafe(24)
            if o["protocol"] == "saml":
                from . import saml
                resp = saml.start_login(auth, o["tenant_id"], row, browser, safe_return_to(return_to))
            else:
                prov = auth.provider(o["tenant_id"], row)
                verifier, challenge = pkce_pair()
                nonce = secrets.token_urlsafe(24)
                state = ids.save_login_state(row["id"], nonce, verifier, safe_return_to(return_to), browser)
                resp = RedirectResponse(prov.authorization_url(s.public_url + "/auth/callback", state, nonce, challenge), status_code=302)
            # SAML returns by a cross-site POST, on which a Lax cookie is not sent; OIDC returns by a top-level GET, where Lax is enough
            resp.set_cookie(LOGIN_COOKIE, browser, max_age=600, httponly=True, secure=s.secure_cookies,
                            samesite="none" if o["protocol"] == "saml" else "lax", path="/auth")
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except AuthError as e:
            return _login_error(e)

    def _login_error(e: AuthError, store=None):
        if store is not None:
            try:
                store.audit("login_failed", code=e.code, detail=e.detail[:200])
            except Exception:
                pass
        return HTMLResponse(_ERROR_PAGE.format(code=e.code, msg=_PUBLIC_MESSAGE.get(e.code, "Sign-in failed.")), status_code=e.status,
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    def start_session(tenant: str, p: Principal, idp_id: Optional[str], groups: list, return_to: str, how: str):
        ids = auth.ids(tenant)
        cookie, sid = ids.create_session({"id": p.user_id}, idp_id, sorted(p.roles), groups)
        auth.ctx_for(tenant).store.audit("login_succeeded", method=how, subject=p.subject, roles=sorted(p.roles), session=sid, actor=p.actor)
        resp = RedirectResponse(safe_return_to(return_to), status_code=302)
        resp.set_cookie(SESSION_COOKIE, cookie, max_age=s.session_ttl, httponly=True, secure=s.secure_cookies, samesite="lax", path="/")
        resp.delete_cookie(LOGIN_COOKIE, path="/auth")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/auth/callback", include_in_schema=False)
    def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        tenant = None
        confirmed = False
        try:
            m = re.fullmatch(r"([0-9a-f]{32})\.([A-Za-z0-9_-]{20,100})", state or "")
            if not m:
                raise AuthError("login_failed", 400, "malformed state")
            import uuid
            tenant = str(uuid.UUID(m.group(1)))
            ids = auth.ids(tenant)
            st = ids.take_login_state(m.group(2), request.cookies.get(LOGIN_COOKIE, ""))
            if st is None:
                raise AuthError("login_failed", 400, "unknown, expired, replayed or foreign state")
            confirmed = True
            if error or not code:
                raise AuthError("login_failed", 401, f"identity provider returned error '{error[:60]}'")
            idp = auth.idp_cached(tenant, st["idp_id"]) or ids.get_idp(st["idp_id"])
            if not idp or not idp["enabled"] or idp["protocol"] != "oidc":
                raise AuthError("login_failed", 400, "identity provider is not available")
            prov = auth.provider(tenant, idp)
            id_token = prov.exchange(code, s.public_url + "/auth/callback", st["verifier"])
            claims = prov.verify(id_token, nonce=st["nonce"])
            p = auth.principal_from_claims(tenant, idp, claims, login=True)
            ident = identity_from_claims(idp["config"], claims)
            return start_session(tenant, p, idp["id"], ident["groups"], st["return_to"], "oidc")
        except AuthError as e:
            return _login_error(e, auth.ctx_for(tenant).store if tenant and confirmed else None)

    @app.post("/auth/saml/acs", include_in_schema=False)
    async def saml_acs(request: Request):
        """The identity provider's POST (via the user's browser): the signed assertion that proves who signed in."""
        try:
            raw = await read_capped(request, 1_500_000)
            import urllib.parse as up
            form = dict(up.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=10))
            return await run_in_threadpool(saml_login, request, form.get("SAMLResponse", ""), form.get("RelayState", ""))
        except AuthError as e:
            return _login_error(e)

    def saml_login(request: Request, response_b64: str, relay: str):
        from . import saml
        import uuid
        tenant = None
        confirmed = False
        try:
            m = re.fullmatch(r"([0-9a-f]{32})\.([A-Za-z0-9_-]{20,100})", relay or "")
            if not m:
                raise AuthError("login_failed", 400, "malformed RelayState")
            tenant = str(uuid.UUID(m.group(1)))
            ids = auth.ids(tenant)
            st = ids.take_login_state(m.group(2), request.cookies.get(LOGIN_COOKIE, ""))
            if st is None:
                raise AuthError("login_failed", 400, "unknown, expired, replayed or foreign RelayState")
            confirmed = True
            idp = auth.idp_cached(tenant, st["idp_id"]) or ids.get_idp(st["idp_id"])
            if not idp or not idp["enabled"] or idp["protocol"] != "saml":
                raise AuthError("login_failed", 400, "identity provider is not available")
            result = saml.validate(response_b64, cfg=idp["config"], public_url=s.public_url, request_id=st["nonce"])
            if not ids.saml_assertion_once(result["assertion_id"], result["not_after"] + timedelta(minutes=5)):
                raise AuthError("login_failed", 401, "assertion replayed")
            claims = saml.claims_from(idp["config"], result)
            view = {"id": idp["id"], "config": saml.claim_config(idp["config"])}
            p = auth.principal_from_claims(tenant, view, claims, login=True)
            ident = identity_from_claims(view["config"], claims)
            return start_session(tenant, p, idp["id"], ident["groups"], st["return_to"], "saml")
        except AuthError as e:
            return _login_error(e, auth.ctx_for(tenant).store if tenant and confirmed else None)

    @app.get("/auth/saml/metadata", include_in_schema=False)
    def saml_metadata():
        from . import saml
        need_public_url()
        return Response(saml.metadata(s.public_url, {}), media_type="application/samlmetadata+xml")

    @app.post("/auth/logout")
    def logout(request: Request):
        p = current_principal.get()
        if p and p.session_id:
            auth.ids(p.tenant_id).revoke_session(p.session_id)
            auth.ctx_for(p.tenant_id).store.audit("logout", session=p.session_id)
        resp = JSONResponse({"signed_out": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/auth/me", include_in_schema=False)
    def me(request: Request):
        """Who am I? Never 401: the page asks this first to decide between the application and the sign-in screen."""
        if not s.required:
            return {"authenticated": True, "auth": "none", "principal": dev_principal(auth.default_tenant).public(),
                    "permissions": sorted(rbac.PERMISSIONS), "csrf": None}
        try:
            p = auth.authenticate(request)
        except AuthError as e:
            return JSONResponse({"authenticated": False, "auth": "required", "reason": e.code}, headers={"Cache-Control": "no-store"})
        except Exception:
            return JSONResponse({"authenticated": False, "auth": "required", "reason": "auth_unavailable"}, status_code=503)
        return JSONResponse({"authenticated": True, "auth": "required", "principal": p.public(), "csrf": p.csrf,
                             "permissions": sorted(k for k, v in rbac.PERMISSIONS.items() if p.roles & v)}, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/me")
    def api_me():
        p = current_principal.get()
        return {**p.public(), "permissions": sorted(k for k, v in rbac.PERMISSIONS.items() if p.roles & v)}

    # ---- administration ----------------------------------------------------------------------------------------------------------
    def me_ids() -> tuple[IdentityStore, object]:
        p = current_principal.get()
        return auth.ids(p.tenant_id), auth.ctx_for(p.tenant_id)

    def cfg_error(e: IdentityConfigError):
        return JSONResponse({"detail": {"message": "invalid configuration", "problems": e.problems}}, status_code=422)

    @app.get("/api/v1/admin/idps")
    def idps_list():
        ids, _ = me_ids()
        return [{**i, "client_secret_ref": i["client_secret_ref"]} for i in ids.list_idps()]

    def save_idp(body: IdpIn, idp_id: Optional[str]):
        ids, ctx = me_ids()
        ref = body.client_secret_ref
        if idp_id is not None and not _uuid(idp_id):
            return JSONResponse({"detail": "unknown identity provider"}, status_code=404)
        try:
            if body.client_secret:
                ss = auth.secrets_for(ids.tenant_id)
                if ss is None:
                    return JSONResponse({"detail": "no secret store is configured (TIA_MASTER_KEY); cannot keep a client secret"}, status_code=400)
                ref = "idp/" + re.sub(r"[^a-z0-9._-]", "-", body.name.lower()) + "/client-secret"
                ss.put(ref, body.client_secret)
            row = ids.put_idp(body.name, body.protocol, body.config, ref, body.enabled, actor=current_principal.get().actor, idp_id=idp_id)
        except IdentityConfigError as e:
            return cfg_error(e)
        except AuthError as e:
            if e.code == "not_found":
                return JSONResponse({"detail": "unknown identity provider"}, status_code=404)
            raise
        auth.forget(ids.tenant_id)
        ctx.store.audit("idp_saved", idp=row["id"], name=row["name"], protocol=row["protocol"], enabled=row["enabled"], created=idp_id is None)
        return row

    @app.post("/api/v1/admin/idps")
    def idps_create(body: IdpIn):
        return save_idp(body, None)

    @app.put("/api/v1/admin/idps/{iid}")
    def idps_update(iid: str, body: IdpIn):
        return save_idp(body, iid)

    @app.delete("/api/v1/admin/idps/{iid}")
    def idps_delete(iid: str):
        ids, ctx = me_ids()
        if not _uuid(iid) or not ids.delete_idp(iid):
            return JSONResponse({"detail": "unknown identity provider"}, status_code=404)
        auth.forget(ids.tenant_id)
        ctx.store.audit("idp_deleted", idp=iid)
        return {"deleted": iid}

    @app.get("/api/v1/admin/users")
    def users_list():
        return me_ids()[0].list_users()

    @app.put("/api/v1/admin/users/{uid}")
    def users_update(uid: str, body: UserIn):
        ids, ctx = me_ids()
        if not _uuid(uid):
            return JSONResponse({"detail": "unknown user"}, status_code=404)
        try:
            u = ids.update_user(uid, roles=body.roles if "roles" in body.model_fields_set else ..., disabled=body.disabled, scm_logins=body.scm_logins)
        except (IdentityConfigError, ValueError) as e:
            return cfg_error(e if isinstance(e, IdentityConfigError) else IdentityConfigError([str(e)]))
        if u is None:
            return JSONResponse({"detail": "unknown user"}, status_code=404)
        auth.forget(ids.tenant_id)
        ctx.store.audit("user_updated", user=uid, roles=u["roles"] if "roles" in body.model_fields_set else "unchanged", disabled=u["disabled"],
                        scm_logins_changed=body.scm_logins is not None)
        return u

    @app.get("/api/v1/admin/tokens")
    def tokens_list():
        return me_ids()[0].list_tokens()

    @app.post("/api/v1/admin/tokens")
    def tokens_create(body: TokenIn):
        ids, ctx = me_ids()
        try:
            t = ids.create_token(body.name, body.roles, body.repos, body.expires_in_days, created_by=current_principal.get().actor)
        except IdentityConfigError as e:
            return cfg_error(e)
        ctx.store.audit("service_token_created", token_id=t["id"], name=t["name"], roles=t["roles"], repos=t["repos"], expires_at=t["expires_at"])
        return JSONResponse(t, status_code=201, headers={"Cache-Control": "no-store"})

    @app.delete("/api/v1/admin/tokens/{tid}")
    def tokens_revoke(tid: str):
        ids, ctx = me_ids()
        if not _uuid(tid) or not ids.revoke_token(tid):
            return JSONResponse({"detail": "unknown or already revoked token"}, status_code=404)
        ctx.store.audit("service_token_revoked", token_id=tid)
        return {"revoked": tid}

    @app.get("/api/v1/admin/sessions")
    def sessions_list():
        return me_ids()[0].list_sessions()

    @app.delete("/api/v1/admin/sessions/{sid}")
    def sessions_revoke(sid: str):
        ids, ctx = me_ids()
        if not _uuid(sid) or not ids.revoke_session(sid):
            return JSONResponse({"detail": "unknown or already revoked session"}, status_code=404)
        ctx.store.audit("session_revoked", session=sid)
        return {"revoked": sid}


def _uuid(v: str) -> bool:
    try:
        import uuid
        uuid.UUID(v)
        return True
    except ValueError:
        return False


_PUBLIC_MESSAGE = {
    "authentication_required": "Authentication is required.", "invalid_token": "The credentials are not valid.",
    "no_session": "Sign in to continue.", "session_expired": "Your session has expired. Sign in again.",
    "session_revoked": "Your session was ended. Sign in again.", "user_disabled": "This account is disabled.",
    "no_role": "Your account has no role in this organisation. Ask an administrator.", "forbidden": "You do not have permission to do this.",
    "csrf": "The request was refused (cross-site protection).", "login_failed": "Sign-in failed. Start again.",
    "unknown_organisation": "No such organisation or sign-in method.", "idp_unreachable": "The identity provider could not be reached.",
    "idp_misconfigured": "Sign-in is not configured correctly. Contact an administrator.", "too_many_logins": "Too many sign-ins in progress. Try again shortly.",
    "auth_unavailable": "Authentication is temporarily unavailable.",
}
_ERROR_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Sign-in</title>"
               "<style>body{{font:16px system-ui;margin:12vh auto;max-width:30rem;padding:0 1rem}}</style>"
               "<h1>Sign-in</h1><p>{msg}</p><p><a href='/'>Back to TIA</a></p><small>{code}</small>")
