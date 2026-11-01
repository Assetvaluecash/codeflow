"""UC1-20: tenant lifecycle (provision, suspend/resume, SCM installation mapping, storage quota, offboarding with verified deletion),
against real PostgreSQL with real role separation. The tenant registry is administered with admin credentials; the runtime and
worker roles only get the two narrow functions the migration grants."""
import io
import json
import os
import stat
import uuid
import zipfile
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

import conftest
from tia.api import create_app
from tia.db import RUNTIME_TABLES, TenantAdmin
from tia.jobs import JobQueue
from tia.tenants import TenantError, Tenants, certificate_valid, dir_size, main as tenants_cli, remove_workspace, tenant_tables


def admin() -> Tenants:
    return Tenants(conftest.PG.admin)


def uniq(prefix="tn") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Live:
    """A tenant with its own app (own workspace) on the shared PostgreSQL."""

    def __init__(self, tmp_path, fx, name=None, ttl=0, **kw):
        self.name = name or uniq()
        self.t = admin().create(self.name)
        self.id = self.t["id"]
        self.ws = tmp_path / f"ws-{self.name}"
        self.fx = fx
        self.c = TestClient(create_app(conftest.PG.db, self.id, str(self.ws), allow_local=True, gate_ttl=ttl, **kw))

    def populate(self, repos=("payments-service", "mobile-bff"), analyse=True, upload=True):
        for n in repos:
            assert self.c.post("/api/v1/repositories", json={"path": self.fx["paths"][n], "name": n}).status_code == 200
        out = {}
        if analyse:
            r = self.c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": self.fx["shas"]["breaking"], "base": "main"})
            assert r.status_code == 200, r.text
            out["analysis"] = r.json()["analysis_id"]
            q = self.c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": self.fx["shas"]["impl"], "base": "main"})
            assert q.status_code == 202, q.text
            out["job"] = q.json()["job_id"]
        if upload:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("app.py", "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/x')\ndef x():\n    return 1\n")
            r = self.c.put("/api/v1/repositories/uploaded/upload", content=buf.getvalue())
            assert r.status_code == 200, r.text
        return out


@pytest.fixture
def live(fx, tmp_path):
    return lambda **kw: Live(tmp_path, fx, **kw)


def rows(tenant_id):
    """Rows per tenant table as the ADMIN sees them."""
    with psycopg.connect(conftest.PG.admin) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (tenant_id,))
        return {t: n for t in tenant_tables(c) if (n := c.execute(f'select count(*) from "{t}" where tenant_id = %s', (tenant_id,)).fetchone()[0])}


# ---- provisioning ----------------------------------------------------------------------------------------------------------
def test_create_validates_and_names_are_never_reused():
    a = admin()
    t = a.create(uniq(), display_name="Acme Bank", plan="enterprise", storage_quota_bytes=10 ** 9)
    assert t["status"] == "active" and t["display_name"] == "Acme Bank" and t["plan"] == "enterprise" and t["storage_quota_bytes"] == 10 ** 9
    assert a.get(t["name"])["id"] == t["id"] == a.get(t["id"])["id"] and t["name"] in [x["name"] for x in a.list()]
    for bad in ("", " x", "-x", "a b", "a/b", "x" * 64, "é"):
        with pytest.raises(TenantError, match="tenant name must be"):
            a.create(bad)
    with pytest.raises(TenantError, match="already exists"):
        a.create(t["name"])
    with pytest.raises(TenantError, match="negative"):
        a.create(uniq(), storage_quota_bytes=-1)
    with pytest.raises(TenantError, match="unknown tenant"):
        a.get("nope-" + uniq())
    with pytest.raises(TenantError, match="unknown tenant"):
        a.get(str(uuid.uuid4()))


def test_lifecycle_transitions_are_guarded_and_logged():
    a, t = admin(), admin().create(uniq())
    with pytest.raises(TenantError, match="only a suspended tenant can be resumed"):
        a.resume(t["name"])
    with pytest.raises(TenantError, match="reason is required"):
        a.suspend(t["name"], "  ")
    s = a.suspend(t["name"], "unpaid invoice")
    assert s["status"] == "suspended" and s["suspended_reason"] == "unpaid invoice" and s["suspended_at"]
    with pytest.raises(TenantError, match="only an active tenant can be suspended"):
        a.suspend(t["name"], "again")
    r = a.resume(t["name"])
    assert r["status"] == "active" and r["suspended_reason"] is None and r["suspended_at"] is None
    a.set_quota(t["name"], 5000)
    a.set_quota(t["name"], None)
    ev = a.events(t["name"])
    assert [e["event"] for e in ev] == ["tenant_created", "tenant_suspended", "tenant_resumed", "quota_changed", "quota_changed"]
    assert ev[1]["detail"] == {"reason": "unpaid invoice"} and ev[3]["detail"] == {"old": None, "new": 5000} and ev[4]["detail"] == {"old": 5000, "new": None}
    assert [e["id"] for e in ev] == sorted(e["id"] for e in ev)


# ---- suspension is enforced on the request path ---------------------------------------------------------------------------------
def test_suspension_blocks_the_api_page_and_webhook_and_resume_restores_them(live):
    a, b = live(), live()
    made = a.populate(upload=False)
    assert a.c.get("/api/v1/repositories").status_code == 200 and a.c.get(f"/analyses/{made['analysis']}").status_code == 200
    admin().suspend(a.name, "investigation")
    for r in (a.c.get("/api/v1/repositories"), a.c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "main"}),
              a.c.post("/api/v1/webhooks/github", content=b"{}"), a.c.get("/api/v1/audit"), a.c.get(f"/analyses/{made['analysis']}")):
        assert r.status_code == 403 and r.headers["x-tia-code"] == "tenant_suspended", r.request.url
    assert a.c.get("/api/v1/repositories").json()["code"] == "tenant_suspended" and "suspended" in a.c.get("/api/v1/tenant").json()["detail"]
    assert a.c.get("/api/v1/health").status_code == 200                         # liveness is not tenant business
    assert a.c.get("/").status_code == 200                                      # the static shell loads; its API calls show the reason
    assert b.c.get("/api/v1/repositories").status_code == 200                   # another tenant is unaffected
    admin().resume(a.name)
    assert a.c.get("/api/v1/repositories").status_code == 200 and a.c.get(f"/analyses/{made['analysis']}").status_code == 200


def test_suspension_takes_effect_within_the_cache_ttl(fx, tmp_path):
    t = Live(tmp_path, fx, ttl=1.0)
    assert t.c.get("/api/v1/repositories").status_code == 200
    admin().suspend(t.name, "x")
    import time
    deadline = time.time() + 5
    while time.time() < deadline and t.c.get("/api/v1/repositories").status_code == 200:
        time.sleep(0.2)
    assert t.c.get("/api/v1/repositories").status_code == 403


def test_a_suspended_tenants_jobs_are_not_claimed_until_it_resumes(live):
    a, b = live(), live()
    kind = "kind-" + uuid.uuid4().hex
    app_q, worker_q = JobQueue(conftest.PG.db), JobQueue(conftest.PG.worker_db)
    ja = app_q.enqueue(a.id, kind, {}, key="a")["job_id"]
    jb = app_q.enqueue(b.id, kind, {}, key="b")["job_id"]
    admin().suspend(a.name, "x")
    first = worker_q.claim("w1", kinds=[kind])
    assert first and first.tenant_id == b.id and first.id == jb                # the active tenant's job runs
    assert worker_q.claim("w1", kinds=[kind]) is None                          # the suspended tenant's job waits
    admin().resume(a.name)
    again = worker_q.claim("w1", kinds=[kind])
    assert again and again.id == ja


def test_runtime_roles_cannot_read_the_registry_only_their_own_state(live):
    a = live()
    admin().set_quota(a.name, 1234)
    with psycopg.connect(conftest.PG.app_dsn) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from tenants")
    with psycopg.connect(conftest.PG.app_dsn) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from tenant_installations")
    with psycopg.connect(conftest.PG.app_dsn) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select tia_tenant_active(%s)", (a.id,))                  # only workers may ask about an arbitrary tenant
    with psycopg.connect(conftest.PG.app_dsn) as c:
        assert c.execute("select * from tia_tenant_state()").fetchall() == []   # no tenant bound: nothing to say
        c.execute("select set_config('tia.tenant_id', %s, false)", (a.id,))
        assert c.execute("select status, storage_quota_bytes from tia_tenant_state()").fetchall() == [("active", 1234)]
    with psycopg.connect(conftest.PG.worker_dsn) as c:
        assert c.execute("select tia_tenant_active(%s)", (a.id,)).fetchone()[0] is True
        assert c.execute("select tia_tenant_active(%s)", (str(uuid.uuid4()),)).fetchone()[0] is False
    with psycopg.connect(conftest.PG.app_dsn) as c:                             # the registry's lifecycle log is admin-only too
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from tenant_events")


# ---- storage quota ---------------------------------------------------------------------------------------------------------------
def test_quota_refuses_growth_once_used_up_and_allows_it_again_when_raised(live):
    a = live()
    a.populate(upload=False)
    st = a.c.get("/api/v1/tenant").json()
    assert st["status"] == "active" and st["storage_quota_bytes"] is None and st["storage_used_bytes"] > 0 and st["approximate"] is True
    assert st["storage_used_bytes"] == st["database_bytes"] + st["workspace_bytes"] and st["storage_used_fraction"] is None
    used = st["storage_used_bytes"]

    admin().set_quota(a.name, used // 2)                                        # already over
    over = a.c.get("/api/v1/tenant").json()
    assert over["storage_used_fraction"] > 1
    body = {"target": "payments-service", "head": a.fx["shas"]["impl"], "base": "main"}
    for r in (a.c.post("/api/v1/impact-analyses", json=body), a.c.post("/api/v1/impact-analyses?async=true", json=body),
              a.c.post("/api/v1/repositories", json={"path": a.fx["paths"]["ledger-sync"], "name": "ledger-sync"}),
              a.c.post("/api/v1/repositories/payments-service/sync", json={}), a.c.post("/api/v1/uploads", json={"name": "u"})):
        assert r.status_code == 403 and r.headers["x-tia-code"] == "storage_quota_exceeded", r.request.url
        assert "storage quota exceeded" in r.json()["detail"]
    assert a.c.get("/api/v1/repositories").status_code == 200                   # reading is never refused
    assert a.c.get("/api/v1/impact-analyses/" + "tia-x").status_code == 404

    admin().set_quota(a.name, used * 100)
    a.c.get("/api/v1/tenant")
    assert a.c.post("/api/v1/impact-analyses", json=body).status_code == 200
    admin().set_quota(a.name, None)
    assert a.c.get("/api/v1/tenant").json()["storage_quota_bytes"] is None


def test_an_upload_larger_than_the_room_left_is_refused_before_it_is_read(live):
    a = live()
    a.populate(analyse=False, upload=False)
    used = a.c.get("/api/v1/tenant").json()["storage_used_bytes"]
    admin().set_quota(a.name, used + 1000)
    a.c.get("/api/v1/tenant")
    junk = b"x" * 5000
    r = a.c.put("/api/v1/repositories/big/upload", content=junk)
    assert r.status_code == 403 and r.headers["x-tia-code"] == "storage_quota_exceeded" and "5000 more requested" in r.json()["detail"]
    sid = a.c.post("/api/v1/uploads", json={"name": "big2"}).json()["upload_id"]
    r = a.c.put(f"/api/v1/uploads/{sid}/archive", content=junk)
    assert r.status_code == 403 and r.headers["x-tia-code"] == "storage_quota_exceeded"
    admin().set_quota(a.name, None)
    r = a.c.put("/api/v1/repositories/big/upload", content=junk)                # the gate no longer objects: the zip itself is bad
    assert r.status_code == 400 and "zip" in r.json()["detail"].lower()


def test_default_quota_applies_when_the_registry_sets_none(fx, tmp_path):
    t = Live(tmp_path, fx, default_quota=1)
    assert t.c.get("/api/v1/tenant").json()["storage_quota_bytes"] == 1 and t.c.get("/api/v1/tenant").json()["storage_used_bytes"] == 0
    assert t.c.post("/api/v1/repositories", json={"path": fx["paths"]["payments-service"], "name": "p"}).status_code == 200   # empty: room
    r2 = t.c.post("/api/v1/repositories", json={"path": fx["paths"]["mobile-bff"], "name": "m"})
    assert r2.status_code == 403 and r2.headers["x-tia-code"] == "storage_quota_exceeded"                                   # now used up
    admin().set_quota(t.name, 10 ** 12)                                         # a registry value overrides the default
    t.c.get("/api/v1/tenant")
    assert t.c.post("/api/v1/repositories", json={"path": fx["paths"]["mobile-bff"], "name": "m2"}).status_code == 200


def test_admin_usage_agrees_with_what_the_runtime_reports(live):
    a = live()
    a.populate()
    st = a.c.get("/api/v1/tenant").json()
    u = admin().usage(a.name, [a.ws])
    assert u["database_bytes"] == st["database_bytes"] and u["workspace_bytes"] == st["workspace_bytes"] > 0
    assert u["total_bytes"] == st["storage_used_bytes"] and u["quota_bytes"] is None
    assert u["database"]["analyses"]["rows"] >= 1 and u["database"]["audit_log"]["rows"] >= 3 and u["database"]["jobs"]["rows"] == 1
    assert u["workspace_bytes"] == dir_size(a.ws)


def test_every_tenant_table_is_known_to_the_runtime_and_to_offboarding():
    with psycopg.connect(conftest.PG.admin) as c:
        found = set(tenant_tables(c))
    assert found - {"tenant_installations"} == set(RUNTIME_TABLES)             # a new tenant table must be added to RUNTIME_TABLES
    assert "tenant_installations" in found and "tenant_events" not in found and "tenants" not in found


# ---- SCM installation mapping -----------------------------------------------------------------------------------------------------
def test_an_installation_belongs_to_exactly_one_tenant():
    a, x, y = admin(), admin().create(uniq()), admin().create(uniq())
    inst = str(uuid.uuid4().int % 10 ** 9)
    m = a.map_installation(x["name"], "github", inst, "acme-org")
    assert m == {"tenant": x["name"], "provider": "github", "installation_id": inst, "account": "acme-org"}
    a.map_installation(x["name"], "github", inst)                               # idempotent for the same tenant
    with pytest.raises(TenantError, match=f"already belongs to tenant '{x['name']}'"):
        a.map_installation(y["name"], "github", inst)
    a.map_installation(y["name"], "ado", inst)                                  # the same id at another provider is a different installation
    assert a.tenant_for_installation("github", inst) == {"id": x["id"], "name": x["name"], "status": "active"}
    assert a.installations(x["name"]) == [{"provider": "github", "installation_id": inst, "account": "acme-org"}]
    a.suspend(x["name"], "x")
    assert a.tenant_for_installation("github", inst)["status"] == "suspended"    # the receiver can refuse it
    for bad in (("svn", "1"), ("github", " ")):
        with pytest.raises(TenantError):
            a.map_installation(x["name"], *bad)
    a.unmap_installation(x["name"], "github", inst)
    assert a.tenant_for_installation("github", inst) is None
    with pytest.raises(TenantError, match="is not mapped"):
        a.unmap_installation(x["name"], "github", inst)
    assert [e["event"] for e in a.events(x["name"])][-2:] == ["tenant_suspended", "installation_unmapped"]


# ---- offboarding -------------------------------------------------------------------------------------------------------------------
def test_offboarding_deletes_everything_proves_it_and_leaves_other_tenants_alone(live):
    a, b = live(), live()
    ma, mb = a.populate(), b.populate()
    admin().map_installation(a.name, "github", "424242")
    b_before, b_audit = rows(b.id), b.c.get("/api/v1/audit/verify").json()
    a_before = rows(a.id)
    assert a_before["analyses"] >= 1 and a_before["repositories"] >= 3 and a_before["jobs"] == 1 and a_before["audit_log"] >= 3
    assert {"extractions", "file_extracts", "graph_versions", "graph_heads", "evidence_snapshots", "tenant_installations"} <= set(a_before)
    assert a.ws.exists() and dir_size(a.ws) > 0 and b_audit["ok"] is True

    cert = admin().offboard(a.name, a.name, [a.ws])

    assert rows(a.id) == {} and not a.ws.exists()                                # gone from every table and from disk
    assert certificate_valid(cert) and cert["tenant_name"] == a.name and cert["version"] == 1
    for t, n in a_before.items():
        assert cert["tables"][t]["rows_before"] == n and cert["tables"][t]["rows_deleted"] == n and cert["tables"][t]["rows_remaining"] == 0
    assert cert["workspace"][0]["existed"] and cert["workspace"][0]["bytes_before"] > 0 and cert["workspace"][0]["exists_after"] is False
    assert cert["audit_chain"]["entries"] == a_before["audit_log"] and len(cert["audit_chain"]["head_hash"]) == 64
    assert "tenant_events" in cert["retained"] and cert["requested_at"] and cert["completed_at"]
    assert admin().verify_deletion(a.name, [a.ws]) == {"clean": True, "remaining_rows": {}, "remaining_paths": []}
    t = admin().get(a.name)
    assert t["status"] == "offboarded" and t["offboarded_at"]
    assert admin().tenant_for_installation("github", "424242") is None

    r = a.c.get("/api/v1/repositories")                                           # the offboarded tenant is closed for good
    assert r.status_code == 410 and r.headers["x-tia-code"] == "tenant_offboarded"
    assert a.c.get(f"/analyses/{ma['analysis']}").status_code == 410
    with pytest.raises(TenantError, match="already exists"):
        admin().create(a.name)
    with pytest.raises(ValueError, match="never reused"):
        TenantAdmin(conftest.PG.admin).ensure(a.name)
    with pytest.raises(TenantError, match="offboarded"):
        admin().set_quota(a.name, 1)
    with pytest.raises(TenantError, match="offboarded"):
        admin().map_installation(a.name, "github", "1")
    ev = [e["event"] for e in admin().events(a.name)]
    assert ev[0] == "tenant_created" and ev[-2:] == ["offboarding_started", "offboarded"]   # the lifecycle log outlives the data

    assert rows(b.id) == b_before                                                 # the other tenant is untouched
    assert b.c.get("/api/v1/audit/verify").json() == b_audit and b.c.get(f"/api/v1/impact-analyses/{mb['analysis']}").status_code == 200
    assert b.ws.exists() and len(b.c.get("/api/v1/repositories").json()) == 3


def test_offboarding_needs_the_exact_name_and_blocks_the_tenant_first(live):
    a = live()
    a.populate(upload=False)
    before = rows(a.id)
    for wrong in ("", a.name.upper(), a.name + " ", "other"):
        with pytest.raises(TenantError, match="does not match the tenant name; nothing was deleted"):
            admin().offboard(a.name, wrong)
    assert admin().get(a.name)["status"] == "active" and rows(a.id) == before and a.c.get("/api/v1/repositories").status_code == 200
    with pytest.raises(TenantError, match="unknown tenant"):
        admin().offboard("missing-" + uniq(), "missing")


def test_offboarding_is_idempotent_and_resumes_after_a_crash(live):
    a = live()
    a.populate(upload=False)
    # simulate a crash half way: the tenant was blocked and some tables emptied, then the process died
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("update tenants set status = 'offboarding' where id = %s", (a.id,))
        c.execute("select set_config('tia.tenant_id', %s, false)", (a.id,))
        c.execute("delete from analyses where tenant_id = %s", (a.id,))
        c.execute("delete from jobs where tenant_id = %s", (a.id,))
    assert a.c.get("/api/v1/repositories").status_code == 410                   # blocked while offboarding, not yet deleted
    assert rows(a.id)
    cert = admin().offboard(a.name, a.name)
    assert rows(a.id) == {} and admin().get(a.name)["status"] == "offboarded" and certificate_valid(cert)
    assert admin().offboard(a.name, a.name) == cert                              # a second run returns the same certificate


def test_verification_detects_anything_left_behind(live, tmp_path):
    a = live()
    a.populate(upload=False)
    admin().offboard(a.name, a.name)
    assert admin().verify_deletion(a.name)["clean"] is True
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:               # a stray row, as a bug elsewhere could leave
        c.execute("select set_config('tia.tenant_id', %s, false)", (a.id,))
        c.execute("insert into repositories (tenant_id, name, data) values (%s, 'stray', '{}')", (a.id,))
    left = admin().verify_deletion(a.name)
    assert left["clean"] is False and left["remaining_rows"] == {"repositories": 1}
    stray = tmp_path / "leftover" / "tenant" / "dir"
    stray.mkdir(parents=True)
    assert admin().verify_deletion(a.name, [stray])["remaining_paths"] == [str(stray.resolve())]


def test_offboarding_stays_open_if_deletion_cannot_be_verified(live, tmp_path, monkeypatch):
    a = live()
    a.populate(upload=False)
    monkeypatch.setattr("tia.tenants.remove_workspace", lambda p: {"path": str(p), "bytes_before": 0, "existed": True, "exists_after": True})
    keep = tmp_path / "keep" / "me" / "workspace"
    keep.mkdir(parents=True)
    with pytest.raises(TenantError, match="could not be verified"):
        admin().offboard(a.name, a.name, [keep])
    assert admin().get(a.name)["status"] == "offboarding"                        # never reported as done
    assert rows(a.id) == {}                                                       # the rows are gone, the path is not
    monkeypatch.undo()
    assert admin().offboard(a.name, a.name, [keep])["workspace"][0]["exists_after"] is False and not keep.exists()


def test_workspace_removal_refuses_anything_that_is_not_a_data_directory(tmp_path, monkeypatch):
    for bad in (Path.home(), Path(Path.home().anchor), Path.cwd(), Path.cwd().parent):
        with pytest.raises(TenantError, match="refusing to delete"):
            remove_workspace(str(bad))
    d = tmp_path / "a" / "b" / "ws"
    (d / "objects").mkdir(parents=True)
    f = d / "objects" / "pack.idx"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)                                                     # read-only, like git objects on Windows
    out = remove_workspace(str(d))
    assert out["existed"] and out["bytes_before"] == 1 and not d.exists() and out["exists_after"] is False
    assert remove_workspace(str(d))["existed"] is False                           # already gone is fine


def test_deleted_tenant_data_is_not_reachable_with_the_old_credentials(live):
    a = live()
    ids = a.populate(upload=False)
    admin().offboard(a.name, a.name)
    with conftest.PG.db.tx(a.id) as conn:                                          # even bound to the old tenant id, nothing is left
        for t in RUNTIME_TABLES:
            assert conn.execute(f'select count(*) from "{t}"').fetchone()[0] == 0, t


# ---- command line -------------------------------------------------------------------------------------------------------------------
def test_cli_round_trip(capsys):
    name = uniq("cli")
    base = ["--admin-dsn", conftest.PG.admin]

    def run(*argv):
        code = tenants_cli([*base, *argv])
        cap = capsys.readouterr()
        return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err

    code, t, _ = run("create", name, "--display-name", "CLI Bank", "--quota-bytes", "1000000")
    assert code == 0 and t["name"] == name and t["storage_quota_bytes"] == 1000000 and t["status"] == "active"
    code, _, err = run("create", name)
    assert code == 1 and "already exists" in err                                  # an operator error is a message and exit code 1
    with pytest.raises(SystemExit) as e:
        tenants_cli([*base, "suspend", name])                                     # --reason is mandatory
    assert e.value.code == 2
    capsys.readouterr()
    assert run("suspend", name, "--reason", "audit")[1]["status"] == "suspended"
    assert run("show", name)[1]["suspended_reason"] == "audit"
    assert run("resume", name)[1]["status"] == "active"
    assert run("quota", name, "5000")[1]["storage_quota_bytes"] == 5000 and run("quota", name, "none")[1]["storage_quota_bytes"] is None
    assert run("map-installation", name, "github", "777", "--account", "org")[1]["installation_id"] == "777"
    assert run("installations", name)[1] == [{"provider": "github", "installation_id": "777", "account": "org"}]
    usage = run("usage", name)[1]
    assert usage["database"]["tenant_installations"]["rows"] == 1 and usage["total_bytes"] == usage["database"]["tenant_installations"]["bytes"] > 0
    assert name in [x["name"] for x in run("list")[1]]
    assert [e["event"] for e in run("events", name)[1]][:2] == ["tenant_created", "tenant_suspended"]
    code, _, err = run("offboard", name, "--confirm", "not-the-name")
    assert code == 1 and "nothing was deleted" in err and run("show", name)[1]["status"] == "active"
    code, cert, _ = run("offboard", name, "--confirm", name)
    assert code == 0 and certificate_valid(cert) and run("verify-deletion", name)[1]["clean"] is True
    assert run("show", name)[1]["status"] == "offboarded" and run("unmap-installation", name, "github", "777")[0] == 1
    code, _, err = run("suspend", name, "--reason", "x")
    assert code == 1 and "offboarded" in err                                      # a closed tenant cannot be reopened


def test_cli_needs_an_admin_dsn(capsys, monkeypatch):
    monkeypatch.delenv("TIA_ADMIN_DATABASE_URL", raising=False)
    assert tenants_cli(["list"]) == 2 and "admin DSN is required" in capsys.readouterr().err
