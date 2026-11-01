"""UC1-05 (receiver) + UC1-14 (comment upsert wiring), verified against real PostgreSQL, real git remotes and a local
fake of the GitHub REST API. Live github.com behaviour is verified separately (see summary.md)."""
import hashlib
import hmac
import json
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import make_app
from tia.analysis_service import make_handlers
from tia.jobs import JobQueue
from tia.report import MARKER
from tia.webhooks import ScmConfig, match_repo, parse_pull_request, repo_key, verify_signature
from tia.worker import Worker

SECRET = b"whsec-test"
TOKEN = "tok-SECRET-123"


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


# ---- pure helpers ---------------------------------------------------------------------------------------------------
def test_signature_verification():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, b"other"))
    assert not verify_signature(SECRET, body + b" ", sign(body))             # tampered body
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=" + sign(body)[7:])       # wrong scheme
    assert not verify_signature(SECRET, body, "sha256=")
    assert not verify_signature(b"", body, sign(body, b""))                   # no secret configured => never valid


def ev(action="opened", **kw):
    p = {"action": action, "pull_request": {"number": 5, "draft": False, "head": {"sha": "a" * 40}, "base": {"ref": "main"}},
         "repository": {"full_name": "acme/pay", "clone_url": "https://github.com/acme/pay.git"}}
    p.update(kw)
    return p


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize", "ready_for_review"])
def test_parse_relevant_actions(action):
    r = parse_pull_request(ev(action))
    assert r["pr"] == 5 and r["head_sha"] == "a" * 40 and r["base_ref"] == "main" and r["full_name"] == "acme/pay"


@pytest.mark.parametrize("action", ["closed", "labeled", "assigned", "review_requested", "converted_to_draft", None])
def test_parse_ignores_irrelevant_actions(action):
    assert parse_pull_request(ev(action)) is None


def test_parse_edited_only_when_base_changed():
    assert parse_pull_request(ev("edited", changes={"title": {"from": "x"}})) is None
    assert parse_pull_request(ev("edited", changes={"base": {"ref": {"from": "old"}}}))["base_ref"] == "main"


def test_parse_malformed_raises():
    with pytest.raises(ValueError):
        parse_pull_request({"action": "opened"})
    with pytest.raises(ValueError):
        parse_pull_request(ev(pull_request={"number": 5}))


def test_repo_matching_ignores_scheme_case_and_dot_git():
    repos = [SimpleNamespace(name="p", url="https://GitHub.com/Acme/Pay.git"), SimpleNamespace(name="q", url="")]
    assert match_repo(repos, "http://github.com/acme/pay").name == "p"
    assert match_repo(repos, "https://github.com/acme/other.git") is None
    assert repo_key("https://github.com/a/b.git/") == repo_key("https://github.com/a/b")


# ---- fake GitHub REST API --------------------------------------------------------------------------------------------
class FakeGitHub:
    def __init__(self):
        self.comments = {}          # pr -> list of {"id", "body"}
        self.writes = []            # (method, pr)
        self.tokens = set()
        self.fail_status = None
        self.check_runs = []        # created check runs (dicts, newest last)
        self.traceparents = []      # the traceparent header of every request TIA made
        self.check_status = None    # set to 403 to mimic a personal access token (checks need a GitHub App)
        self.next_id = 1000
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def _handle(self, method):
                outer.tokens.add(self.headers.get("Authorization"))
                outer.traceparents.append(self.headers.get("traceparent"))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                parts = self.path.split("?")[0].strip("/").split("/")        # repos/o/r/issues/<pr>/comments | issues/comments/<id>
                if "check-runs" in parts:                                       # repos/o/r/commits/<sha>/check-runs | check-runs[/<id>]
                    if outer.check_status:
                        return self._send(outer.check_status, {"message": "Resource not accessible by personal access token"})
                    if method == "GET":
                        sha = parts[4]
                        return self._send(200, {"check_runs": [c for c in outer.check_runs if c["head_sha"] == sha]})
                    if method == "POST":
                        outer.next_id += 1
                        outer.check_runs.append({**body, "id": outer.next_id})
                        return self._send(201, {"id": outer.next_id})
                    if method == "PATCH":
                        for c in outer.check_runs:
                            if c["id"] == int(parts[4]):
                                c.update(body)
                                return self._send(200, c)
                    return self._send(404, {})
                if method != "GET" and outer.fail_status:
                    return self._send(outer.fail_status, {"message": "nope"})
                if method == "GET":
                    return self._send(200, outer.comments.get(int(parts[4]), []))
                if method == "POST":
                    pr = int(parts[4]); outer.next_id += 1
                    outer.comments.setdefault(pr, []).append({"id": outer.next_id, "body": body["body"]})
                    outer.writes.append(("POST", pr))
                    return self._send(201, {"id": outer.next_id})
                if method == "PATCH":
                    cid = int(parts[5])
                    for pr, lst in outer.comments.items():
                        for c in lst:
                            if c["id"] == cid:
                                c["body"] = body["body"]; outer.writes.append(("PATCH", pr))
                                return self._send(200, c)
                self._send(404, {})

            def do_GET(self): self._handle("GET")
            def do_POST(self): self._handle("POST")
            def do_PATCH(self): self._handle("PATCH")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def gh():
    g = FakeGitHub()
    yield g
    g.close()


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def origins(fx, tmp_path_factory):
    """Each fixture repository as a bare 'remote' that can serve refs/pull/N/head."""
    root = tmp_path_factory.mktemp("origins")
    out = {}
    for name, path in fx["paths"].items():
        bare = root / f"{name}.git"
        git(root, "clone", "-q", "--bare", path, str(bare))
        out[name] = bare
    return out


def set_pr(origins, n, sha):
    git(origins["payments-service"], "update-ref", f"refs/pull/{n}/head", sha)


class Env:
    pass


@pytest.fixture
def env(fx, origins, gh, tmp_path):
    e = Env()
    e.fx, e.origins, e.gh, e.ws = fx, origins, gh, tmp_path / "ws"
    e.scm = ScmConfig(github_token=TOKEN, github_webhook_secret=SECRET, github_api_url=gh.url, comment_audience="target")
    e.tenant = conftest.new_tenant()
    e.app = make_app(tmp_path / "data", tenant=e.tenant, allow_local=True, scm=e.scm)
    e.c = TestClient(e.app)
    for n, bare in origins.items():
        r = e.c.post("/api/v1/repositories", json={"url": bare.as_uri(), "name": n})
        assert r.status_code == 200, r.text
    e.url = origins["payments-service"].as_uri()
    e.ws = tmp_path / "data"                      # the app clones into the workspace it was created with

    def post(payload, event="pull_request", secret=SECRET, headers=None):
        body = json.dumps(payload).encode()
        h = {"X-GitHub-Event": event, "X-GitHub-Delivery": uuid.uuid4().hex, "X-Hub-Signature-256": sign(body, secret),
             "Content-Type": "application/json", **(headers or {})}
        return e.c.post("/api/v1/webhooks/github", content=body, headers=h)
    e.post = post

    def event(pr, head_sha, action="opened", base="main"):
        return {"action": action, "number": pr,
                "pull_request": {"number": pr, "draft": False, "head": {"sha": head_sha}, "base": {"ref": base}},
                "repository": {"full_name": "acme/payments-service", "clone_url": e.url}}
    e.event = event

    def drain(scm=None):
        h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(e.ws), scm=scm or e.scm)
        w = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
        while w.run_once():
            pass
    e.drain = drain
    e.job = lambda jid: e.c.get(f"/api/v1/jobs/{jid}").json()
    return e


# ---- endpoint guards ------------------------------------------------------------------------------------------------
def test_receiver_is_off_without_a_secret(tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    assert c.post("/api/v1/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "ping"}).status_code == 503


def test_rejects_bad_or_missing_signature_and_does_not_audit_it(env):
    body = json.dumps(env.event(5, "a" * 40)).encode()
    before = len(env.c.get("/api/v1/audit").json())
    assert env.post(env.event(5, "a" * 40), secret=b"wrong").status_code == 401
    r = env.c.post("/api/v1/webhooks/github", content=body, headers={"X-GitHub-Event": "pull_request"})
    assert r.status_code == 401
    assert len(env.c.get("/api/v1/audit").json()) == before


def test_ping_and_unhandled_events_and_actions(env):
    assert env.post({"zen": "x"}, event="ping").json() == {"ok": True, "event": "ping"}
    r = env.post({"ref": "refs/heads/x"}, event="push")
    assert r.status_code == 202 and "not handled" in r.json()["ignored"]
    r = env.post(env.event(5, "a" * 40, action="closed"))
    assert r.status_code == 202 and "no analysis" in r.json()["ignored"]
    assert env.post({"action": "opened"}).status_code == 400


def test_unregistered_repository_is_ignored_and_audited(env):
    p = env.event(5, "a" * 40)
    p["repository"]["clone_url"] = "https://github.com/acme/unknown.git"
    r = env.post(p)
    assert r.status_code == 202 and "not registered" in r.json()["ignored"]
    assert "webhook_ignored" in [a["event"] for a in env.c.get("/api/v1/audit").json()]


def test_missing_token_is_a_clear_server_error(env, tmp_path):
    scm = ScmConfig(github_token=None, github_webhook_secret=SECRET)
    c = TestClient(make_app(tmp_path / "d2", allow_local=True, scm=scm))
    body = json.dumps(env.event(5, "a" * 40)).encode()
    r = c.post("/api/v1/webhooks/github", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)})
    assert r.status_code == 503 and "TIA_GITHUB_TOKEN" in r.json()["detail"]


# ---- end to end -----------------------------------------------------------------------------------------------------
def test_pull_request_event_to_pr_comment_and_update_in_place(env):
    breaking, impl = env.fx["shas"]["breaking"], env.fx["shas"]["impl"]
    set_pr(env.origins, 5, breaking)

    r = env.post(env.event(5, breaking))
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["repository"] == "payments-service" and job["deduplicated"] is False
    again = env.post(env.event(5, breaking)).json()                                   # GitHub redelivery
    assert again["deduplicated"] is True and again["job_id"] == job["job_id"]

    env.drain()
    done = env.job(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["comment"] == "created" and done["result"]["status"] == "complete_with_gaps"
    # the analysis behind it is real: the PR's schema change reaches the downstream consumers
    rep = env.c.get(f"/api/v1/impact-analyses/{done['result']['analysis_id']}").json()
    assert rep["summary"]["findings"] == 5 and rep["summary"]["by_severity"]["critical"] == 2
    assert rep["change"]["pull_request"] == 5

    [comment] = env.gh.comments[5]
    assert MARKER in comment["body"]
    assert "BREAKING: PaymentStatus.status removed" in comment["body"]            # the PR's own contract change is shown
    assert "withheld" in comment["body"] and "No findings" not in comment["body"]  # never a quiet 'clean' result
    # default audience = target repository only: downstream repositories are not named in a PR comment
    for other in ("reconciliation-service", "mobile-bff", "ledger-sync", "audit-reporting"):
        assert other not in comment["body"], other
    assert env.gh.tokens == {f"Bearer {TOKEN}"}
    first_body = comment["body"]

    # a new push to the same PR updates the same comment, it does not add another
    set_pr(env.origins, 5, impl)
    new = env.post(env.event(5, impl, action="synchronize")).json()
    assert new["deduplicated"] is False and new["job_id"] != job["job_id"]
    env.drain()
    assert env.job(new["job_id"])["result"]["comment"] == "updated"
    assert len(env.gh.comments[5]) == 1 and env.gh.comments[5][0]["body"] != first_body
    assert env.gh.writes == [("POST", 5), ("PATCH", 5)]

    audit = env.c.get("/api/v1/audit").json()
    events = [a["event"] for a in audit]
    assert events.count("webhook_accepted") == 3 and events.count("pr_comment_published") == 2
    assert TOKEN not in json.dumps(audit) and TOKEN not in json.dumps(env.job(job["job_id"]))
    assert env.c.get("/api/v1/audit/verify").json()["ok"] is True


def test_full_detail_comment_when_configured(env):
    set_pr(env.origins, 6, env.fx["shas"]["breaking"])
    env.post(env.event(6, env.fx["shas"]["breaking"]))
    env.drain(ScmConfig(github_token=TOKEN, github_api_url=env.gh.url, comment_audience="all"))
    body = env.gh.comments[6][0]["body"]
    assert "reconciliation-service" in body and "mobile-bff" in body


def test_newer_push_supersedes_the_older_run_and_only_one_comment_is_written(env):
    breaking, impl = env.fx["shas"]["breaking"], env.fx["shas"]["impl"]
    set_pr(env.origins, 8, breaking)
    old = env.post(env.event(8, breaking)).json()
    set_pr(env.origins, 8, impl)
    new = env.post(env.event(8, impl, action="synchronize")).json()
    assert env.job(old["job_id"])["status"] == "superseded" and env.job(new["job_id"])["status"] == "queued"
    env.drain()
    assert env.job(old["job_id"])["status"] == "superseded"                           # never ran, never commented
    assert env.job(new["job_id"])["status"] == "succeeded"
    assert env.gh.writes == [("POST", 8)] and len(env.gh.comments[8]) == 1


def test_github_rejecting_the_comment_fails_permanently_with_a_clear_reason(env):
    set_pr(env.origins, 9, env.fx["shas"]["breaking"])
    job = env.post(env.event(9, env.fx["shas"]["breaking"])).json()
    env.gh.fail_status = 403
    env.drain()
    j = env.job(job["job_id"])
    assert j["status"] == "failed" and j["attempts"] == 1
    assert "HTTP 403" in j["error"] and TOKEN not in j["error"]


def test_pull_request_that_cannot_be_fetched_fails_permanently(env):
    job = env.post(env.event(404, "b" * 40)).json()                                   # no refs/pull/404/head on the remote
    env.drain()
    j = env.job(job["job_id"])
    assert j["status"] == "failed" and "pull request 404" in j["error"]
