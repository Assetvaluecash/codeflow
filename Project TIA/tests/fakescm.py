"""GitLab and Bitbucket Data Center REST stand-ins (tests only): merge-request notes and commit statuses, project members; pull-request
activities/comments, build statuses and repository/project permissions; bearer-token auth and a log of every request."""
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeScmHost:
    def __init__(self, token="host-token"):
        self.token, self.requests = token, []
        self.notes, self.statuses, self.comments = {}, [], {}
        self.members = [{"username": "Alice", "access_level": 30, "state": "active"}, {"username": "guest-gus", "access_level": 10, "state": "active"},
                        {"username": "rita", "access_level": 20, "state": "active"}, {"username": "blocked-bob", "access_level": 40, "state": "blocked"}]
        self.bb_repo_perms = [{"user": {"name": "alice"}, "permission": "REPO_WRITE"}]
        self.bb_project_perms = [{"user": {"name": "alice"}, "permission": "PROJECT_READ"}, {"user": {"name": "carol"}, "permission": "PROJECT_ADMIN"}]
        self.next_id = 100
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
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                path = urllib.parse.unquote(u.path)
                outer.requests.append((method, path, q))
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"message": "401 Unauthorized"})
                # ---- GitLab
                m = re.fullmatch(r"/api/v4/projects/(.+)/merge_requests/(\d+)/notes(?:/(\d+))?", path)
                if m:
                    key = (m.group(1), int(m.group(2)))
                    notes = outer.notes.setdefault(key, [])
                    if method == "GET":
                        return self._send(200, notes)
                    if method == "POST":
                        outer.next_id += 1
                        notes.append({"id": outer.next_id, "body": body["body"]})
                        return self._send(201, notes[-1])
                    if method == "PUT":
                        for x in notes:
                            if x["id"] == int(m.group(3)):
                                x["body"] = body["body"]
                                return self._send(200, x)
                        return self._send(404, {})
                m = re.fullmatch(r"/api/v4/projects/(.+)/statuses/([0-9a-f]{40})", path)
                if m and method == "POST":
                    outer.statuses.append({"provider": "gitlab", "project": m.group(1), "sha": m.group(2), **q})
                    return self._send(201, q)
                m = re.fullmatch(r"/api/v4/projects/(.+)/members/all", path)
                if m and method == "GET":
                    return self._send(200, outer.members if q.get("page", "1") == "1" else [])
                # ---- Bitbucket Data Center
                m = re.fullmatch(r"/rest/api/1\.0/projects/([^/]+)/repos/([^/]+)/pull-requests/(\d+)/(activities|comments)(?:/(\d+))?", path)
                if m:
                    key = (m.group(1), m.group(2), int(m.group(3)))
                    cs = outer.comments.setdefault(key, [])
                    if m.group(4) == "activities" and method == "GET":
                        return self._send(200, {"values": [{"action": "COMMENTED", "comment": c} for c in cs], "isLastPage": True})
                    if m.group(4) == "comments" and method == "POST":
                        outer.next_id += 1
                        cs.append({"id": outer.next_id, "text": body["text"], "version": 0})
                        return self._send(201, cs[-1])
                    if m.group(4) == "comments" and method == "PUT":
                        for c in cs:
                            if c["id"] == int(m.group(5)):
                                if body.get("version") != c["version"]:
                                    return self._send(409, {"errors": [{"message": "stale version"}]})
                                c["text"], c["version"] = body["text"], c["version"] + 1
                                return self._send(200, c)
                        return self._send(404, {})
                m = re.fullmatch(r"/rest/build-status/1\.0/commits/([0-9a-f]{40})", path)
                if m and method == "POST":
                    outer.statuses.append({"provider": "bitbucket", "sha": m.group(1), **body})
                    return self._send(204, {})
                if re.fullmatch(r"/rest/api/1\.0/projects/[^/]+/repos/[^/]+/permissions/users", path):
                    return self._send(200, {"values": outer.bb_repo_perms, "isLastPage": True})
                if re.fullmatch(r"/rest/api/1\.0/projects/[^/]+/permissions/users", path):
                    return self._send(200, {"values": outer.bb_project_perms, "isLastPage": True})
                return self._send(404, {"message": "not found"})

            def do_GET(self): self._handle("GET")
            def do_POST(self): self._handle("POST")
            def do_PUT(self): self._handle("PUT")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
