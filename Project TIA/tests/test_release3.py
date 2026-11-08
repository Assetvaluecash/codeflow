"""Release 3 (PRD FR-011, FR-030 to FR-032, AC-033, AC-034, PL-005, 11.4): named releases and their comparison, what ran where at a point in
time, incident investigation with labelled associations, retention with an anchored audit chain, and encrypted tenant backup/restore."""
import base64
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import make_app, new_tenant
from fixtures.build_fixture import _commit, _run, _write
from fixtures.build_polyglot_fixture import BILLING, build as build_poly
from tia import backup, retention
from tia.db import migrate
from tia.store import Store

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def head(path, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    poly = build_poly(str(tmp_path_factory.mktemp("r3")))
    billing = poly["paths"]["billing-service"]
    _run(billing, "checkout", "-q", "-b", "no_ledger")
    _write(__import__("pathlib").Path(billing), {"src/main/kotlin/com/acme/billing/BillingService.kt":
                                                  BILLING.replace('        rest.postForObject("$ledgerUrl/api/entries", p, String::class.java)\n', "")})
    poly["shas"]["no_ledger"] = _commit(__import__("pathlib").Path(billing), "billing stops writing to the ledger")
    _run(billing, "checkout", "-q", "main")
    _run(billing, "tag", "v1.0")
    tid = new_tenant()
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), tenant=tid, allow_local=True))
    for n, p in poly["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    return {"c": c, "poly": poly, "tid": tid, "shas": {n: head(p) for n, p in poly["paths"].items()}}


# ---- releases and temporal comparison (FR-030, AC-033) ----------------------------------------------------------------------------------
def test_releases_compare_relationship_by_relationship(world):
    c, shas = world["c"], world["shas"]
    r1 = c.post("/api/v1/releases", json={"name": "2026.10", "revisions": {**{n: "main" for n in shas}, "billing-service": "v1.0"}})
    assert r1.status_code == 200, r1.text
    assert r1.json()["revisions"]["billing-service"] == shas["billing-service"] and r1.json()["edges"] > 0
    r2 = c.post("/api/v1/releases", json={"name": "2026.11", "revisions": {**shas, "billing-service": "no_ledger"}, "description": "Nov"})
    assert r2.status_code == 200
    assert [r["name"] for r in c.get("/api/v1/releases").json()["releases"]] == ["2026.11", "2026.10"]
    d = c.get("/api/v1/releases/compare?base=2026.10&head=2026.11").json()
    assert d["summary"] == {"introduced": 0, "removed": 1, "modified": 0}
    (gone,) = d["removed"]
    assert (gone["rel"], gone["from_service"], gone["to_service"], gone["to"]) == ("CONSUMES_API", "billing-service", "ledger-service",
                                                                                   "ledger-service:POST /api/entries")
    back = c.get("/api/v1/releases/compare?base=2026.11&head=2026.10").json()
    assert back["summary"] == {"introduced": 1, "removed": 0, "modified": 0}
    # the same name for other revisions is refused; the same release again is the same record
    assert c.post("/api/v1/releases", json={"name": "2026.10", "revisions": {"billing-service": "no_ledger"}}).status_code == 409
    again = c.post("/api/v1/releases", json={"name": "2026.10", "revisions": {**{n: "main" for n in shas}, "billing-service": "v1.0"}}).json()
    assert again["created"] is False


def test_consumers_known_at_a_release(world):
    c = world["c"]
    old = c.get("/api/v1/releases/2026.10/consumers", params={"component": "POST /api/entries"}).json()
    new = c.get("/api/v1/releases/2026.11/consumers", params={"component": "POST /api/entries"}).json()
    assert [x["service"] for x in old["consumers"]] == ["billing-service"] and old["consumers"][0]["status"] == "source_confirmed"
    assert new["consumers"] == []
    prices = c.get("/api/v1/releases/2026.11/consumers", params={"component": "GET /prices/{}"}).json()
    assert sorted(x["service"] for x in prices["consumers"]) == ["billing-service", "orders-service"]
    assert c.get("/api/v1/releases/2026.11/consumers", params={"component": "GET /nope"}).status_code == 404
    assert c.get("/api/v1/releases/1999.01/consumers", params={"component": "x"}).status_code == 404


def test_release_input_is_validated(world):
    c = world["c"]
    assert c.post("/api/v1/releases", json={"name": "bad name!", "revisions": {"orders-service": "main"}}).status_code == 400
    assert c.post("/api/v1/releases", json={"name": "x", "revisions": {}}).status_code == 400
    assert c.post("/api/v1/releases", json={"name": "x", "revisions": {"ghost-service": "main"}}).status_code == 404
    assert c.post("/api/v1/releases", json={"name": "x", "revisions": {"orders-service": "no-such-branch"}}).status_code == 400


# ---- what ran where (FR-011) and incidents (FR-031, FR-032, AC-034) ---------------------------------------------------------------------
def test_what_ran_in_each_environment_at_a_point_in_time(world):
    c, shas = world["c"], world["shas"]
    deps = [{"repo": "pricing-service", "environment": "production", "revision": shas["pricing-service"], "deployed_at": iso(T0 - timedelta(hours=2))},
            {"repo": "orders-service", "environment": "production", "revision": shas["orders-service"], "deployed_at": iso(T0 - timedelta(hours=1))},
            {"repo": "ledger-service", "environment": "production", "revision": shas["ledger-service"], "deployed_at": iso(T0 - timedelta(hours=5))},
            {"repo": "billing-service", "environment": "production", "revision": shas["billing-service"], "deployed_at": iso(T0 - timedelta(hours=200))},
            {"repo": "orders-service", "environment": "staging", "revision": world["poly"]["shas"]["base"], "deployed_at": iso(T0 - timedelta(hours=30))}]
    assert c.post("/api/v1/evidence/deployments", content=json.dumps(deps)).status_code == 200
    at = c.get("/api/v1/environments/at", params={"at": iso(T0 - timedelta(hours=3))}).json()["deployed"]
    assert set(at) == {"ledger-service", "billing-service", "orders-service"}                 # pricing was deployed after that moment
    assert set(at["orders-service"]) == {"staging"}
    now = c.get("/api/v1/environments/at", params={"at": iso(T0)}).json()["deployed"]
    assert now["pricing-service"]["production"]["revision"] == shas["pricing-service"]
    assert c.get("/api/v1/environments/at", params={"at": "yesterday"}).status_code == 400


def test_incident_candidates_are_labelled_never_a_root_cause(world):
    c, shas = world["c"], world["shas"]
    inc = {"id": "INC-7", "title": "orders failing", "service": "orders-service", "environment": "production", "started_at": iso(T0),
           "changes": [{"repo": "orders-service", "pull_request": 42, "revision": shas["orders-service"]}]}
    assert c.post("/api/v1/incidents", content=json.dumps(inc)).status_code == 200
    assert c.post("/api/v1/incidents", content=json.dumps({"id": "x"})).status_code == 400
    link = c.post("/api/v1/incidents/INC-7/links", content=json.dumps({"repo": "ledger-service", "revision": shas["ledger-service"], "note": "seen in logs"}))
    assert link.status_code == 200 and link.json()["by"]
    assert c.post("/api/v1/incidents/INC-7/links", content=json.dumps({"repo": "ledger-service", "revision": "main"})).status_code == 400
    assert c.post("/api/v1/incidents/INC-404/links", content=json.dumps({"repo": "ledger-service", "revision": "abcdef1"})).status_code == 404
    out = c.get("/api/v1/incidents/INC-7/investigation").json()
    assert out["root_cause"] is None and "does not determine root cause" in out["note"]
    by = {x["repo"]: x for x in out["candidates"]}
    assert by["orders-service"]["associations"] == ["directly_recorded", "time_correlated"]
    assert by["pricing-service"]["associations"] == ["dependency_derived"]                   # orders-service depends on pricing-service
    assert "orders-service depends on pricing-service" in by["pricing-service"]["evidence"][0]["because"]
    assert by["ledger-service"]["associations"] == ["manually_validated"]                    # deployed in the window, but not upstream
    assert "billing-service" not in by                                                        # deployed outside the 72 h window
    assert out["upstream_services"] == ["pricing-service"]
    narrow = c.get("/api/v1/incidents/INC-7/investigation?window_hours=1.5").json()
    assert {x["repo"] for x in narrow["candidates"]} == {"orders-service", "ledger-service"}
    wide = c.get("/api/v1/incidents/INC-7/investigation?window_hours=300").json()
    assert "billing-service" not in {x["repo"] for x in wide["candidates"]}                   # billing calls pricing; orders does not call billing


# ---- retention with an anchored audit chain (PRD 11.4), audit report ---------------------------------------------------------------------
def test_retention_purges_and_the_audit_chain_still_verifies(world):
    c, tid = world["c"], world["tid"]
    before = c.get("/api/v1/audit/verify").json()
    assert before["ok"] and before["anchor"] is None and before["entries"] > 5
    r = c.put("/api/v1/tenant/config", json={"config": {"retention": {"audit_days": 365, "runtime_days": 30}}, "comment": "policy"})
    assert r.status_code == 200, r.text
    assert c.put("/api/v1/tenant/config", json={"config": {"retention": {"audit_days": 5}}}).status_code == 422
    report = c.get("/api/v1/audit/report", params={"start": "2000-01-01T00:00:00+00:00", "end": "2100-01-01T00:00:00+00:00"}).json()
    assert report["entries"] == before["entries"] + 1 and report["by_event"]["tenant_config_changed"] >= 1
    other = Store(conftest.PG.db, new_tenant())                  # another tenant with a longer chain: its numbers must never leak into ours
    for i in range(report["entries"] + 20):
        other.audit("noise", i=i)
    res = retention.enforce(conftest.PG.admin, tid, now=datetime.now(timezone.utc) + timedelta(days=400))
    (mine,) = res
    assert mine["removed"]["audit"] >= report["entries"] and mine["removed"]["runtime"] >= 5      # the deployments pushed above
    after = c.get("/api/v1/audit/verify").json()
    assert after["ok"] and after["anchor"]["seq"] >= report["entries"] and after["anchor"]["seq"] == mine["removed"]["audit"], (after, mine)
    first = min(c.get("/api/v1/audit?limit=1000").json(), key=lambda e: e["seq"])
    assert first["seq"] == after["anchor"]["seq"] + 1 and first["event"] == "retention_enforced"   # the purge is the first entry after the anchor
    c.get("/api/v1/releases")                                                                    # later activity extends the anchored chain
    c.post("/api/v1/incidents", content=json.dumps({"id": "INC-8", "service": "orders-service", "started_at": iso(T0)}))
    assert c.get("/api/v1/audit/verify").json()["ok"]
    # tampering after the anchor is still detected
    with psycopg.connect(conftest.PG.admin) as a:
        a.execute("update audit_log set payload = '{}' where tenant_id = %s and seq = %s", (tid, after["anchor"]["seq"] + 1))
    bad = c.get("/api/v1/audit/verify").json()
    assert not bad["ok"] and bad["first_bad_seq"] == after["anchor"]["seq"] + 1


def test_the_runtime_role_cannot_write_an_anchor(world):
    with conftest.PG.db.tx(world["tid"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("insert into audit_anchors (tenant_id, seq, hash, purged_before) values (%s, 1, 'x', 'y')", (world["tid"],))


# ---- encrypted tenant backup, restore into another database, verify (PL-005) -------------------------------------------------------------
def test_backup_restore_and_verify_into_another_database(world, monkeypatch):
    admin = conftest.PG.admin
    monkeypatch.setenv("TIA_BACKUP_KEY", base64.b64encode(os.urandom(32)).decode())
    blob = backup.export(admin, world["tid"])
    assert blob.startswith(backup.MAGIC + b"aesgcm\n") and b"orders-service" not in blob            # encrypted
    doc = backup.read(blob)
    assert doc["manifest"]["evidence_records"]["rows"] > 0 and doc["manifest"]["repositories"]["rows"] == 4
    tampered = blob[:-5] + bytes([blob[-5] ^ 1]) + blob[-4:]
    with pytest.raises(backup.BackupError, match="altered"):
        backup.read(tampered)
    with pytest.raises(backup.BackupError, match="not encrypted"):
        backup.read(backup.MAGIC + b"plain\n")
    with pytest.raises(backup.BackupError, match="already has this tenant"):
        backup.restore(admin, blob)                                                                     # never merges into existing data
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute("drop database if exists tia_restore_t")
        a.execute("create database tia_restore_t")
    target = psycopg.conninfo.make_conninfo(admin, dbname="tia_restore_t")
    migrate(target, conftest.APP_PW)
    out = backup.restore(target, blob)
    assert out["tenant_id"] == world["tid"] and out["tables"]["repositories"] == 4
    v = backup.verify(target, world["tid"], blob)
    assert v["ok"] and v["mismatched"] == [] and v["tables_checked"] == len(doc["manifest"])
    with psycopg.connect(target, autocommit=True) as t:
        t.execute("delete from evidence_records where tenant_id = %s and kind = 'incident'", (world["tid"],))
    assert backup.verify(target, world["tid"], blob)["mismatched"] == ["evidence_records"]
    monkeypatch.setenv("TIA_BACKUP_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(backup.BackupError, match="cannot be decrypted"):
        backup.read(blob)
