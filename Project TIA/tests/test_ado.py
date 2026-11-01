"""UC1-06 / UC1-15: Azure DevOps service hooks, pull-request thread and status - real PostgreSQL, real git remotes, an Azure DevOps REST stand-in."""
import base64
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakeado import FakeAdo  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import bearer, make_auth_app, tenant_named, token_for  # noqa: E402
from test_auth_api import audit  # noqa: E402
from tia import ado  # noqa: E402
from tia.ado import AdoConfig, AdoError  # noqa: E402
from tia.analysis_service import make_handlers  # noqa: E402
from tia.api import create_app  # noqa: E402
from tia.jobs import JobQueue  # noqa: E402
from tia.report import MARKER  # noqa: E402
from tia.store import Store  # noqa: E402
from tia.tenants import Tenants  # noqa: E402
from tia.worker import Worker  # noqa: E402

USER, PW = "tia-hook", "hook-password-123"


def basic(u=USER, p=PW):
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()


@pytest.fixture
def fake():
    f = FakeAdo()
    yield f
    f.close()


def cfg_for(fake, **kw):
    return AdoConfig(hook_username=USER, hook_password=PW, token=fake.token, api_url=fake.url, **kw)


def event(base_url, remote_url, pr=5, head=None, kind="git.pullrequest.created", status="active", draft=False, collection="coll-1"):
    return {"eventType": kind, "resource": {"pullRequestId": pr, "status": status, "isDraft": draft, "sourceRefName": "refs/heads/feature",
                                            "targetRefName": "refs/heads/main", "lastMergeSourceCommit": {"commitId": head or "a" * 40},
                                            "repository": {"id": "repo-guid", "name": "payments-service", "project": {"name": "Payments"}, "remoteUrl": remote_url}},
            "resourceContainers": {"collection": {"id": collection, "baseUrl": base_url + "/"}}}


# ---- pure parts -------------------------------------------------------------------------------------------------------------------------
def test_service_hook_credentials():
    c = AdoConfig(hook_username=USER, hook_password=PW)
    assert ado.verify_basic(c, basic())
    for bad in (basic(USER, "wrong"), basic("other", PW), None, "", "Bearer x", "Basic !!!", "Basic " + base64.b64encode(b"nocolon").decode()):
        assert not ado.verify_basic(c, bad), bad
    assert not ado.verify_basic(AdoConfig(), basic())                                     # not configured: never valid


def test_event_parsing():
    e = ado.parse_pull_request_event(event("https://dev.azure.com/acme", "https://dev.azure.com/acme/Payments/_git/payments-service"))
    assert e["pr"] == 5 and e["base_ref"] == "main" and e["head_sha"] == "a" * 40 and e["project"] == "Payments" and e["base_url"] == "https://dev.azure.com/acme"
    assert ado.parse_pull_request_event(event("https://x", "u", kind="git.pullrequest.updated"))["pr"] == 5
    for skip in (event("https://x", "u", status="completed"), event("https://x", "u", status="abandoned"), event("https://x", "u", draft=True),
                 {"eventType": "git.push"}, {}):
        assert ado.parse_pull_request_event(skip) is None
    with pytest.raises(ValueError):
        ado.parse_pull_request_event({"eventType": "git.pullrequest.created", "resource": {}})


@pytest.mark.parametrize("url,ok", [("https://dev.azure.com/acme", True), ("https://acme.visualstudio.com", True), ("http://dev.azure.com/acme", False),
                                    ("https://evil.example/acme", False), ("https://dev.azure.com.evil.example/x", False)])
def test_the_token_goes_only_to_azure_devops_hosts(url, ok):
    assert ado.allowed_host(AdoConfig(), url) is ok


def test_an_on_premises_server_is_allowed_when_configured():
    c = AdoConfig(api_url="https://tfs.bank.example/tfs")
    assert ado.allowed_host(c, "https://tfs.bank.example/tfs/DefaultCollection") and not ado.allowed_host(c, "https://other.example")
    with pytest.raises(AdoError, match="refusing"):
        ado._call(AdoConfig(), "GET", "https://evil.example/x", "pat")


@pytest.mark.parametrize("url,want", [
    ("https://dev.azure.com/acme/Payments/_git/payments-service", {"base_url": "https://dev.azure.com/acme", "project": "Payments", "repository": "payments-service"}),
    ("https://acme.visualstudio.com/Payments/_git/pay", {"base_url": "https://acme.visualstudio.com", "project": "Payments", "repository": "pay"}),
    ("https://tfs.bank.example/tfs/DefaultCollection/Proj/_git/r", {"base_url": "https://tfs.bank.example/tfs/DefaultCollection", "project": "Proj", "repository": "r"}),
    ("https://github.com/a/b", None), ("not a url", None)])
def test_remote_parsing(url, want):
    assert ado.parse_remote(url) == want


def test_one_thread_updated_in_place_and_an_advisory_status(fake):
    c = cfg_for(fake)
    rep = {"analysis_id": "a1", "status": "complete", "summary": {"material_findings": 2}}
    base = fake.url + "/acme"
    assert ado.upsert_thread(c, fake.token, base, "Payments", "r", 5, MARKER + "\nfirst") == "created"
    assert ado.upsert_thread(c, fake.token, base, "Payments", "r", 5, MARKER + "\nsecond") == "updated"
    threads = fake.threads[("Payments", "r", 5)]
    assert len(threads) == 1 and threads[0]["comments"][0]["content"].endswith("second")
    assert threads[0]["status"] == "closed"                                                       # never blocks a "resolve all comments" policy
    ado.post_status(c, fake.token, base, "Payments", "r", 5, rep, "https://tia.example.com/analyses/a1")
    st = fake.statuses[-1]
    assert st["state"] == "notApplicable" and st["context"] == {"name": "impact-analysis", "genre": "tia"} and "Advisory" in st["description"]


def test_permission_failures_are_permanent_and_explained(fake):
    with pytest.raises(AdoError) as e:
        ado.upsert_thread(cfg_for(fake), "wrong-pat", fake.url + "/acme", "P", "r", 1, "x")
    assert e.value.permanent and "401" in str(e.value) and "Pull request threads" in str(e.value)
    fake.fail_status = 503
    with pytest.raises(AdoError) as e:
        ado.upsert_thread(cfg_for(fake), fake.token, fake.url + "/acme", "P", "r", 1, "x")
    assert not e.value.permanent


# ---- receiving ------------------------------------------------------------------------------------------------------------------------
def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def world(fx, fake, tmp_path):
    w = type("W", (), {})()
    w.fx, w.fake, w.tmp = fx, fake, tmp_path
    origins = tmp_path / "origins"
    origins.mkdir()
    w.bare = origins / "payments-service.git"
    git(origins, "clone", "-q", "--bare", fx["paths"]["payments-service"], str(w.bare))
    git(w.bare, "update-ref", "refs/pull/5/merge", fx["shas"]["breaking"])
    w.cfg = cfg_for(fake)
    w.tenant = conftest.new_tenant()
    w.app = create_app(conftest.PG.db, w.tenant, str(tmp_path / "ws"), allow_local=True, ado_cfg=w.cfg)
    w.c = TestClient(w.app)
    for n, p in fx["paths"].items():
        if n != "payments-service":
            assert w.c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    assert w.c.post("/api/v1/repositories", json={"url": w.bare.as_uri(), "name": "payments-service"}).status_code == 200
    w.event = lambda **kw: event(fake.url + "/acme", w.bare.as_uri(), head=fx["shas"]["breaking"], **kw)
    w.post = lambda payload, auth=basic(): w.c.post("/api/v1/webhooks/ado", content=json.dumps(payload).encode(),
                                                     headers={"Authorization": auth, "Content-Type": "application/json"})
    return w


def queued(tenant):
    with conftest.PG.db.tx(tenant) as c:
        return [(r[0], r[1]) for r in c.execute("select kind, payload from jobs where tenant_id = %s", (tenant,))]


def test_receiver_off_without_credentials(tmp_path):
    c = TestClient(create_app(conftest.PG.db, conftest.new_tenant(), str(tmp_path / "ws")))
    assert c.post("/api/v1/webhooks/ado", json={}).status_code == 503


def test_bad_credentials_are_refused_and_not_audited(world):
    before = len(audit(world.tenant))
    assert world.post(world.event(), auth=basic(USER, "nope")).status_code == 401
    assert world.c.post("/api/v1/webhooks/ado", json=world.event()).status_code == 401
    assert len(audit(world.tenant)) == before and queued(world.tenant) == []


def test_an_accepted_event_queues_one_job_and_redelivery_is_deduplicated(world):
    r = world.post(world.event())
    assert r.status_code == 202 and r.json()["pr"] == 5
    again = world.post(world.event(kind="git.pullrequest.updated"))
    assert again.json()["deduplicated"] is True
    jobs = queued(world.tenant)
    assert len(jobs) == 1 and jobs[0][0] == "ado_pull_request" and jobs[0][1]["ado"]["project"] == "Payments"
    assert "ado-pat" not in json.dumps(jobs)                                                      # no credential in the queue


def test_events_that_need_nothing(world):
    assert world.post(world.event(status="completed")).status_code == 202
    other = event(world.fake.url + "/acme", "https://dev.azure.com/acme/X/_git/unknown")
    r = world.post(other)
    assert r.status_code == 202 and "not registered" in r.json()["ignored"]
    assert audit(world.tenant, "webhook_ignored")
    assert world.post({"eventType": "git.pullrequest.created", "resource": {}}).status_code == 400
    evil = event("https://evil.example/acme", world.bare.as_uri())
    assert world.post(evil).status_code == 400                                                    # never told to call a non-ADO host


def test_a_mapped_organisation_routes_events_to_its_tenant(world):
    tid, name = tenant_named()
    Tenants(conftest.PG.admin).map_installation(name, "ado", "coll-mapped-" + uuid.uuid4().hex[:6])
    coll = Tenants(conftest.PG.admin).installations(name)[0]["installation_id"]
    st = Store(conftest.PG.db, tid)
    from tia.repos import RepoService
    RepoService(st, str(world.tmp / "ws2"), allow_local=True).register(url=world.bare.as_uri(), name="payments-service")
    r = world.post(world.event(collection=coll))
    assert r.status_code == 202 and len(queued(tid)) == 1 and queued(world.tenant) == []


# ---- the job, end to end -------------------------------------------------------------------------------------------------------------
def drain(world):
    h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(world.tmp / "ws"), ado_cfg=world.cfg)
    w = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
    while w.run_once():
        pass
    with conftest.PG.db.tx(world.tenant) as c:
        return c.execute("select status, error, result from jobs where tenant_id = %s order by created_at desc", (world.tenant,)).fetchall()


def test_pull_request_to_thread_and_status(world):
    world.post(world.event())
    (status, error, result), = drain(world)
    assert status == "succeeded", error
    assert result["comment"] == "created" and result["pr_status"] == "created"
    t = world.fake.threads[("Payments", "repo-guid", 5)]
    assert len(t) == 1 and MARKER in t[0]["comments"][0]["content"] and "payments-service" in t[0]["comments"][0]["content"]
    assert world.fake.statuses[-1]["state"] == "notApplicable"
    ev = [e["event"] for e in audit(world.tenant)]
    assert "pr_comment_published" in ev


def test_a_new_push_updates_the_same_thread(world):
    world.post(world.event())
    drain(world)
    git(world.bare, "update-ref", "refs/pull/5/merge", world.fx["shas"]["impl"])
    world.post(world.event(kind="git.pullrequest.updated") | {"resource": {**world.event()["resource"], "lastMergeSourceCommit": {"commitId": world.fx["shas"]["impl"]}}})
    rows = drain(world)
    assert [r[0] for r in rows] == ["succeeded", "succeeded"]
    assert len(world.fake.threads[("Payments", "repo-guid", 5)]) == 1 and rows[0][2]["comment"] == "updated"


def test_a_bad_token_fails_the_job_permanently_with_the_reason(world):
    world.cfg = cfg_for(world.fake).__class__(hook_username=USER, hook_password=PW, token="wrong", api_url=world.fake.url)
    world.post(world.event())
    (status, error, _), = drain(world)
    assert status == "failed" and "401" in error and "wrong" not in error


def test_by_default_the_thread_names_only_the_pull_requests_repository(world):
    world.post(world.event())
    drain(world)
    body = world.fake.threads[("Payments", "repo-guid", 5)][0]["comments"][0]["content"]
    for other in ("reconciliation-service", "mobile-bff", "ledger-sync"):
        assert other not in body


def test_manual_publish_to_an_azure_devops_pull_request(world, fx):
    app, tid = make_auth_app(world.tmp / "m", ado_cfg=world.cfg)
    c = TestClient(app)
    h = bearer(token_for(tid, ["engineer"]))
    for n, p in fx["paths"].items():
        c.post("/api/v1/repositories", headers=h, json={"path": p, "name": n})
    st = Store(conftest.PG.db, tid)
    r = st.get_repo("payments-service")
    r.provider, r.url = "ado", world.fake.url + "/acme/Payments/_git/payments-service"
    st.put_repo(r)
    acc = __import__("tia.access", fromlist=["AccessStore"]).AccessStore(conftest.PG.db, tid)
    acc.set_mode("payments-service", "tenant", "test")                                          # Azure DevOps repositories are restricted by default
    aid = c.post("/api/v1/impact-analyses", headers=h, json={"target": "payments-service", "head": "breaking"}).json()["analysis_id"]
    res = c.post(f"/api/v1/impact-analyses/{aid}/publish", headers=h, json={"repository": "payments-service", "pr": 9})
    assert res.status_code == 200, res.text
    assert res.json()["comment"] == "created" and ("Payments", "payments-service", 9) in world.fake.threads
