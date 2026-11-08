"""A ServiceNow stand-in over real TLS (tests only): the change_request table lookup, the attachment listing and the attachment upload, with
basic-auth checking and a log of every request so a test can prove nothing else was touched."""
import base64
import json
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeServiceNow:
    def __init__(self, pki: dict, user="tia", password="secret-pw"):
        self.requests, self.attachments, self.tasks, self.hooks = [], [], [], []
        self.changes = {"CHG0031234": {"sys_id": "c0ffee00000000000000000000000001", "number": "CHG0031234", "state": "-2"}}
        auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                outer.requests.append({"method": method, "path": u.path, "query": q})
                if method == "POST" and u.path.startswith("/hooks/"):          # an incoming chat webhook (Slack / Teams style), no auth
                    outer.hooks.append({"path": u.path, "json": json.loads(body or b"null")})
                    return self._send(200, {"ok": True})
                if self.headers.get("Authorization") != auth:
                    return self._send(401, {"error": {"message": "User Not Authenticated"}})
                if method == "GET" and u.path == "/api/now/table/change_request":
                    num = q.get("sysparm_query", "").removeprefix("number=")
                    return self._send(200, {"result": [outer.changes[num]] if num in outer.changes else []})
                if method == "GET" and u.path == "/api/now/attachment":
                    parts = dict(p.split("=", 1) for p in q.get("sysparm_query", "").split("^") if "=" in p)
                    hits = [a for a in outer.attachments if a["table_sys_id"] == parts.get("table_sys_id") and a["file_name"] == parts.get("file_name")]
                    return self._send(200, {"result": hits})
                if method == "POST" and u.path == "/api/now/attachment/file":
                    a = {"sys_id": f"a{len(outer.attachments):031d}", "table_name": q["table_name"], "table_sys_id": q["table_sys_id"],
                         "file_name": q["file_name"], "content": body.decode("utf-8"), "content_type": self.headers.get("Content-Type")}
                    outer.attachments.append(a)
                    return self._send(201, {"result": {"sys_id": a["sys_id"], "file_name": a["file_name"]}})
                if method == "GET" and u.path == "/api/now/table/task":
                    corr = q.get("sysparm_query", "").removeprefix("correlation_id=")
                    return self._send(200, {"result": [{"sys_id": t["sys_id"], "number": t["number"]} for t in outer.tasks if t["correlation_id"] == corr]})
                if method == "POST" and u.path == "/api/now/table/task":
                    d = json.loads(body)
                    t = {**d, "sys_id": f"t{len(outer.tasks):031d}", "number": f"TASK{1000 + len(outer.tasks):07d}"}
                    outer.tasks.append(t)
                    return self._send(201, {"result": {"sys_id": t["sys_id"], "number": t["number"]}})
                return self._send(404, {"error": {"message": "not found"}})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(pki["cert"], pki["key"])
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.url = f"https://localhost:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
