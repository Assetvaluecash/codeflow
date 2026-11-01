"""Identity store (PL-001, PL-002): identity providers, users, browser sessions, service-account tokens, and the Principal every
request runs as. One instance serves one tenant; every query names tenant_id explicitly (row-level security is the second guard).

Secrets: a session cookie and a service token are 256-bit random values; only their SHA-256 is stored (they carry no password-like
low entropy to stretch). A token embeds its tenant id so the row can be found under row-level security without a global lookup.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg.types.json import Jsonb

from .db import Database
from .rbac import ROLES, valid_roles
from .scrub import scrub_text

TOKEN_PREFIX = "tia_"
SESSION_TTL = 8 * 3600
SESSION_IDLE = 30 * 60
LOGIN_STATE_TTL = 600
TOUCH_EVERY = 60


class AuthError(Exception):
    """Authentication or authorisation failed. `code` is stable and safe to return; `detail` goes to the audit log only."""

    def __init__(self, code: str, status: int = 401, detail: str = "", tenant: Optional[str] = None):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail
        self.tenant = tenant                    # set when the caller is a known identity of that tenant: the refusal is then audited there


class IdentityConfigError(ValueError):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    kind: str                                  # user | service | system
    subject: str
    roles: frozenset
    name: str = ""
    email: str = ""
    user_id: Optional[str] = None
    token_id: Optional[str] = None
    session_id: Optional[str] = None
    csrf: Optional[str] = None                 # set for cookie sessions only
    scm_logins: dict = field(default_factory=dict)
    repo_scope: object = "*"                   # service tokens: "*" or a tuple of repository names

    @property
    def actor(self) -> str:
        return f"{self.kind}:{self.subject}"[:96]

    def public(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "name": self.name, "email": self.email, "roles": sorted(self.roles),
                "tenant_id": self.tenant_id, **({"user_id": self.user_id} if self.user_id else {}),
                **({"token_id": self.token_id} if self.token_id else {})}


current_principal: contextvars.ContextVar = contextvars.ContextVar("tia_principal", default=None)


def dev_principal(tenant_id: str) -> Principal:
    """Authentication is not configured (development): everyone is an administrator. The server refuses to listen on a network
    address in this mode unless the operator says so explicitly."""
    return Principal(tenant_id, "system", "anonymous-development", frozenset(ROLES), "development (authentication off)")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------------------------------------------------
# identity-provider settings
# ------------------------------------------------------------------------------------------------------------------------------------
OIDC_KEYS = {"issuer", "audience", "discovery", "authorization_endpoint", "token_endpoint", "jwks_uri", "scopes", "subject_claim",
             "email_claim", "name_claim", "groups_claim", "scm_login_claims", "role_map", "default_role", "allowed_algs", "max_token_age"}
SAML_KEYS = {"entity_id", "sso_url", "certificates", "sp_entity_id", "name_id_format", "subject_attribute", "email_attribute",
             "name_attribute", "groups_attribute", "scm_login_attributes", "role_map", "default_role", "want_assertions_signed", "clock_skew"}
SECRET_LIKE = re.compile(r"secret|password|private|token", re.I)


def _dym(key, valid):
    import difflib
    m = difflib.get_close_matches(key, sorted(valid), n=1)
    return f" (did you mean '{m[0]}'?)" if m else ""


def _url(v, what, problems, https=True):
    from .netsafe import allow_private
    u = urllib.parse.urlsplit(v) if isinstance(v, str) else None
    if not u or u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.fragment:
        problems.append(f"{what}: must be an https URL without credentials")
    elif u.scheme == "http" and not allow_private():
        problems.append(f"{what}: must be https")


def parse_idp(protocol: str, cfg: dict) -> dict:
    """Strict validation, every problem at once; returns the normalised non-secret configuration."""
    problems: list[str] = []
    if protocol not in ("oidc", "saml"):
        raise IdentityConfigError([f"protocol must be 'oidc' or 'saml', not {protocol!r}"])
    if not isinstance(cfg, dict):
        raise IdentityConfigError(["configuration must be a JSON object"])
    valid = OIDC_KEYS if protocol == "oidc" else SAML_KEYS
    out: dict = {}
    for k, v in cfg.items():
        if k not in valid and SECRET_LIKE.search(k):
            problems.append(f"'{k}' looks like a secret; store secrets in the tenant secret store and reference them by name")
        elif k not in valid:
            problems.append(f"unknown setting '{k}'{_dym(k, valid)}")
        else:
            out[k] = v
    req = ("issuer", "audience") if protocol == "oidc" else ("sso_url", "entity_id", "certificates")
    for k in req:
        if not out.get(k):
            problems.append(f"'{k}' is required")
    if protocol == "oidc":
        if out.get("issuer"):
            _url(out["issuer"], "issuer", problems)
        explicit = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if out.get(k)]
        out["discovery"] = bool(out.get("discovery", not explicit))
        if not out["discovery"] and len(explicit) != 3:
            problems.append("without discovery, authorization_endpoint, token_endpoint and jwks_uri are all required")
        for k in explicit:
            _url(out[k], k, problems)
        if out.get("allowed_algs") is not None:
            from .jwtverify import ALGS
            a = out["allowed_algs"]
            if not isinstance(a, list) or not a or any(x not in ALGS for x in a):
                problems.append(f"allowed_algs must be a non-empty list of {sorted(ALGS)}")
        out.setdefault("scopes", "openid email profile")
        if not isinstance(out["scopes"], str) or "openid" not in out["scopes"].split():
            problems.append("scopes must be a string that includes 'openid'")
        for k in ("subject_claim", "email_claim", "name_claim", "groups_claim"):
            if k in out and out[k] is not None and not (isinstance(out[k], str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", out[k])):
                problems.append(f"{k} must be a claim name")
        out.setdefault("subject_claim", "sub")
        out.setdefault("email_claim", "email")
        out.setdefault("name_claim", "name")
        if out.get("max_token_age") is not None and not (isinstance(out["max_token_age"], int) and 60 <= out["max_token_age"] <= 86400):
            problems.append("max_token_age must be 60..86400 seconds")
    else:
        _url(out.get("sso_url"), "sso_url", problems)
        certs = out.get("certificates")
        if not (isinstance(certs, list) and certs and all(isinstance(c, str) and "BEGIN CERTIFICATE" in c for c in certs)):
            problems.append("certificates must be a list of PEM certificates (the identity provider's signing certificates)")
        out.setdefault("subject_attribute", None)
        out.setdefault("want_assertions_signed", True)
        if out["want_assertions_signed"] is not True:
            problems.append("want_assertions_signed cannot be turned off: unsigned assertions are never accepted")
        sk = out.get("clock_skew", 60)
        if not (isinstance(sk, int) and 0 <= sk <= 300):
            problems.append("clock_skew must be 0..300 seconds")
        out["clock_skew"] = sk
    rm = out.get("role_map", {})
    if not isinstance(rm, dict) or any(not isinstance(k, str) or not isinstance(v, (str, list)) for k, v in rm.items()):
        problems.append("role_map must map a group name to a role (or a list of roles)")
    else:
        for k, v in rm.items():
            try:
                valid_roles(v if isinstance(v, list) else [v])
            except ValueError as e:
                problems.append(f"role_map['{k}']: {e}")
    out["role_map"] = rm if isinstance(rm, dict) else {}
    dr = out.get("default_role")
    if dr is not None:
        try:
            valid_roles([dr])
        except ValueError as e:
            problems.append(f"default_role: {e}")
        if dr == "administrator":
            problems.append("default_role cannot be 'administrator': everyone who signs in would administer the tenant")
    key = "scm_login_claims" if protocol == "oidc" else "scm_login_attributes"
    slc = out.get(key)
    if slc is not None and not (isinstance(slc, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in slc.items())):
        problems.append(f"{key} must map a source-code host ('github') to the {'claim' if protocol == 'oidc' else 'attribute'} holding the login")
    if problems:
        raise IdentityConfigError(problems)
    return out


def claim_at(claims: dict, path: Optional[str]):
    """A claim by name or dotted path ('realm_access.roles'); a literal key containing dots wins."""
    if not path:
        return None
    if path in claims:
        return claims[path]
    cur = claims
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if isinstance(x, (str, int))]
    return [str(v)]


def roles_from_groups(cfg: dict, groups: list) -> list:
    out: list = []
    rm = cfg.get("role_map") or {}
    for g in groups:
        for r in (rm.get(g) if isinstance(rm.get(g), list) else [rm.get(g)] if rm.get(g) else []):
            if r in ROLES and r not in out:
                out.append(r)
    if not out and cfg.get("default_role") in ROLES:
        out.append(cfg["default_role"])
    return out


def identity_from_claims(cfg: dict, claims: dict) -> dict:
    """Subject, display fields, groups and source-host logins out of verified claims."""
    subject = claim_at(claims, cfg.get("subject_claim", "sub"))
    if not isinstance(subject, (str, int)) or not str(subject):
        raise AuthError("invalid_token", 401, "no subject claim")
    groups = as_list(claim_at(claims, cfg.get("groups_claim")))
    logins = {}
    for host, c in (cfg.get("scm_login_claims") or {}).items():
        v = claim_at(claims, c)
        if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,99}", v):
            logins[host] = v
    return {"subject": str(subject)[:200], "email": str(claim_at(claims, cfg.get("email_claim", "email")) or "")[:200],
            "name": str(claim_at(claims, cfg.get("name_claim", "name")) or "")[:200], "groups": groups[:200], "scm_logins": logins}


# ------------------------------------------------------------------------------------------------------------------------------------
# routing before a tenant is known (SECURITY DEFINER functions: routing facts only)
# ------------------------------------------------------------------------------------------------------------------------------------
def route_issuer(db: Database, issuer: str, audience: str) -> list[tuple[str, str]]:
    with db.plain() as c:
        return [(str(t), str(i)) for t, i in c.execute("select tenant_id, idp_id from tia_idp_by_issuer(%s, %s)", (issuer, audience))]


def login_options(db: Database, org: str) -> list[dict]:
    with db.plain() as c:
        rows = c.execute("select tenant_id, idp_id, idp_name, protocol from tia_login_options(%s)", (org,)).fetchall()
    return [{"tenant_id": str(t), "idp_id": str(i), "name": n, "protocol": p} for t, i, n, p in rows]


# ------------------------------------------------------------------------------------------------------------------------------------
class IdentityStore:
    def __init__(self, db: Database, tenant_id: str, session_ttl: int = SESSION_TTL, session_idle: int = SESSION_IDLE):
        self.db, self.tenant_id = db, str(tenant_id)
        self.session_ttl, self.session_idle = session_ttl, session_idle

    # ---- identity providers ---------------------------------------------------------------------------------------------------
    def idp_row(self, r) -> dict:
        return {"id": str(r[0]), "name": r[1], "protocol": r[2], "enabled": r[3], "config": r[4], "client_secret_ref": r[5],
                "created_by": r[6], "created_at": r[7].isoformat(timespec="seconds"), "updated_at": r[8].isoformat(timespec="seconds")}

    IDP_COLS = "id, name, protocol, enabled, config, client_secret_ref, created_by, created_at, updated_at"

    def list_idps(self) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            return [self.idp_row(r) for r in c.execute(f"select {self.IDP_COLS} from tenant_idps where tenant_id = %s order by name",
                                                       (self.tenant_id,))]

    def get_idp(self, idp_id: str) -> Optional[dict]:
        try:
            iid = str(uuid.UUID(str(idp_id)))
        except ValueError:
            return None
        with self.db.tx(self.tenant_id) as c:
            r = c.execute(f"select {self.IDP_COLS} from tenant_idps where tenant_id = %s and id = %s", (self.tenant_id, iid)).fetchone()
        return self.idp_row(r) if r else None

    def idp_by_name(self, name: str) -> Optional[dict]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute(f"select {self.IDP_COLS} from tenant_idps where tenant_id = %s and name = %s", (self.tenant_id, name)).fetchone()
        return self.idp_row(r) if r else None

    def put_idp(self, name: str, protocol: str, config: dict, client_secret_ref: Optional[str] = None, enabled: bool = True,
                actor: str = "api", idp_id: Optional[str] = None) -> dict:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", name or ""):
            raise IdentityConfigError(["name must be 1-64 letters, digits, space, '.', '_' or '-'"])
        if client_secret_ref is not None and not re.fullmatch(r"idp/[a-z0-9._-]{1,64}/client-secret", client_secret_ref):
            # any other name (github/token, another provider's secret) would be sent to the provider's token endpoint
            raise IdentityConfigError(["client_secret_ref must be 'idp/<name>/client-secret': a provider may reference only its own client secret"])
        norm = parse_idp(protocol, config)
        import psycopg.errors as pe
        with self.db.tx(self.tenant_id) as c:
            try:
                if idp_id:
                    r = c.execute(
                        "update tenant_idps set name=%s, protocol=%s, enabled=%s, config=%s, client_secret_ref=%s, updated_at=now() "
                        f"where tenant_id=%s and id=%s returning {self.IDP_COLS}",
                        (name, protocol, enabled, Jsonb(norm), client_secret_ref, self.tenant_id, idp_id)).fetchone()
                    if not r:
                        raise AuthError("not_found", 404)
                else:
                    r = c.execute(
                        "insert into tenant_idps (tenant_id, name, protocol, enabled, config, client_secret_ref, created_by) "
                        f"values (%s,%s,%s,%s,%s,%s,%s) returning {self.IDP_COLS}",
                        (self.tenant_id, name, protocol, enabled, Jsonb(norm), client_secret_ref, actor)).fetchone()
            except pe.UniqueViolation as e:
                if "issuer_audience" in str(e):
                    raise IdentityConfigError(["this issuer and audience combination is not available"])      # no oracle for other tenants' providers
                raise IdentityConfigError([f"an identity provider named '{name}' already exists"])
        return self.idp_row(r)

    def delete_idp(self, idp_id: str) -> bool:
        with self.db.tx(self.tenant_id) as c:
            n = c.execute("delete from tenant_idps where tenant_id = %s and id = %s", (self.tenant_id, idp_id)).rowcount
            if n:
                c.execute("update auth_sessions set revoked_at = now() where tenant_id = %s and idp_id = %s and revoked_at is null",
                          (self.tenant_id, idp_id))
        return bool(n)

    # ---- users ----------------------------------------------------------------------------------------------------------------
    USER_COLS = "id, idp_id, subject, email, display_name, roles, scm_logins, disabled, first_seen, last_login"

    @staticmethod
    def user_row(r) -> dict:
        return {"id": str(r[0]), "idp_id": str(r[1]) if r[1] else None, "subject": r[2], "email": r[3], "name": r[4], "roles": r[5],
                "scm_logins": r[6] or {}, "disabled": r[7], "first_seen": r[8].isoformat(timespec="seconds"),
                "last_login": r[9].isoformat(timespec="seconds") if r[9] else None}

    def upsert_user(self, idp_id: Optional[str], ident: dict, login: bool = True) -> dict:
        """Create the user on first sight (just-in-time) or refresh display fields. Roles an administrator assigned are never touched here;
        source-host logins the identity provider supplies are merged over, those an administrator set by hand are kept."""
        with self.db.tx(self.tenant_id) as c:
            c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 1))", (self.tenant_id + ident["subject"],))
            r = c.execute(f"select {self.USER_COLS} from tenant_users where tenant_id=%s and subject=%s and "
                          "coalesce(idp_id, '00000000-0000-0000-0000-000000000000'::uuid) = coalesce(%s::uuid, '00000000-0000-0000-0000-000000000000'::uuid)",
                          (self.tenant_id, ident["subject"], idp_id)).fetchone()
            if r is None:
                r = c.execute("insert into tenant_users (tenant_id, idp_id, subject, email, display_name, scm_logins, last_login) "
                              f"values (%s,%s,%s,%s,%s,%s, now()) returning {self.USER_COLS}",
                              (self.tenant_id, idp_id, ident["subject"], ident.get("email"), ident.get("name"),
                               Jsonb(ident.get("scm_logins") or {}))).fetchone()
            else:
                merged = {**(r[6] or {}), **(ident.get("scm_logins") or {})}
                r = c.execute("update tenant_users set email=%s, display_name=%s, scm_logins=%s, last_login=case when %s then now() else last_login end "
                              f"where tenant_id=%s and id=%s returning {self.USER_COLS}",
                              (ident.get("email"), ident.get("name"), Jsonb(merged), login, self.tenant_id, r[0])).fetchone()
        return self.user_row(r)

    def find_user(self, idp_id: Optional[str], subject: str) -> Optional[dict]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute(f"select {self.USER_COLS} from tenant_users where tenant_id=%s and subject=%s and "
                          "coalesce(idp_id, '00000000-0000-0000-0000-000000000000'::uuid) = coalesce(%s::uuid, '00000000-0000-0000-0000-000000000000'::uuid)",
                          (self.tenant_id, subject, idp_id)).fetchone()
        return self.user_row(r) if r else None

    def list_users(self, limit: int = 500) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            return [self.user_row(r) for r in c.execute(
                f"select {self.USER_COLS} from tenant_users where tenant_id = %s order by coalesce(email, subject) limit %s",
                (self.tenant_id, limit))]

    def get_user(self, user_id: str) -> Optional[dict]:
        try:
            uid = str(uuid.UUID(str(user_id)))
        except ValueError:
            return None
        with self.db.tx(self.tenant_id) as c:
            r = c.execute(f"select {self.USER_COLS} from tenant_users where tenant_id = %s and id = %s", (self.tenant_id, uid)).fetchone()
        return self.user_row(r) if r else None

    def update_user(self, user_id: str, roles=..., disabled=None, scm_logins=None) -> Optional[dict]:
        """An administrator's edit. `roles=None` clears the assignment (derive from the identity provider again). A change that can only
        reduce access (roles/disabled) takes effect at once: the user's sessions are revoked."""
        sets, args = [], []
        if roles is not ...:
            roles = None if roles is None else valid_roles(roles)
            sets.append("roles = %s")
            args.append(roles)
        if disabled is not None:
            sets.append("disabled = %s")
            args.append(bool(disabled))
        if scm_logins is not None:
            if not (isinstance(scm_logins, dict) and all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,99}", v)
                                                         for k, v in scm_logins.items())):
                raise IdentityConfigError(["scm_logins must map a host name to a login"])
            sets.append("scm_logins = %s")
            args.append(Jsonb(scm_logins))
        if not sets:
            return self.get_user(user_id)
        with self.db.tx(self.tenant_id) as c:
            r = c.execute(f"update tenant_users set {', '.join(sets)} where tenant_id = %s and id = %s returning {self.USER_COLS}",
                          (*args, self.tenant_id, user_id)).fetchone()
            if r and (roles is not ... or disabled):
                c.execute("update auth_sessions set revoked_at = now() where tenant_id = %s and user_id = %s and revoked_at is null",
                          (self.tenant_id, user_id))
        return self.user_row(r) if r else None

    @staticmethod
    def effective_roles(user: dict, idp_cfg: Optional[dict], groups: list) -> list:
        if user["roles"] is not None:
            return [r for r in user["roles"] if r in ROLES]
        return roles_from_groups(idp_cfg or {}, groups)

    # ---- sessions -------------------------------------------------------------------------------------------------------------
    def create_session(self, user: dict, idp_id: Optional[str], roles: list, groups: list) -> tuple[str, str]:
        """Returns (cookie value, session id). The cookie value is shown once; only its hash is stored."""
        secret = secrets.token_urlsafe(32)
        with self.db.tx(self.tenant_id) as c:
            sid = c.execute("insert into auth_sessions (tenant_id, secret_hash, user_id, idp_id, roles, groups, expires_at) "
                            "values (%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s)) returning id",
                            (self.tenant_id, sha(secret), user["id"], idp_id, roles, Jsonb(groups), self.session_ttl)).fetchone()[0]
            c.execute("delete from auth_sessions where tenant_id = %s and (expires_at < now() - interval '1 day' or revoked_at < now() - interval '1 day')",
                      (self.tenant_id,))
        return f"{uuid.UUID(self.tenant_id).hex}.{secret}", str(sid)

    def session_principal(self, secret: str) -> Principal:
        h = sha(secret)
        with self.db.tx(self.tenant_id) as c:
            r = c.execute(
                "select s.id, s.user_id, s.roles, s.idp_id, u.subject, u.email, u.display_name, u.scm_logins, u.disabled, u.roles, "
                "s.revoked_at is not null, s.expires_at < now(), s.last_seen_at < now() - make_interval(secs => %s), s.secret_hash, s.last_seen_at < now() - make_interval(secs => %s) "
                "from auth_sessions s join tenant_users u on u.tenant_id = s.tenant_id and u.id = s.user_id "
                "where s.tenant_id = %s and s.secret_hash = %s", (self.session_idle, TOUCH_EVERY, self.tenant_id, h)).fetchone()
            if r is None or not hmac.compare_digest(r[13], h):
                raise AuthError("no_session", 401, "unknown session")
            if r[10]:
                raise AuthError("session_revoked", 401, "session revoked")
            if r[11]:
                raise AuthError("session_expired", 401, "session expired")
            if r[12]:
                raise AuthError("session_expired", 401, "session idle timeout")
            if r[8]:
                raise AuthError("user_disabled", 403, "user disabled", self.tenant_id)
            if r[14]:
                c.execute("update auth_sessions set last_seen_at = now() where tenant_id = %s and id = %s", (self.tenant_id, r[0]))
        roles = r[9] if r[9] is not None else r[2]                  # an administrator's later assignment wins over the login-time mapping
        return Principal(self.tenant_id, "user", r[4], frozenset(x for x in roles if x in ROLES), r[6] or "", r[5] or "", str(r[1]),
                         session_id=str(r[0]), csrf=csrf_for(secret), scm_logins=r[7] or {})

    def revoke_session(self, session_id: str) -> bool:
        with self.db.tx(self.tenant_id) as c:
            return bool(c.execute("update auth_sessions set revoked_at = now() where tenant_id = %s and id = %s and revoked_at is null",
                                  (self.tenant_id, session_id)).rowcount)

    def list_sessions(self, limit: int = 200) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select s.id, s.user_id, coalesce(u.email, u.subject), s.roles, s.created_at, s.expires_at, s.last_seen_at, s.revoked_at "
                             "from auth_sessions s join tenant_users u on u.tenant_id = s.tenant_id and u.id = s.user_id "
                             "where s.tenant_id = %s and s.revoked_at is null and s.expires_at > now() order by s.created_at desc limit %s",
                             (self.tenant_id, limit)).fetchall()
        return [{"id": str(r[0]), "user_id": str(r[1]), "user": r[2], "roles": r[3], "created_at": r[4].isoformat(timespec="seconds"),
                 "expires_at": r[5].isoformat(timespec="seconds"), "last_seen_at": r[6].isoformat(timespec="seconds")} for r in rows]

    # ---- login state (OIDC authorisation-code flow with PKCE) -----------------------------------------------------------------------
    MAX_LIVE_STATES = 2000

    def save_login_state(self, idp_id: str, nonce: str, verifier: str, return_to: str, browser: str) -> str:
        """Sign-in can be started by anyone, so the number of pending sign-ins per tenant is capped."""
        state = secrets.token_urlsafe(32)
        with self.db.tx(self.tenant_id) as c:
            c.execute("delete from auth_login_states where tenant_id = %s and expires_at < now()", (self.tenant_id,))
            if c.execute("select count(*) from auth_login_states where tenant_id = %s", (self.tenant_id,)).fetchone()[0] >= self.MAX_LIVE_STATES:
                raise AuthError("too_many_logins", 429, "pending sign-ins at the cap")
            c.execute("insert into auth_login_states (tenant_id, state_hash, idp_id, nonce, code_verifier, return_to, browser_hash, expires_at) "
                      "values (%s,%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s))",
                      (self.tenant_id, sha(state), idp_id, nonce, verifier, return_to, sha(browser), LOGIN_STATE_TTL))
        return f"{uuid.UUID(self.tenant_id).hex}.{state}"

    def take_login_state(self, state: str, browser: str) -> Optional[dict]:
        """Single use: the row is deleted as it is read, so a replayed callback finds nothing. The callback must come from the browser
        that started the sign-in (login CSRF: an attacker cannot make a victim finish the attacker's sign-in)."""
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("delete from auth_login_states where tenant_id = %s and state_hash = %s returning idp_id, nonce, code_verifier, return_to, "
                          "expires_at > now(), browser_hash", (self.tenant_id, sha(state))).fetchone()
        if not r or not r[4] or not hmac.compare_digest(r[5] or "", sha(browser or "")):
            return None
        return {"idp_id": str(r[0]), "nonce": r[1], "verifier": r[2], "return_to": r[3]}

    def saml_assertion_once(self, assertion_id: str, not_after: datetime) -> bool:
        """True the first time an assertion id is presented; False for a replay."""
        with self.db.tx(self.tenant_id) as c:
            c.execute("delete from saml_assertions_seen where tenant_id = %s and expires_at < now()", (self.tenant_id,))
            return bool(c.execute("insert into saml_assertions_seen (tenant_id, assertion_id, expires_at) values (%s,%s,%s) "
                                  "on conflict do nothing", (self.tenant_id, assertion_id, not_after)).rowcount)

    # ---- service-account tokens -----------------------------------------------------------------------------------------------------
    def create_token(self, name: str, roles: list, repos="*", expires_in_days: Optional[int] = 90, created_by: str = "api",
                     allow_administrator: bool = False) -> dict:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", name or ""):
            raise IdentityConfigError(["name must be 1-64 letters, digits, space, '.', '_' or '-'"])
        try:
            roles = valid_roles(roles)
        except ValueError as e:
            raise IdentityConfigError([str(e)])
        if not roles:
            raise IdentityConfigError(["at least one role is required"])
        if "administrator" in roles and not allow_administrator:
            raise IdentityConfigError(["a service account cannot be an administrator (least privilege); grant the roles it needs"])
        if repos != "*" and not (isinstance(repos, list) and all(isinstance(x, str) for x in repos)):
            raise IdentityConfigError(['repos must be "*" or a list of repository names'])
        if expires_in_days is not None and not (isinstance(expires_in_days, int) and 1 <= expires_in_days <= 3650):
            raise IdentityConfigError(["expires_in_days must be 1..3650 (or null for no expiry)"])
        import psycopg.errors as pe
        tid, secret = uuid.uuid4(), secrets.token_urlsafe(32)
        try:
            with self.db.tx(self.tenant_id) as c:
                r = c.execute("insert into service_tokens (tenant_id, id, name, roles, repos, secret_hash, created_by, expires_at) "
                              "values (%s,%s,%s,%s,%s,%s,%s, case when %s::int is null then null else now() + make_interval(days => %s::int) end) "
                              "returning expires_at", (self.tenant_id, tid, name, roles, Jsonb(repos), sha(secret), created_by,
                                                       expires_in_days, expires_in_days)).fetchone()
        except pe.UniqueViolation:
            raise IdentityConfigError([f"a token named '{name}' already exists"])
        return {"id": str(tid), "name": name, "roles": roles, "repos": repos, "expires_at": r[0].isoformat(timespec="seconds") if r[0] else None,
                "token": f"{TOKEN_PREFIX}{uuid.UUID(self.tenant_id).hex}_{tid.hex}_{secret}"}

    def list_tokens(self) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select id, name, roles, repos, created_by, created_at, expires_at, revoked_at, last_used_at from service_tokens "
                             "where tenant_id = %s order by created_at desc", (self.tenant_id,)).fetchall()
        f = lambda d: d.isoformat(timespec="seconds") if d else None
        return [{"id": str(r[0]), "name": r[1], "roles": r[2], "repos": r[3], "created_by": r[4], "created_at": f(r[5]), "expires_at": f(r[6]),
                 "revoked_at": f(r[7]), "last_used_at": f(r[8])} for r in rows]

    def revoke_token(self, token_id: str) -> bool:
        with self.db.tx(self.tenant_id) as c:
            return bool(c.execute("update service_tokens set revoked_at = now() where tenant_id = %s and id = %s and revoked_at is null",
                                  (self.tenant_id, token_id)).rowcount)

    def token_principal(self, token_id: str, secret: str) -> Principal:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select name, roles, repos, secret_hash, revoked_at is not null, expires_at is not null and expires_at < now(), "
                          "last_used_at is null or last_used_at < now() - make_interval(secs => %s) from service_tokens "
                          "where tenant_id = %s and id = %s", (TOUCH_EVERY, self.tenant_id, token_id)).fetchone()
            ok = r is not None and hmac.compare_digest(r[3], sha(secret))
            if not ok:
                raise AuthError("invalid_token", 401, "unknown token or wrong secret")
            if r[4]:
                raise AuthError("invalid_token", 401, "token revoked")
            if r[5]:
                raise AuthError("invalid_token", 401, "token expired")
            if r[6]:
                c.execute("update service_tokens set last_used_at = now() where tenant_id = %s and id = %s", (self.tenant_id, token_id))
        scope = "*" if r[2] == "*" else tuple(r[2])
        return Principal(self.tenant_id, "service", r[0], frozenset(x for x in r[1] if x in ROLES), r[0], token_id=token_id, repo_scope=scope)


def csrf_for(session_secret: str) -> str:
    return hashlib.sha256(("tia-csrf|" + session_secret).encode()).hexdigest()


def parse_service_token(tok: str) -> Optional[tuple[str, str, str]]:
    """(tenant id, token id, secret) from 'tia_<tenant hex>_<token hex>_<secret>', else None."""
    m = re.fullmatch(r"tia_([0-9a-f]{32})_([0-9a-f]{32})_([A-Za-z0-9_-]{20,100})", tok or "")
    if not m:
        return None
    return str(uuid.UUID(m.group(1))), str(uuid.UUID(m.group(2))), m.group(3)


def parse_session_cookie(v: str) -> Optional[tuple[str, str]]:
    m = re.fullmatch(r"([0-9a-f]{32})\.([A-Za-z0-9_-]{20,100})", v or "")
    return (str(uuid.UUID(m.group(1))), m.group(2)) if m else None


def safe_return_to(v: Optional[str]) -> str:
    """Where to go after sign-in: a path on this site only (no scheme, no host, no backslash tricks)."""
    if not v or not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(ch) < 32 for ch in v) or len(v) > 500:
        return "/"
    return v
