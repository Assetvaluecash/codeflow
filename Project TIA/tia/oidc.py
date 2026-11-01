"""OpenID Connect (PL-001): browser sign-in by the authorization-code flow with PKCE, and bearer-token verification for API clients.

Sign-in: state (single use, stored server-side), nonce and PKCE verifier are created at /auth/login; /auth/callback exchanges the
code at the token endpoint (client secret from the tenant's encrypted secret store when the provider is a confidential client),
verifies the ID token (signature from the provider's published keys, issuer, audience, expiry, nonce) and only then creates a session.
All provider endpoints are fetched through netsafe (https, public addresses, no redirects, size cap).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
from typing import Callable, Optional

from . import jwtverify, netsafe
from .identity import AuthError


class OidcError(AuthError):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class Provider:
    """One identity provider as configured by a tenant (non-secret settings + an optional client secret)."""

    DISCOVERY_TTL = 3600.0

    def __init__(self, cfg: dict, client_secret: Optional[str] = None, http_get: Optional[Callable] = None,
                 http_post: Optional[Callable] = None):
        self.cfg, self.client_secret = cfg, client_secret
        self.created = time.monotonic()
        self._get, self._post = http_get or netsafe.get_json, http_post or netsafe.post_form
        self._disc: Optional[dict] = None
        self._disc_error = None
        self._disc_at = 0.0
        self._lock = threading.Lock()
        self.jwks = jwtverify.Jwks(lambda: self._get(self.endpoint("jwks_uri")))

    @property
    def issuer(self) -> str:
        return self.cfg["issuer"]

    @property
    def audience(self) -> str:
        return self.cfg["audience"]

    def endpoint(self, name: str) -> str:
        if not self.cfg.get("discovery", True):
            return self.cfg[name]
        with self._lock:
            if self._disc_error and time.monotonic() - self._disc_error[0] < 30:
                raise self._disc_error[1]                                   # a failing provider is not retried on every request
            if self._disc is None or time.monotonic() - self._disc_at > self.DISCOVERY_TTL:
                url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
                try:
                    doc = self._get(url)
                    if doc.get("issuer") != self.issuer:                     # a mismatch would let another provider vouch for this one
                        raise OidcError("idp_misconfigured", 502, f"discovery document issuer {doc.get('issuer')!r} != configured {self.issuer!r}")
                except netsafe.FetchError as e:
                    err = OidcError("idp_unreachable", 502, str(e))
                    self._disc_error = (time.monotonic(), err)
                    raise err
                except OidcError as err:
                    self._disc_error = (time.monotonic(), err)
                    raise
                self._disc, self._disc_at, self._disc_error = doc, time.monotonic(), None
            value = self._disc.get(name)
        if not isinstance(value, str):
            raise OidcError("idp_misconfigured", 502, f"discovery document has no {name}")
        return value

    def verify(self, token: str, nonce: Optional[str] = None) -> dict:
        try:
            header = jwtverify.split(token)[0]
            allowed = self.cfg.get("allowed_algs")
            if allowed and header.get("alg") not in allowed:
                raise jwtverify.TokenError(f"algorithm {header.get('alg')} is not allowed for this provider")
            return jwtverify.verify(token, self.jwks, issuer=self.issuer, audience=self.audience, nonce=nonce,
                                    max_age=self.cfg.get("max_token_age")).claims
        except jwtverify.TokenError as e:
            raise OidcError("invalid_token", 401, str(e))
        except netsafe.FetchError as e:
            raise OidcError("idp_unreachable", 502, str(e))

    def authorization_url(self, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
        q = {"response_type": "code", "client_id": self.audience, "redirect_uri": redirect_uri, "scope": self.cfg.get("scopes", "openid email profile"),
             "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        base = self.endpoint("authorization_endpoint")
        return base + ("&" if "?" in base else "?") + urllib.parse.urlencode(q)

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> str:
        """The ID token for an authorization code."""
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": self.audience, "code_verifier": verifier}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        try:
            status, body = self._post(self.endpoint("token_endpoint"), form)
        except netsafe.FetchError as e:
            raise OidcError("idp_unreachable", 502, str(e))
        tok = body.get("id_token")
        if status != 200 or not isinstance(tok, str):
            raise OidcError("login_failed", 401, f"token endpoint answered {status}: {str(body.get('error'))[:80]}")
        return tok


class ProviderCache:
    """Providers keep their key sets and discovery documents, so they are reused while the tenant's settings are unchanged."""

    MAX_AGE = 900.0

    def __init__(self):
        self._p: dict = {}
        self._lock = threading.Lock()

    def get(self, idp: dict, secret_loader: Callable[[], Optional[str]], **kw) -> Provider:
        key = (idp["id"], hashlib.sha256(repr(sorted(idp["config"].items(), key=str)).encode()).hexdigest(), idp.get("client_secret_ref"))
        with self._lock:
            p = self._p.get(key)
            if p is not None and time.monotonic() - p.created > self.MAX_AGE:      # picks up a rotated client secret
                p = None
            if p is None:
                for k in [k for k in self._p if k[0] == idp["id"]]:
                    del self._p[k]
                p = self._p[key] = Provider(idp["config"], secret_loader() if idp.get("client_secret_ref") else None, **kw)
            return p
