"""UC1-03: job queue - idempotency, supersede, fairness, caps, retries, leases, concurrency, isolation, end to end."""
import threading
import time
import uuid

import pytest

import conftest
from conftest import make_app, new_tenant
from tia.analysis_service import make_handlers
from tia.jobs import JobQueue
from tia.worker import PermanentError, Worker


@pytest.fixture
def kind():
    """Unique job kind per test: the queue is shared across tenants/tests, so each test claims only its own work."""
    return f"k-{uuid.uuid4().hex[:8]}"


def api_q():
    return JobQueue(conftest.PG.db)


def worker_q():
    return JobQueue(conftest.PG.worker_db)


# ---- producer semantics ---------------------------------------------------------------------------------------
def test_enqueue_is_idempotent_per_tenant(kind):
    t1, t2 = new_tenant(), new_tenant()
    a = api_q().enqueue(t1, kind, {"n": 1}, key="same")
    b = api_q().enqueue(t1, kind, {"n": 2}, key="same")
    c = api_q().enqueue(t2, kind, {"n": 1}, key="same")            # another tenant may reuse the key
    assert a["deduplicated"] is False and b["deduplicated"] is True and b["job_id"] == a["job_id"]
    assert c["deduplicated"] is False and c["job_id"] != a["job_id"]
    assert api_q().get(t1, a["job_id"])["status"] == "queued"
    assert api_q().get(t2, a["job_id"]) is None                      # tenant 2 cannot see tenant 1's job


def test_newest_submission_wins_within_a_group(kind):
    t, q = new_tenant(), api_q()
    j1 = q.enqueue(t, kind, {}, key="k1", group_key="pr:x:7")["job_id"]
    j2 = q.enqueue(t, kind, {}, key="k2", group_key="pr:x:7")["job_id"]
    other = q.enqueue(t, kind, {}, key="k3", group_key="pr:x:8")["job_id"]
    assert q.get(t, j1)["status"] == "superseded" and q.get(t, j2)["status"] == "queued"
    assert q.get(t, other)["status"] == "queued"                       # other PR untouched
    assert q.is_latest(t, j2) and not q.is_latest(t, j1) and q.is_latest(t, other)


def test_running_job_is_told_to_stop_when_superseded(kind):
    t = new_tenant()
    old = api_q().enqueue(t, kind, {}, key="old", group_key="g")["job_id"]
    job = worker_q().claim("w1", kinds=[kind])
    assert job.id == old and api_q().get(t, old)["status"] == "running"
    api_q().enqueue(t, kind, {}, key="new", group_key="g")
    assert api_q().get(t, old)["cancel_requested"] is True
    assert worker_q().heartbeat(job) is False                          # worker learns it should stop
    assert api_q().is_latest(t, old) is False


def test_cancel(kind):
    t, q = new_tenant(), api_q()
    j = q.enqueue(t, kind, {}, key="a")["job_id"]
    assert q.cancel(t, j) == "cancelled" and q.get(t, j)["status"] == "cancelled"
    j2 = q.enqueue(t, kind, {}, key="b")["job_id"]
    worker_q().claim("w", kinds=[kind])
    assert q.cancel(t, j2) == "cancel_requested"
    assert q.cancel(t, str(uuid.uuid4())) is None
    assert q.cancel(new_tenant(), j2) is None                          # cannot cancel another tenant's job


def test_force_requeues_a_failed_job(kind):
    t, q = new_tenant(), api_q()
    j = q.enqueue(t, kind, {"v": 1}, key="k", max_attempts=1)["job_id"]
    job = worker_q().claim("w", kinds=[kind]); worker_q().fail(job, "boom")
    assert q.get(t, j)["status"] == "failed"
    assert q.enqueue(t, kind, {"v": 1}, key="k")["deduplicated"] is True          # without force: still the failed job
    again = q.enqueue(t, kind, {"v": 2}, key="k", force=True)
    assert again["job_id"] == j and q.get(t, j)["status"] == "queued" and q.get(t, j)["attempts"] == 0


# ---- isolation between roles / tenants -------------------------------------------------------------------------
def test_api_role_cannot_see_or_claim_other_tenants_work(kind):
    import psycopg
    t = new_tenant(); api_q().enqueue(t, kind, {}, key="a")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):           # the API role cannot even run the claim: it may not ask whether
        api_q().claim("api-process", kinds=[kind])                      # an arbitrary tenant is active (tia_tenant_active is worker-only)
    assert worker_q().claim("worker", kinds=[kind]) is not None        # tia_worker role can


def test_worker_still_tenant_bound_for_tenant_data(kind):
    """Seeing jobs across tenants must not grant access to other tables."""
    import psycopg
    t = new_tenant()
    with psycopg.connect(conftest.PG.worker_dsn) as c:
        assert c.execute("select count(*) from repositories").fetchone()[0] == 0      # no tenant bound -> nothing
        assert c.execute("select count(*) from audit_log").fetchone()[0] == 0


# ---- fairness and caps ------------------------------------------------------------------------------------------
def test_one_tenant_flooding_does_not_starve_another(kind):
    flood, small = new_tenant(), new_tenant()
    for i in range(12):
        api_q().enqueue(flood, kind, {"i": i}, key=f"f{i}")
    small_id = api_q().enqueue(small, kind, {}, key="s")["job_id"]
    order = []
    for _ in range(3):
        order.append(worker_q().claim("w", per_tenant_cap=50, kinds=[kind]))
    assert small_id in [j.id for j in order[:2]], "the small tenant's job must be served within the first two claims"


def test_per_tenant_concurrency_cap_is_exact(kind):
    t = new_tenant()
    for i in range(6):
        api_q().enqueue(t, kind, {}, key=f"k{i}")
    got = [worker_q().claim("w", per_tenant_cap=2, kinds=[kind]) for _ in range(4)]
    assert [g is not None for g in got] == [True, True, False, False]
    worker_q().succeed(got[0], {})                                      # a slot frees up
    assert worker_q().claim("w", per_tenant_cap=2, kinds=[kind]) is not None


def test_cap_holds_under_concurrent_claims(kind):
    t = new_tenant()
    for i in range(20):
        api_q().enqueue(t, kind, {}, key=f"k{i}")
    claimed, lock = [], threading.Lock()

    def go():
        j = worker_q().claim("w", per_tenant_cap=3, kinds=[kind])
        if j:
            with lock:
                claimed.append(j.id)

    ts = [threading.Thread(target=go) for _ in range(12)]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert len(claimed) == 3 and len(set(claimed)) == 3


# ---- reliability ---------------------------------------------------------------------------------------------------
def test_retry_then_fail_and_permanent_errors(kind):
    t = new_tenant(); calls = []

    def boom(job):
        calls.append(job.attempts); raise RuntimeError("transient")

    j = api_q().enqueue(t, kind, {}, key="r", max_attempts=3)["job_id"]
    w = Worker(worker_q(), {kind: boom}, base_delay_s=0)
    while w.run_once():
        pass
    s = api_q().get(t, j)
    assert calls == [1, 2, 3] and s["status"] == "failed" and "transient" in s["error"] and s["attempts"] == 3

    p = api_q().enqueue(t, kind, {}, key="p", max_attempts=5)["job_id"]
    perm = Worker(worker_q(), {kind: lambda job: (_ for _ in ()).throw(PermanentError("bad input"))}, base_delay_s=0)
    while perm.run_once():
        pass
    s = api_q().get(t, p)
    assert s["status"] == "failed" and s["attempts"] == 1 and s["error"] == "bad input"


def test_backoff_delays_the_retry(kind):
    t = new_tenant()
    j = api_q().enqueue(t, kind, {}, key="b", max_attempts=3)["job_id"]
    w = Worker(worker_q(), {kind: lambda job: (_ for _ in ()).throw(RuntimeError("x"))}, base_delay_s=30)
    assert w.run_once() is True
    assert api_q().get(t, j)["status"] == "queued"
    assert worker_q().claim("w", kinds=[kind]) is None                  # not yet: run_after is ~30s away


def test_success_stores_result(kind):
    t = new_tenant(); j = api_q().enqueue(t, kind, {"x": 2}, key="ok")["job_id"]
    Worker(worker_q(), {kind: lambda job: {"double": job.payload["x"] * 2}}).run_once()
    s = api_q().get(t, j)
    assert s["status"] == "succeeded" and s["result"] == {"double": 4} and s["error"] is None


def test_expired_lease_is_recovered_and_stale_worker_cannot_complete(kind):
    t = new_tenant(); j = api_q().enqueue(t, kind, {}, key="l", max_attempts=3)["job_id"]
    dead = worker_q().claim("dead-worker", lease_s=0.05, kinds=[kind])
    time.sleep(0.3)
    assert worker_q().reap_expired() >= 1
    assert api_q().get(t, j)["status"] == "queued"
    alive = worker_q().claim("alive-worker", kinds=[kind])
    assert alive.id == j and alive.attempts == 2
    worker_q().succeed(dead, {"stale": True})                           # the lost worker wakes up late: ignored
    assert api_q().get(t, j)["status"] == "running"
    worker_q().succeed(alive, {"ok": True})
    assert api_q().get(t, j)["result"] == {"ok": True}


def test_lease_expiry_on_last_attempt_fails_the_job(kind):
    t = new_tenant(); j = api_q().enqueue(t, kind, {}, key="l1", max_attempts=1)["job_id"]
    worker_q().claim("w", lease_s=0.05, kinds=[kind]); time.sleep(0.3)
    worker_q().reap_expired()
    assert api_q().get(t, j)["status"] == "failed"


def test_heartbeat_extends_lease(kind):
    t = new_tenant(); api_q().enqueue(t, kind, {}, key="h")
    job = worker_q().claim("w", lease_s=0.3, kinds=[kind])
    assert worker_q().heartbeat(job, lease_s=60) is True
    time.sleep(0.5)
    assert worker_q().reap_expired() == 0                               # still leased


def test_many_workers_process_every_job_exactly_once(kind):
    tenants = [new_tenant() for _ in range(3)]
    n = 0
    for t in tenants:
        for i in range(10):
            api_q().enqueue(t, kind, {"i": i, "t": t}, key=f"k{i}"); n += 1
    seen, lock = [], threading.Lock()

    def handler(job):
        with lock:
            seen.append(job.id)
        time.sleep(0.01)
        return {"ok": True}

    def loop():
        w = Worker(worker_q(), {kind: handler}, per_tenant_cap=3)
        while w.run_once():
            pass

    ts = [threading.Thread(target=loop) for _ in range(6)]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert len(seen) == n and len(set(seen)) == n
    assert all(api_q().get(t, jid)["status"] == "succeeded" for t in tenants[:1] for jid in set(seen)
               if api_q().get(t, jid))


# ---- end to end: API -> queue -> worker -> report -----------------------------------------------------------------
def drain(handlers):
    w = Worker(worker_q(), handlers, base_delay_s=0)
    while w.run_once():
        pass


def test_async_analysis_end_to_end(fx, tmp_path):
    from fastapi.testclient import TestClient
    tenant = new_tenant()
    from tia.api import create_app
    c = TestClient(create_app(conftest.PG.db, tenant, str(tmp_path / "ws"), allow_local=True))
    for n, p in fx["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    body = {"target": "payments-service", "head": "breaking", "base": "main"}
    r = c.post("/api/v1/impact-analyses?async=true", json=body)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["deduplicated"] is False
    again = c.post("/api/v1/impact-analyses?async=true", json=body).json()
    assert again["deduplicated"] is True and again["job_id"] == job["job_id"]       # idempotent resubmission

    assert c.get(f"/api/v1/jobs/{job['job_id']}").json()["status"] == "queued"
    drain(make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(tmp_path / "ws")))
    done = c.get(f"/api/v1/jobs/{job['job_id']}").json()
    assert done["status"] == "succeeded" and done["result"]["status"] == "complete_with_gaps", done
    rep = c.get(f"/api/v1/impact-analyses/{done['result']['analysis_id']}").json()
    assert len(rep["findings"]) == 5
    sync = c.post("/api/v1/impact-analyses", json=body).json()
    assert sync["analysis_id"] == done["result"]["analysis_id"]                     # queued and sync agree exactly
    assert c.post("/api/v1/impact-analyses?async=true", json={**body, "token": "x"}).status_code == 400
    assert c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "pr": 5}).status_code == 400
    assert c.get("/api/v1/jobs/" + str(uuid.uuid4())).status_code == 404
    audit = [e["event"] for e in c.get("/api/v1/audit").json()]
    assert "analysis_queued" in audit and "analysis_completed" in audit
    assert c.get("/api/v1/audit/verify").json()["ok"] is True


def test_resubmitting_a_pull_request_supersedes_the_older_run(fx, tmp_path):
    from fastapi.testclient import TestClient
    from tia.api import create_app
    c = TestClient(create_app(conftest.PG.db, new_tenant(), str(tmp_path / "ws"), allow_local=True))
    for n, p in fx["paths"].items():
        c.post("/api/v1/repositories", json={"path": p, "name": n})
    a = c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": "breaking"}).json()
    b = c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": "breaking"}).json()
    assert b["deduplicated"] is True
    older = c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": "impl"}).json()
    newer = c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": "impl", "max_depth": 3}).json()
    # same group (same head ref 'impl') with different parameters: the older queued job is superseded...
    assert c.get(f"/api/v1/jobs/{older['job_id']}").json()["status"] == "superseded"
    assert c.get(f"/api/v1/jobs/{newer['job_id']}").json()["status"] == "queued"
    # ...while the other group ('breaking') is untouched by it
    assert c.get(f"/api/v1/jobs/{a['job_id']}").json()["status"] == "queued"
    # same group again (same head ref 'breaking') with different parameters supersedes 'a'
    a2 = c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": "breaking", "max_depth": 7}).json()
    s = c.get(f"/api/v1/jobs/{a['job_id']}").json()
    assert s["status"] == "superseded" and s["is_latest"] is False
    assert c.get(f"/api/v1/jobs/{a2['job_id']}").json()["is_latest"] is True
    drain(make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(tmp_path / "ws")))
    assert c.get(f"/api/v1/jobs/{a['job_id']}").json()["status"] == "superseded"        # never ran
    assert c.get(f"/api/v1/jobs/{a2['job_id']}").json()["status"] == "succeeded"
    assert c.delete(f"/api/v1/jobs/{a2['job_id']}").status_code == 409                  # finished jobs cannot be cancelled


def test_handler_failure_for_unknown_repository_is_permanent(tmp_path):
    t = new_tenant()
    concrete = {"target": "ghost", "base": "x", "head": "y", "scope": ["ghost"], "revisions": {}, "pr": None,
                "change_record": None, "max_depth": 8, "max_results": 200}
    j = api_q().enqueue(t, "impact_analysis", concrete, key=uuid.uuid4().hex)["job_id"]
    drain(make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(tmp_path / "ws")))
    s = api_q().get(t, j)
    assert s["status"] == "failed" and s["attempts"] == 1 and "not registered" in s["error"]
