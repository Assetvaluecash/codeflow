"""A GitHub REST API stand-in for the access tests: repository metadata, collaborators (paginated, per-repository failure), issue comments."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class AccessGitHub:
    def __init__(self):
        self.collaborators: dict = {}        # "owner/name" -> [(login, role)]
        self.private: dict = {}              # "owner/name" -> bool
        self.fail: dict = {}                 # "owner/name" -> HTTP status for every request about it
        self.comments: dict = {}             # (full, pr) -> [{"id", "body"}]
        self.requests: list = []
        self.next_id = 100
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
                path, _, query = self.path.partition("?")
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                outer.requests.append((method, path, self.headers.get("Authorization")))
                m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
                if not m:
                    return self._send(404, {})
                full, rest = m.group(1), m.group(2) or ""
                if full in outer.fail:
                    return self._send(outer.fail[full], {"message": "no"})
                if rest == "" and method == "GET":
                    if full not in outer.private:
                        return self._send(404, {})
                    return self._send(200, {"full_name": full, "private": outer.private[full]})
                if rest == "/collaborators" and method == "GET":
                    page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
                    rows = outer.collaborators.get(full, [])[(page - 1) * per: page * per]
                    return self._send(200, [{"login": l, "role_name": r} for l, r in rows])
                m2 = re.fullmatch(r"/issues/(\d+)/comments", rest)
                if m2:
                    pr = int(m2.group(1))
                    if method == "GET":
                        return self._send(200, outer.comments.get((full, pr), []))
                    if method == "POST":
                        outer.next_id += 1
                        outer.comments.setdefault((full, pr), []).append({"id": outer.next_id, "body": body["body"]})
                        return self._send(201, {"id": outer.next_id})
                m3 = re.fullmatch(r"/issues/comments/(\d+)", rest)
                if m3 and method == "PATCH":
                    for (f, pr), lst in outer.comments.items():
                        for c in lst:
                            if c["id"] == int(m3.group(1)):
                                c["body"] = body["body"]
                                return self._send(200, c)
                if "check-runs" in rest:
                    return self._send(403, {"message": "Resource not accessible by personal access token"})
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

    def comment_bodies(self, full, pr):
        return [c["body"] for c in self.comments.get((full, pr), [])]
