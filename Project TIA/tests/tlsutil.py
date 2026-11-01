"""Test helpers: a throw-away CA and a server certificate (real TLS handshakes in tests), and a fake HashiCorp Vault (KV v2 + Transit)
that enforces tokens and Transit context binding, so the Vault client is tested against realistic behaviour."""
import base64
import datetime
import ipaddress
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_pki(directory, key_password: bytes = None) -> dict:
    """ca.pem, server.pem, server.key (optionally password protected), plus 'other-ca.pem' that did NOT sign the server certificate."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    def ca(cn):
        k = _key()
        c = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=30))
             .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(k, hashes.SHA256()))
        return k, c
    ck, cc = ca("tia-test-ca")
    _, other = ca("tia-other-ca")
    sk = _key()
    sc = (x509.CertificateBuilder().subject_name(_name("localhost")).issuer_name(cc.subject).public_key(sk.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                         critical=False).sign(ck, hashes.SHA256()))
    enc = (serialization.BestAvailableEncryption(key_password) if key_password else serialization.NoEncryption())
    out = {"ca": d / "ca.pem", "cert": d / "server.pem", "key": d / "server.key", "other_ca": d / "other-ca.pem"}
    out["ca"].write_bytes(cc.public_bytes(serialization.Encoding.PEM))
    out["other_ca"].write_bytes(other.public_bytes(serialization.Encoding.PEM))
    out["cert"].write_bytes(sc.public_bytes(serialization.Encoding.PEM))
    out["key"].write_bytes(sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc))
    return {k: str(v) for k, v in out.items()}


class FakeVault:
    """KV v2 at <mount>/data/<path>, Transit at transit/{encrypt,decrypt}/<key>. Everything needs the right X-Vault-Token."""

    def __init__(self, token="s.test-token", pki=None):
        self.token, self.kv, self.transit_keys, self.requests = token, {}, {}, []
        self.fail_status = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                outer.requests.append({"method": method, "path": self.path, "token": self.headers.get("X-Vault-Token"),
                                       "namespace": self.headers.get("X-Vault-Namespace"), "body": body})
                if outer.fail_status:
                    return self._send(outer.fail_status, {"errors": ["secret-detail-that-must-not-be-echoed-by-the-client"]})
                if self.headers.get("X-Vault-Token") != outer.token:
                    return self._send(403, {"errors": ["permission denied"]})
                parts = self.path.strip("/").split("/")                       # v1/<mount>/data/<path...> | v1/transit/encrypt/<key>
                if parts[2] == "data" and method == "GET":
                    item = outer.kv.get("/".join([parts[1]] + parts[3:]))
                    return self._send(200, {"data": {"data": item, "metadata": {"version": 1}}}) if item is not None else self._send(404, {"errors": []})
                if parts[2] == "data" and method == "POST":
                    outer.kv["/".join([parts[1]] + parts[3:])] = body["data"]
                    return self._send(200, {"data": {"version": 1}})
                if parts[1] == "transit" and parts[2] in ("encrypt", "decrypt"):
                    key = outer.transit_keys.setdefault(parts[3], AESGCM.generate_key(256))
                    ctx = base64.b64decode(body.get("context", "")) if body.get("context") else b""
                    if parts[2] == "encrypt":
                        nonce = os.urandom(12)
                        ct = AESGCM(key).encrypt(nonce, base64.b64decode(body["plaintext"]), ctx)
                        return self._send(200, {"data": {"ciphertext": "vault:v1:" + base64.b64encode(nonce + ct).decode()}})
                    raw = base64.b64decode(body["ciphertext"].split(":", 2)[2])
                    try:
                        pt = AESGCM(key).decrypt(raw[:12], raw[12:], ctx)
                    except Exception:
                        return self._send(400, {"errors": ["invalid ciphertext or context"]})
                    return self._send(200, {"data": {"plaintext": base64.b64encode(pt).decode()}})
                self._send(404, {"errors": ["unsupported path"]})

            def do_GET(self): self._handle("GET")
            def do_POST(self): self._handle("POST")
            def do_PUT(self): self._handle("POST")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        scheme = "http"
        if pki:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(pki["cert"], pki["key"])
            self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
            scheme = "https"
        self.addr = f"{scheme}://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class FakeCollector:
    """An OTLP/HTTP trace collector: records every POST /v1/traces body and the headers it came with."""

    def __init__(self, status=200, delay=0.0):
        self.payloads, self.headers, self.status, self.delay = [], [], status, delay
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                if outer.delay:
                    import time
                    time.sleep(outer.delay)
                if self.path == "/v1/traces" and outer.status == 200:
                    outer.payloads.append(json.loads(body))
                    outer.headers.append(dict(self.headers))
                self.send_response(outer.status if self.path == "/v1/traces" else 404)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.endpoint = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def spans(self):
        return [s for p in self.payloads for rs in p["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]

    def close(self):
        self.srv.shutdown()
