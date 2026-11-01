import base64
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tia import repos as R  # noqa: E402
from conftest import fresh_store, make_app  # noqa: E402


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


# ---- pure helpers -------------------------------------------------------------------------------------
@pytest.mark.parametrize("url,prov", [
    ("https://github.com/acme/pay.git", "github"),
    ("https://dev.azure.com/acme/proj/_git/pay", "ado"),
    ("https://acme.visualstudio.com/proj/_git/pay", "ado"),
    ("https://tfs.bank.local/tfs/Col/proj/_git/pay", "ado"),
    ("git@github.com:acme/pay.git", "github"),
    ("https://gitlab.example.com/acme/pay.git", "git")])
def test_detect_provider(url, prov):
    assert R.detect_provider(url) == prov


def test_derive_name():
    assert R.derive_name("https://dev.azure.com/acme/proj/_git/Pay Service") == "Pay-Service"
    assert R.derive_name("https://github.com/acme/pay.git") == "pay"
    assert R.derive_name("C:\\src\\my-service") == "my-service"


def test_auth_goes_through_env_not_args_and_is_redacted():
    env = R._auth_env("ado", "S3CRET")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == ":S3CRET"
    assert base64.b64decode(R._auth_env("github", "S3CRET")["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:S3CRET"
    leaked = "fatal: header Authorization: Basic " + env["GIT_CONFIG_VALUE_0"].split()[-1] + " token S3CRET"
    assert "S3CRET" not in R._redact(leaked, "S3CRET") and env["GIT_CONFIG_VALUE_0"].split()[-1] not in R._redact(leaked, "S3CRET")


def test_friendly_errors():
    f = R._friendly
    assert "provide an access token" in f("fatal: could not read Username for 'https://github.com': terminal prompts disabled", False, "clone")
    assert "token was rejected" in f("remote: Invalid username or password.\nfatal: Authentication failed for 'https://x'", True, "clone")
    assert "not found" in f("fatal: repository 'https://x/y.git/' not found", True, "clone")
    assert "Cannot reach" in f("fatal: unable to access 'https://x/': Could not resolve host: x", False, "clone")
    assert f("Cloning into bare repository 'z'...\nfatal: weird", False, "clone") == "fatal: weird"


def test_git_failure_message_has_no_token(tmp_path):
    with pytest.raises(R.IngestError) as e:
        R._git(["clone", "--bare", "https://127.0.0.1:9/x/y.git", str(tmp_path / "z")], provider="github", token="S3CRET", timeout=60)
    assert "S3CRET" not in str(e.value)


# ---- service with real repos -----------------------------------------------------------------------------
@pytest.fixture
def origin(tmp_path):
    """A 'remote' repository (a bare repo acting as GitHub) with a main branch and a PR ref."""
    work = tmp_path / "work"; work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("x = 1\n"); git(work, "add", "-A"); git(work, "commit", "-qm", "one")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    git(work, "checkout", "-qb", "feature"); (work / "a.py").write_text("x = 2\n"); git(work, "commit", "-qam", "two")
    git(work, "push", "-q", str(bare), "feature:refs/pull/7/head")
    return {"work": work, "bare": bare, "url": bare.as_uri()}


def svc(tmp_path, **kw):
    return R.RepoService(fresh_store(), str(tmp_path / "ws"), **kw)


def test_register_remote_sync_and_pr(origin, tmp_path):
    s = svc(tmp_path, allow_local=True)
    r = s.register(url=origin["url"], name="pay")
    assert r.kind == "remote" and r.default_branch == "main" and r.branches == ["main"]
    # new commit on origin main -> sync picks it up
    git(origin["work"], "checkout", "-q", "main"); (origin["work"] / "b.py").write_text("y = 1\n")
    git(origin["work"], "add", "-A"); git(origin["work"], "commit", "-qm", "three"); git(origin["work"], "push", "-q", str(origin["bare"]), "main")
    assert s.sync("pay").last_commit == git(origin["bare"], "rev-parse", "main")
    # PR ref is fetched on demand, regardless of provider
    sha = s.fetch_pull_request("pay", 7)
    assert sha == git(origin["bare"], "rev-parse", "refs/pull/7/head")
    with pytest.raises(R.IngestError):
        s.fetch_pull_request("pay", 999)
    assert s.merge_base("pay", "main", sha)
    s.remove("pay")
    assert not Path(r.path).exists() and s.list() == []


def test_sync_follows_remote_default_branch_change(origin, tmp_path):
    """Found live: GitHub's default branch was 'breaking' at clone time, later changed to 'main'; PR base must follow."""
    git(origin["bare"], "branch", "other", "main")
    git(origin["bare"], "symbolic-ref", "HEAD", "refs/heads/other")
    s = svc(tmp_path, allow_local=True)
    assert s.register(url=origin["url"], name="pay").default_branch == "other"
    git(origin["bare"], "symbolic-ref", "HEAD", "refs/heads/main")
    assert s.sync("pay").default_branch == "main"


def test_empty_change_is_flagged_not_reported_as_no_impact(fx, store):
    from tia.impact import AnalysisRequest, analyze
    same = fx["shas"]["base"]
    rep = analyze(AnalysisRequest(repos=fx["paths"], target="payments-service", base=same, head=same), store)
    assert rep["status"] == "complete_with_gaps"
    assert any(u["area"] == "empty_change" for u in rep["unknowns"])


def test_duplicate_and_guards(origin, tmp_path):
    s = svc(tmp_path, allow_local=True)
    s.register(url=origin["url"], name="pay")
    with pytest.raises(R.IngestError, match="already registered"):
        s.register(url=origin["url"], name="pay")
    with pytest.raises(R.IngestError, match="credentials"):
        s.register(url="https://user:pw@github.com/a/b.git")
    with pytest.raises(R.IngestError, match="exactly one"):
        s.register()
    with pytest.raises(R.IngestError, match="only https"):
        s.register(url="ftp://x/y")
    locked = svc(tmp_path / "locked")
    with pytest.raises(R.IngestError, match="disabled"):
        locked.register(path=str(origin["work"]))
    with pytest.raises(R.IngestError, match="disabled"):
        locked.register(url=origin["url"])
    allow = svc(tmp_path / "allow", allowed_hosts=["github.com"])
    with pytest.raises(R.IngestError, match="allowed git hosts"):
        allow.register(url="https://evil.example.com/a/b.git")


def test_register_local_in_place_never_deleted(origin, tmp_path):
    s = svc(tmp_path, allow_local=True)
    r = s.register(path=str(origin["work"]), name="local")
    assert r.kind == "local" and r.path == str(origin["work"].resolve())
    s.remove("local")
    assert origin["work"].exists()                      # TIA never deletes a user's own repo
    with pytest.raises(R.IngestError, match="not a git repository"):
        s.register(path=str(tmp_path))


# ---- discovery against fake provider APIs -------------------------------------------------------------------
class _Fake(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Fake.seen.append((self.path, self.headers.get("Authorization")))
        if "/_apis/git/repositories" in self.path:
            body = {"value": [{"name": "pay", "remoteUrl": "https://dev.azure.com/o/p/_git/pay", "defaultBranch": "refs/heads/main"},
                              {"name": "old", "remoteUrl": "https://dev.azure.com/o/p/_git/old", "isDisabled": True}]}
        elif self.path.startswith("/orgs/acme/repos"):
            body = [{"name": "svc", "clone_url": "https://github.com/acme/svc.git", "default_branch": "main"},
                    {"name": "dead", "clone_url": "https://github.com/acme/dead.git", "archived": True}]
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_api():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_discover_ado_and_github(fake_api, tmp_path):
    s = svc(tmp_path, allow_local=True)                 # the fake API is on this machine; hosts must otherwise be public
    found = s.discover("ado", "o", "p", "PAT", api_base=fake_api)
    assert [f["name"] for f in found] == ["pay"] and found[0]["default_branch"] == "main"
    assert base64.b64decode(_Fake.seen[-1][1].split()[-1]).decode() == ":PAT"
    found = s.discover("github", "acme", None, "TOK", api_base=fake_api)
    assert [f["name"] for f in found] == ["svc"] and _Fake.seen[-1][1] == "Bearer TOK"
    with pytest.raises(R.IngestError, match="needs 'project'"):
        s.discover("ado", "o", None, "PAT", api_base=fake_api)
    with pytest.raises(R.IngestError, match="not found"):
        s.discover("github", "missing", None, "TOK", api_base=fake_api)


# ---- end to end through the HTTP API ---------------------------------------------------------------------------
def test_api_end_to_end_with_demo_landscape(fx, tmp_path):
    c = TestClient(make_app(tmp_path / "data", allow_local=True))
    assert c.get("/").status_code == 200 and "TIA" in c.get("/").text
    for name, path in fx["paths"].items():
        r = c.post("/api/v1/repositories", json={"path": path, "name": name})
        assert r.status_code == 200, r.text
    assert len(c.get("/api/v1/repositories").json()) == 6
    r = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "breaking", "base": "main"})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["status"] == "complete_with_gaps" and len(rep["findings"]) == 5
    aid = r.json()["analysis_id"]
    assert "TIA impact analysis" in c.get(f"/api/v1/impact-analyses/{aid}/comment").text
    limited = c.get(f"/api/v1/impact-analyses/{aid}", headers={"X-TIA-Readable-Repos": "payments-service,mobile-bff"}).json()
    assert {f["component"] for f in limited["findings"]} == {"mobile-bff"}
    assert c.get(f"/api/v1/impact-analyses/{aid}/unknowns").json() and c.get(f"/api/v1/impact-analyses/{aid}/paths").json()
    # errors are 4xx with a message, never a stack trace
    bad = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "nope"})
    assert bad.status_code == 400 and "not found" in bad.json()["detail"]
    assert c.post("/api/v1/impact-analyses", json={"target": "payments-service"}).status_code == 400
    assert c.post("/api/v1/impact-analyses", json={"target": "ghost", "head": "x"}).status_code == 400
    assert c.get("/api/v1/impact-analyses/nope").status_code == 404
    assert c.delete("/api/v1/repositories/notifications").status_code == 200
    assert len(c.get("/api/v1/repositories").json()) == 5


def test_api_pr_analysis_via_remote_url(fx, tmp_path):
    """Provider 'remote' = the fixture's payments repo exposed with a refs/pull/N/head ref, cloned by URL."""
    pay = Path(fx["paths"]["payments-service"])
    git(pay, "update-ref", "refs/pull/42/head", fx["shas"]["breaking"])
    c = TestClient(make_app(tmp_path / "data", allow_local=True))
    assert c.post("/api/v1/repositories", json={"url": pay.as_uri(), "name": "payments-service"}).status_code == 200
    for name, path in fx["paths"].items():
        if name != "payments-service":
            assert c.post("/api/v1/repositories", json={"url": Path(path).as_uri(), "name": name}).status_code == 200
    r = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "pr": 42})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert r.json()["analysis_id"].startswith("tia-pr-42-") and len(rep["findings"]) == 5
    assert rep["change"]["proposed_revision"] == fx["shas"]["breaking"]


def test_repository_actions_are_audited_without_secrets(origin, tmp_path):
    st = fresh_store()
    s = R.RepoService(st, str(tmp_path / "ws"), allow_local=True)
    s.register(url=origin["url"], name="pay", token="S3CRET-TOKEN")
    s.sync("pay", token="S3CRET-TOKEN")
    s.fetch_pull_request("pay", 7, token="S3CRET-TOKEN")
    s.remove("pay")
    ev = [e["event"] for e in st.audit_events()]
    assert ev[::-1] == ["repository_registered", "repository_synced", "pull_request_fetched", "repository_removed"]
    assert "S3CRET" not in json.dumps(st.audit_events()) and st.verify_audit()["ok"]


def test_audit_endpoints(fx, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    c.post("/api/v1/repositories", json={"path": fx["paths"]["notifications"], "name": "notifications"})
    ev = c.get("/api/v1/audit").json()
    reg = next(e for e in ev if e["event"] == "repository_registered")
    assert reg["name"] == "notifications"
    assert ev[0]["event"] == "graph_version_advanced"                      # registering also advanced the landscape graph
    assert c.get("/api/v1/audit/verify").json()["ok"] is True
