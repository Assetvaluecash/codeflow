"""A small but real OpenID Connect provider for tests: discovery, JWKS, authorization endpoint, token endpoint (PKCE and client-secret checks),
RSA and EC keys, key rotation, and a mint() for arbitrary (also hostile) tokens. It listens on loopback and speaks real HTTP, so the code under
test goes through the same netsafe fetch path it uses against a real provider."""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from tia.jwtverify import b64e


def _i(n: int) -> str:
    return b64e(n.to_bytes((n.bit_length() + 7) // 8 or 1, "big"))


class Key:
    def __init__(self, kind="RSA", kid=None, bits=2048, alg=None):
        self.kind, self.kid = kind, kid or secrets.token_hex(4)
        self.alg = alg
        self.private = rsa.generate_private_key(65537, bits) if kind == "RSA" else ec.generate_private_key(ec.SECP256R1())

    @property
    def jwk(self) -> dict:
        pub = self.private.public_key().public_numbers()
        if self.kind == "RSA":
            return {"kty": "RSA", "kid": self.kid, "use": "sig", "alg": self.alg or "RS256", "n": _i(pub.n), "e": _i(pub.e)}
        return {"kty": "EC", "kid": self.kid, "use": "sig", "alg": self.alg or "ES256", "crv": "P-256", "x": _i(pub.x), "y": _i(pub.y)}

    def sign(self, signing_input: bytes, alg: str) -> bytes:
        if alg == "RS256":
            return self.private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        if alg == "PS256":
            return self.private.sign(signing_input, padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())
        if alg == "ES256":
            r, s = utils.decode_dss_signature(self.private.sign(signing_input, ec.ECDSA(hashes.SHA256())))
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        raise ValueError(alg)


class FakeIdp:
    def __init__(self, client_id="tia-client", client_secret=None, issuer_path=""):
        self.client_id, self.client_secret = client_id, client_secret
        self.keys = [Key("RSA", "k1")]
        self.codes: dict = {}
        self.user = {"sub": "u-1", "email": "ada@example.com", "name": "Ada", "groups": ["tia-engineers"]}
        self.token_status = 200
        self.fail_authorize = None
        self.nonce_override = None
        self.issuer_override = None
        self.requests: list = []
        self.jwks_calls = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.issuer = self.base + issuer_path
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- tokens ------------------------------------------------------------------------------------------------------------------
    def mint(self, claims=None, alg="RS256", key=None, header=None, aud=None, sign=True, **over) -> str:
        key = key or self.keys[-1]
        now = int(time.time())
        c = {"iss": self.issuer, "aud": aud or self.client_id, "sub": self.user["sub"], "email": self.user.get("email"), "name": self.user.get("name"),
             "groups": self.user.get("groups"), "iat": now, "exp": now + 600, **(claims or {}), **over}
        c = {k: v for k, v in c.items() if v is not None}
        h = {"alg": alg, "typ": "JWT", "kid": key.kid, **(header or {})}
        si = b64e(json.dumps(h).encode()) + "." + b64e(json.dumps(c).encode())
        return si + "." + (b64e(key.sign(si.encode(), alg)) if sign else "")

    def rotate(self, keep_old=False):
        new = Key("RSA")
        self.keys = (self.keys + [new]) if keep_old else [new]
        return new

    # ---- http ----------------------------------------------------------------------------------------------------------------------
    def _handler(self):
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json", headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                idp.requests.append(("GET", u.path))
                if u.path.endswith("/.well-known/openid-configuration"):
                    return self._send(200, {"issuer": idp.issuer_override or idp.issuer, "authorization_endpoint": idp.base + "/authorize",
                                            "token_endpoint": idp.base + "/token", "jwks_uri": idp.base + "/jwks"})
                if u.path == "/jwks":
                    idp.jwks_calls += 1
                    return self._send(200, {"keys": [k.jwk for k in idp.keys]})
                if u.path == "/_set_user":                     # test control: who the next sign-in will be
                    idp.user = {"sub": q["sub"], "email": q.get("email"), "name": q.get("name"), "groups": [g for g in q.get("groups", "").split(",") if g],
                                **({"gh": q["gh"]} if q.get("gh") else {})}
                    return self._send(200, idp.user)
                if u.path == "/authorize":
                    if idp.fail_authorize:
                        return self._send(302, b"", headers={"Location": f"{q['redirect_uri']}?error={idp.fail_authorize}&state={q['state']}"})
                    code = secrets.token_urlsafe(16)
                    idp.codes[code] = {"challenge": q.get("code_challenge"), "method": q.get("code_challenge_method"), "nonce": q.get("nonce"),
                                       "client_id": q.get("client_id"), "redirect_uri": q.get("redirect_uri"), "user": dict(idp.user)}
                    return self._send(302, b"", headers={"Location": f"{q['redirect_uri']}?code={code}&state={q['state']}"})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                idp.requests.append(("POST", self.path, form))
                if self.path != "/token":
                    return self._send(404, {})
                if idp.token_status != 200:
                    return self._send(idp.token_status, {"error": "server_error"})
                rec = idp.codes.pop(form.get("code"), None)
                if rec is None or form.get("grant_type") != "authorization_code":
                    return self._send(400, {"error": "invalid_grant"})
                if idp.client_secret is not None and form.get("client_secret") != idp.client_secret:
                    return self._send(401, {"error": "invalid_client"})
                if rec["method"] != "S256" or not form.get("code_verifier") or \
                        urlsafe_b64encode(sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() != rec["challenge"]:
                    return self._send(400, {"error": "invalid_grant", "error_description": "PKCE"})
                if form.get("redirect_uri") != rec["redirect_uri"] or form.get("client_id") != idp.client_id:
                    return self._send(400, {"error": "invalid_grant"})
                u = rec["user"]
                extra = {k: v for k, v in u.items() if k not in ("sub", "email", "name", "groups")}
                tok = idp.mint({"sub": u["sub"], "email": u.get("email"), "name": u.get("name"), "groups": u.get("groups"),
                                "nonce": idp.nonce_override or rec["nonce"], **extra})
                self._send(200, {"id_token": tok, "token_type": "Bearer", "access_token": "opaque"})

        return H
