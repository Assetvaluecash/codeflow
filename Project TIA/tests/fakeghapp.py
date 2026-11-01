"""A GitHub App platform stand-in: verifies the App's JWT with the App's real public key, issues installation tokens, serves the repository,
comment, check-run and user-authorisation endpoints the connector uses. Check runs are accepted only for installation tokens (as on GitHub)."""
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tia import jwtverify
from tia.jwtverify import b64e


def new_key():
    k = rsa.generate_private_key(65537, 2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    n = k.public_key().public_numbers()
    jwk = {"kty": "RSA", "n": b64e(n.n.to_bytes(256, "big")), "e": b64e(n.e.to_bytes(3, "big"))}
    return pem, jwk


class FakeApp:
    def __init__(self, app_id="12345", client_id="Iv1.abc", client_secret="shh"):
        self.app_id, self.client_id, self.client_secret = app_id, client_id, client_secret
        self.pem, self.jwk = new_key()
        self.installations: dict = {}            # id -> {"account": login, "repos": {full: {...}}, "suspended": bool, "gone": bool}
        self.users: dict = {}                    # oauth code -> list of installation ids that user may see
        self.tokens: dict = {}                   # installation token -> installation id
        self.user_tokens: dict = {}
        self.comments: dict = {}                 # (full, pr) -> [{"id", "body"}]
        self.check_runs: list = []
        self.collaborators: dict = {}
        self.requests: list = []                 # (method, path, bearer kind)
        self.token_calls = 0
        self.fail_token_status = None
        self.clock = time.time
        self.next_id = 500
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _bearer(self):
                h = self.headers.get("Authorization", "")
                return h[7:] if h.startswith("Bearer ") else ""

            def _app_jwt_ok(self, tok):
                try:
                    header, claims, si, sig = jwtverify.split(tok)
                    jwtverify.verify_signature(header, si, sig, {**outer.jwk, "use": "sig"})
                except jwtverify.TokenError:
                    return False
                now = outer.clock()
                return claims.get("iss") == outer.app_id and claims["exp"] - claims["iat"] <= 600 and claims["exp"] > now and claims["iat"] <= now

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                path, _, query = self.path.partition("?")
                q = dict(urllib.parse.parse_qsl(query))
                tok = self._bearer()
                kind = "app" if tok.count(".") == 2 else "installation" if tok in outer.tokens else "user" if tok in outer.user_tokens else "other"
                outer.requests.append((method, path, kind))
                if path == "/login/oauth/access_token" and method == "POST":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    if form.get("client_id") != outer.client_id or form.get("client_secret") != outer.client_secret or form.get("code") not in outer.users:
                        return self._send(200, {"error": "bad_verification_code"})
                    ut = "ghu_" + secrets.token_hex(8)
                    outer.user_tokens[ut] = form["code"]
                    return self._send(200, {"access_token": ut})
                if path == "/user/installations":
                    if kind != "user":
                        return self._send(401, {"message": "Bad credentials"})
                    ids = outer.user_info(tok)["installations"]
                    page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
                    return self._send(200, {"installations": [{"id": i} for i in ids][(page - 1) * per: page * per]})
                if path == "/user":
                    if kind != "user":
                        return self._send(401, {"message": "Bad credentials"})
                    return self._send(200, {"login": outer.user_info(tok)["login"]})
                mo = re.fullmatch(r"/user/memberships/orgs/([^/]+)", path)
                if mo:
                    if kind != "user":
                        return self._send(401, {"message": "Bad credentials"})
                    if mo.group(1) not in outer.user_info(tok)["admin_orgs"]:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, {"role": "admin", "state": "active"})
                m = re.fullmatch(r"/app/installations/(\d+)(/access_tokens)?", path)
                if m:
                    if kind != "app" or not self._app_jwt_ok(tok):
                        return self._send(401, {"message": "A JSON web token could not be decoded"})
                    inst = outer.installations.get(int(m.group(1)))
                    if inst is None or inst.get("gone"):
                        return self._send(404, {"message": "Not Found"})
                    if m.group(2):
                        outer.token_calls += 1
                        if outer.fail_token_status:
                            return self._send(outer.fail_token_status, {"message": "x"})
                        t = "ghs_" + secrets.token_hex(10)
                        outer.tokens[t] = int(m.group(1))
                        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(outer.clock() + outer.token_ttl))
                        return self._send(201, {"token": t, "expires_at": exp})
                    return self._send(200, {"id": int(m.group(1)), "account": {"login": inst["account"], "type": inst.get("type", "Organization")},
                                            "permissions": {"checks": "write", "pull_requests": "write", "contents": "read", "metadata": "read"},
                                            "repository_selection": "selected", "suspended_at": "2026-01-01T00:00:00Z" if inst.get("suspended") else None})
                if path == "/installation/repositories":
                    if kind != "installation":
                        return self._send(401, {"message": "Bad credentials"})
                    repos = list(outer.installations[outer.tokens[tok]]["repos"].values())
                    page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
                    return self._send(200, {"total_count": len(repos), "repositories": repos[(page - 1) * per: page * per]})
                m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
                if not m:
                    return self._send(404, {})
                full, rest = m.group(1), m.group(2) or ""
                if rest == "/installation" and method == "GET":
                    if kind != "app" or not self._app_jwt_ok(tok):
                        return self._send(401, {"message": "bad jwt"})
                    for iid, inst in outer.installations.items():
                        if full in inst["repos"] and not inst.get("gone"):
                            return self._send(200, {"id": iid})
                    return self._send(404, {"message": "Not Found"})
                if kind != "installation":                                   # everything below takes an installation token
                    if "check-runs" in rest:
                        return self._send(403, {"message": "Resource not accessible by personal access token"})
                    if kind != "other" or not tok:
                        return self._send(401, {"message": "Bad credentials"})
                if rest == "" and method == "GET":
                    return self._send(200, {"full_name": full, "private": True})
                if rest == "/collaborators" and method == "GET":
                    return self._send(200, [{"login": l, "role_name": r} for l, r in outer.collaborators.get(full, [])])
                m2 = re.fullmatch(r"/issues/(\d+)/comments", rest)
                if m2:
                    pr = int(m2.group(1))
                    if method == "GET":
                        return self._send(200, outer.comments.get((full, pr), []))
                    outer.next_id += 1
                    outer.comments.setdefault((full, pr), []).append({"id": outer.next_id, "body": json.loads(raw)["body"]})
                    return self._send(201, {"id": outer.next_id})
                m3 = re.fullmatch(r"/issues/comments/(\d+)", rest)
                if m3 and method == "PATCH":
                    for lst in outer.comments.values():
                        for c in lst:
                            if c["id"] == int(m3.group(1)):
                                c["body"] = json.loads(raw)["body"]
                                return self._send(200, c)
                m4 = re.fullmatch(r"/commits/([0-9a-f]+)/check-runs", rest)
                if m4 and method == "GET":
                    return self._send(200, {"check_runs": [c for c in outer.check_runs if c["head_sha"] == m4.group(1)]})
                if rest == "/check-runs" and method == "POST":
                    outer.next_id += 1
                    outer.check_runs.append({**json.loads(raw), "id": outer.next_id})
                    return self._send(201, {"id": outer.next_id})
                m5 = re.fullmatch(r"/check-runs/(\d+)", rest)
                if m5 and method == "PATCH":
                    for c in outer.check_runs:
                        if c["id"] == int(m5.group(1)):
                            c.update(json.loads(raw))
                            return self._send(200, c)
                self._send(404, {})

            def do_GET(self): self._handle("GET")
            def do_POST(self): self._handle("POST")
            def do_PATCH(self): self._handle("PATCH")

        self.token_ttl = 3600
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def user_info(self, user_token):
        """users[code] is a list of installation ids (that user owns every account involved) or a dict with login/installations/admin_orgs."""
        v = self.users[self.user_tokens[user_token]]
        if isinstance(v, dict):
            return {"login": v.get("login", "u"), "installations": v["installations"], "admin_orgs": set(v.get("admin_orgs", []))}
        return {"login": "u", "installations": v, "admin_orgs": {self.installations[i]["account"] for i in v if i in self.installations}}

    def install(self, iid, account, repos):
        self.installations[iid] = {"account": account, "repos": {
            f"{account}/{r}": {"full_name": f"{account}/{r}", "name": r, "clone_url": f"https://github.com/{account}/{r}.git", "private": True,
                               "default_branch": "main"} for r in repos}}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
