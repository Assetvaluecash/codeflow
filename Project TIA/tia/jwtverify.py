"""JSON Web Token verification for OpenID Connect (PL-001), on the `cryptography` package, no other dependency.

Deliberately narrow:
* only asymmetric algorithms (RS256/384/512, PS256/384/512, ES256/384/512): a token cannot be "verified" with a public key used as an
  HMAC secret, and "none" is never accepted;
* the key is chosen from the identity provider's published key set by `kid` (and algorithm family), never from the token itself
  (embedded `jwk`, `jku`, `x5u` are ignored; unknown critical headers reject the token);
* issuer, audience, expiry, not-before and (for sign-in) nonce are all checked; the clock tolerance is explicit and small.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

ALGS = {
    "RS256": ("RSA", "PKCS1", hashes.SHA256), "RS384": ("RSA", "PKCS1", hashes.SHA384), "RS512": ("RSA", "PKCS1", hashes.SHA512),
    "PS256": ("RSA", "PSS", hashes.SHA256), "PS384": ("RSA", "PSS", hashes.SHA384), "PS512": ("RSA", "PSS", hashes.SHA512),
    "ES256": ("EC", "P-256", hashes.SHA256), "ES384": ("EC", "P-384", hashes.SHA384), "ES512": ("EC", "P-521", hashes.SHA512),
}
CURVES = {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1, "P-521": ec.SECP521R1}
MAX_TOKEN = 16_384
LEEWAY = 60


class TokenError(Exception):
    """The token is not acceptable. The message is for the audit log, never for the caller."""


def b64d(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        raise TokenError("malformed base64url")


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _int(s: str) -> int:
    return int.from_bytes(b64d(s), "big")


def public_key(jwk: dict):
    kty = jwk.get("kty")
    try:
        if kty == "RSA":
            return rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
        if kty == "EC" and jwk.get("crv") in CURVES:
            return ec.EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), CURVES[jwk["crv"]]()).public_key()
    except (KeyError, ValueError, TypeError) as e:
        raise TokenError(f"unusable key: {e}")
    raise TokenError(f"unsupported key type {kty}")


def split(token: str) -> tuple[dict, dict, bytes, bytes]:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        raise TokenError("token missing or too large")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("not a compact JWS")
    try:
        header, payload = json.loads(b64d(parts[0])), json.loads(b64d(parts[1]))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("header or payload is not JSON")
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenError("header or payload is not an object")
    return header, payload, (parts[0] + "." + parts[1]).encode(), b64d(parts[2])


def peek(token: str) -> dict:
    """The UNVERIFIED claims: only for routing (which identity provider to ask); never trust them before verify()."""
    return split(token)[1]


def verify_signature(header: dict, signing_input: bytes, sig: bytes, jwk: dict) -> None:
    alg = header.get("alg")
    if alg not in ALGS:
        raise TokenError(f"algorithm {alg!r} is not accepted")
    family, variant, h = ALGS[alg]
    if jwk.get("kty") != family:
        raise TokenError("key type does not match the algorithm")
    if jwk.get("alg") not in (None, alg):
        raise TokenError("key is restricted to another algorithm")
    if jwk.get("use") not in (None, "sig"):
        raise TokenError("key is not a signature key")
    key = public_key(jwk)
    try:
        if family == "RSA":
            if key.key_size < 2048:
                raise TokenError("RSA key is smaller than 2048 bits")
            pad = padding.PKCS1v15() if variant == "PKCS1" else padding.PSS(padding.MGF1(h()), h().digest_size)
            key.verify(sig, signing_input, pad, h())
        else:
            if CURVES[variant]().name != key.curve.name:
                raise TokenError("key curve does not match the algorithm")
            n = (key.curve.key_size + 7) // 8
            if len(sig) != 2 * n:
                raise TokenError("bad ECDSA signature length")
            key.verify(utils.encode_dss_signature(int.from_bytes(sig[:n], "big"), int.from_bytes(sig[n:], "big")), signing_input,
                       ec.ECDSA(h()))
    except InvalidSignature:
        raise TokenError("signature does not verify")


class Jwks:
    """One identity provider's key set, cached; an unknown `kid` triggers (at most one per `min_refresh` seconds) a refetch for rotation."""

    def __init__(self, fetch: Callable[[], dict], ttl: float = 600.0, min_refresh: float = 30.0, now: Callable[[], float] = time.monotonic):
        self.fetch, self.ttl, self.min_refresh, self.now = fetch, ttl, min_refresh, now
        self._keys: list = []
        self._at = -1e18
        self._lock = threading.Lock()

    def _load(self, force: bool):
        with self._lock:
            t = self.now()
            if (force and t - self._at >= self.min_refresh) or t - self._at >= self.ttl or not self._keys:
                keys = self.fetch().get("keys")
                if not isinstance(keys, list) or not keys:
                    raise TokenError("the identity provider published no keys")
                self._keys, self._at = [k for k in keys if isinstance(k, dict)], t
            return list(self._keys)

    def find(self, header: dict) -> dict:
        kid, alg = header.get("kid"), header.get("alg")
        for force in (False, True):
            keys = self._load(force)
            fam = ALGS.get(alg, (None,))[0]
            hit = [k for k in keys if k.get("kty") == fam and (kid is None and len(keys) == 1 or k.get("kid") == kid)]
            if hit:
                return hit[0]
        raise TokenError("no key with this id in the identity provider's key set")


@dataclass
class Verified:
    header: dict
    claims: dict


def verify(token: str, jwks: Jwks, *, issuer: str, audience: str, now: Optional[float] = None, leeway: int = LEEWAY,
           nonce: Optional[str] = None, require_azp: bool = False, max_age: Optional[int] = None) -> Verified:
    header, claims, signing_input, sig = split(token)
    if set(header.get("crit") or []):
        raise TokenError("token has critical headers this verifier does not understand")
    if header.get("alg") not in ALGS:                       # before any key lookup: an attacker-chosen algorithm must not cost a key-set fetch
        raise TokenError(f"algorithm {header.get('alg')!r} is not accepted")
    verify_signature(header, signing_input, sig, jwks.find(header))
    t = time.time() if now is None else now
    if claims.get("iss") != issuer:
        raise TokenError("issuer mismatch")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if audience not in auds:
        raise TokenError("audience mismatch")
    if len(auds) > 1 and claims.get("azp") != audience:
        raise TokenError("multiple audiences and the authorised party is not this application")
    if require_azp and claims.get("azp") not in (None, audience):
        raise TokenError("authorised party mismatch")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise TokenError("token has no expiry")
    if t > exp + leeway:
        raise TokenError("token has expired")
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or t + leeway < nbf):
        raise TokenError("token is not valid yet")
    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and iat > t + leeway:
        raise TokenError("token was issued in the future")
    if max_age is not None and isinstance(iat, (int, float)) and t - iat > max_age:
        raise TokenError("token is older than allowed")
    if nonce is not None and claims.get("nonce") != nonce:
        raise TokenError("nonce mismatch")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise TokenError("token has no subject")
    return Verified(header, claims)
