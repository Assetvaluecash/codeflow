"""An Azure DevOps REST stand-in: pull-request threads (list, create, edit the first comment) and pull-request statuses, behind basic auth with a PAT."""
import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAdo:
    def __init__(self, token="ado-pat"):
        self.token = token
        self.threads: dict = {}          # (project, repo, pr) -> [{"id", "status", "comments": [{"id", "content"}]}]
        self.statuses: list = []
        self.requests: list = []
        self.fail_status = None
        self.next_id = 10
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

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                path = self.path.split("?")[0]
                outer.requests.append((method, self.path, self.headers.get("Authorization")))
                auth = self.headers.get("Authorization", "")
                if auth != "Basic " + base64.b64encode(f":{outer.token}".encode()).decode():
                    return self._send(401, {"message": "TF400813: not authorized"})
                m = re.fullmatch(r"/([^/]+)/([^/]+)/_apis/git/repositories/([^/]+)/pullRequests/(\d+)(/.*)", path)
                if not m:
                    return self._send(404, {})
                key, rest = (m.group(2), m.group(3), int(m.group(4))), m.group(5)
                if outer.fail_status and method != "GET":
                    return self._send(outer.fail_status, {"message": "nope"})
                if rest == "/threads" and method == "GET":
                    return self._send(200, {"value": outer.threads.get(key, []), "count": len(outer.threads.get(key, []))})
                if rest == "/threads" and method == "POST":
                    outer.next_id += 2
                    t = {"id": outer.next_id, "status": body.get("status"), "comments": [{"id": 1, "content": c["content"]} for c in body["comments"]]}
                    outer.threads.setdefault(key, []).append(t)
                    return self._send(200, t)
                m2 = re.fullmatch(r"/threads/(\d+)/comments/(\d+)", rest)
                if m2 and method == "PATCH":
                    for t in outer.threads.get(key, []):
                        if t["id"] == int(m2.group(1)):
                            for c in t["comments"]:
                                if c["id"] == int(m2.group(2)):
                                    c["content"] = body["content"]
                                    return self._send(200, c)
                    return self._send(404, {})
                if rest == "/statuses" and method == "POST":
                    outer.statuses.append({"key": key, **body})
                    return self._send(201, body)
                self._send(404, {})

            def do_GET(self): self._handle("GET")
            def do_POST(self): self._handle("POST")
            def do_PATCH(self): self._handle("PATCH")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
