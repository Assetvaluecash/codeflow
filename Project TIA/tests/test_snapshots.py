"""UC1-13 / AC-015: evidence snapshots are materialised, auditable, immutable, and re-analysis on one is identical."""
import json
import shutil
import subprocess

import psycopg
import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import fresh_store, git, make_app

BREAKING = {"target": "payments-service", "head": "breaking", "base": "main"}


def register(c, repos):
    for n, p in repos.items():
        assert c.post("/api/v1/repositories", json={"path": str(p), "name": n}).status_code == 200


def analyse(c, **kw):
    r = c.post("/api/v1/impact-analyses", json={**BREAKING, **kw})
    assert r.status_code == 200, r.text
    return r.json()["report"]


def reproduce(c, aid, headers=None):
    return c.post(f"/api/v1/impact-analyses/{aid}/reproduce", headers=headers or {})


def finding(rep, component):
    return next((f for f in rep["findings"] if f["component"] == component), None)


def test_snapshot_is_materialised_with_everything_needed_to_audit_the_result(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register(c, landscape)
    rep = analyse(c)
    sid = rep["evidence_snapshot_id"]
    snap = c.get(f"/api/v1/evidence-snapshots/{sid}").json()
    assert snap["id"] == sid and snap["created_at"] and snap["graph_id"].startswith("g-")
    assert snap["target"] == "payments-service"
    assert snap["target_base"] == rep["change"]["base_revision"] and snap["target_head"] == rep["change"]["proposed_revision"]
    heads = {n: git(p, "rev-parse", "HEAD") for n, p in landscape.items()}
    assert snap["revisions"]["mobile-bff"] == heads["mobile-bff"]                         # every repository, pinned to a commit
    assert set(snap["revisions"]) == set(landscape) and snap["revisions"]["payments-service"] == snap["target_base"]
    assert snap["config_version"] and snap["extractor_versions"] == rep["extractor_versions"] and set(rep["extractor_versions"]) == {"python", "java", "data"}
    assert snap["freshness"]["payments-service"]["scan_status"] == "ok" and snap["stale_relationships"] == []
    assert c.get("/api/v1/evidence-snapshots/snap-nope").status_code == 404

    ev = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}/evidence").json()
    assert ev["evidence_snapshot_id"] == sid and ev["snapshot"]["revisions"] == snap["revisions"]
    mats = [f for f in rep["findings"] if f["material"]]
    assert len(ev["material_findings"]) == len(mats) >= 4
    for m in ev["material_findings"]:                                                    # the path, with the revision of each hop
        assert m["hops"] and all(h["revision"] == snap["revisions"][h["repo"]] and h["file"] is not None for h in m["hops"])
        assert m["edges"] and m["evidence_status"] in ("source_confirmed", "contract_confirmed", "inferred", "corroborated")
    assert c.get("/api/v1/impact-analyses/nope/evidence").status_code == 404


def test_reanalysis_on_the_snapshot_is_identical(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register(c, landscape)
    rep = analyse(c)
    r = reproduce(c, rep["analysis_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identical"] is True and body["differences"] == [] and body["versions_match"] is True
    assert body["evidence_snapshot_id"] == rep["evidence_snapshot_id"]
    stored = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}").json()
    assert body["report"]["findings"] == stored["findings"] and len(stored["findings"]) == 5
    assert "analysis_reproduced" in [a["event"] for a in c.get("/api/v1/audit").json()]
    assert reproduce(c, "nope").status_code == 404                                                 # unknown analysis: not found (was 400 before the access checks)


def test_reanalysis_is_identical_even_after_the_landscape_has_moved_on(landscape, tmp_path):
    """Not a re-run at head: the consumer stops calling payments and the repo is synced; the old analysis still reproduces."""
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register(c, landscape)
    old = analyse(c, pr=None)
    assert finding(old, "reconciliation-service")["severity"] == "critical"

    consumer = landscape["reconciliation-service"]
    (consumer / "client.py").write_text("def fetch_status(payment_id):\n    return None\n")
    git(consumer, "commit", "-qam", "stop calling payments")
    assert c.post("/api/v1/repositories/reconciliation-service/sync", json={}).status_code == 200
    now = analyse(c)
    assert finding(now, "reconciliation-service") is None                                # the world changed...
    assert now["evidence_snapshot_id"] != old["evidence_snapshot_id"]

    assert old["analysis_id"] != now["analysis_id"]                                      # distinct evidence, distinct results
    body = reproduce(c, old["analysis_id"]).json()
    assert body["identical"] is True and body["evidence_snapshot_id"] == old["evidence_snapshot_id"]
    assert finding(body["report"], "reconciliation-service")["severity"] == "critical"


def test_stale_state_at_analysis_time_is_part_of_the_snapshot(remotes, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    for n, bare in remotes.items():
        assert c.post("/api/v1/repositories", json={"url": bare.as_uri(), "name": n}).status_code == 200
    gone = tmp_path / "offline.git"
    shutil.move(str(remotes["reconciliation-service"]), str(gone))
    assert c.post("/api/v1/repositories/reconciliation-service/sync", json={}).status_code == 400
    during = analyse(c)
    assert finding(during, "reconciliation-service")["evidence_status"] == "stale"
    snap = c.get(f"/api/v1/evidence-snapshots/{during['evidence_snapshot_id']}").json()
    assert snap["freshness"]["reconciliation-service"]["scan_status"] == "failed"
    assert [x["consumer_repo"] for x in snap["stale_relationships"]] == ["reconciliation-service"]

    shutil.move(str(gone), str(remotes["reconciliation-service"]))                        # healed: current state differs
    assert c.post("/api/v1/repositories/reconciliation-service/sync", json={}).status_code == 200
    healed = analyse(c)
    assert finding(healed, "reconciliation-service")["evidence_status"] == "source_confirmed"
    assert healed["evidence_snapshot_id"] != during["evidence_snapshot_id"]

    assert during["analysis_id"] != healed["analysis_id"]
    body = reproduce(c, during["analysis_id"]).json()
    assert body["identical"] is True and body["evidence_snapshot_id"] == during["evidence_snapshot_id"]
    again = finding(body["report"], "reconciliation-service")
    assert again["evidence_status"] == "stale" and again["severity"] == "medium"          # the state at the time, not today's


def test_reproduction_reports_a_changed_extractor_or_configuration_version(landscape, tmp_path, monkeypatch):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register(c, landscape)
    rep = analyse(c)
    import tia.analysis_service as svc_mod
    monkeypatch.setattr(svc_mod, "CONFIG_VERSION", "cfg-changed")
    body = reproduce(c, rep["analysis_id"]).json()
    assert body["versions_match"] is False                                               # said plainly, never silently


def test_snapshot_commits_are_pinned_so_evidence_survives_history_rewrites(remotes, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    for n, bare in remotes.items():
        assert c.post("/api/v1/repositories", json={"url": bare.as_uri(), "name": n}).status_code == 200
    rep = analyse(c)
    head = rep["change"]["proposed_revision"]
    clone = tmp_path / "d" / "payments-service"
    assert head in git(clone, "for-each-ref", "--format=%(objectname)", "refs/tia/pin").split()

    git(remotes["payments-service"], "branch", "-D", "breaking")                          # the branch is deleted upstream
    assert c.post("/api/v1/repositories/payments-service/sync", json={}).status_code == 200
    subprocess.run(["git", "-C", str(clone), "gc", "-q", "--prune=now"], check=True)
    assert reproduce(c, rep["analysis_id"]).json()["identical"] is True                  # pinned: still reproducible

    # without the pin the same history rewrite makes the evidence unavailable, and TIA says so
    for ref in git(clone, "for-each-ref", "--format=%(refname)", "refs/tia/pin").split():
        git(clone, "update-ref", "-d", ref)
    subprocess.run(["git", "-C", str(clone), "gc", "-q", "--prune=now"], check=True)
    r = reproduce(c, rep["analysis_id"])
    assert r.status_code == 400 and "no longer available" in r.json()["detail"]


def test_snapshots_are_immutable_by_privilege_and_tenant_isolated(landscape, tmp_path):
    tenant = conftest.new_tenant()
    from tia.api import create_app
    c = TestClient(create_app(conftest.PG.db, tenant, str(tmp_path / "d"), allow_local=True))
    register(c, landscape)
    rep = analyse(c)
    sid = rep["evidence_snapshot_id"]
    for stmt in ("update evidence_snapshots set data = '{}'", "delete from evidence_snapshots"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conftest.PG.db.tx(tenant) as cur:
                cur.execute(stmt)
    from tia.store import Store
    assert Store(conftest.PG.db, tenant).get_snapshot(sid)["id"] == sid
    assert fresh_store().get_snapshot(sid) is None                                       # another tenant sees nothing


def test_audience_without_access_learns_nothing_from_the_evidence_views(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register(c, landscape)
    rep = analyse(c)
    h = {"X-TIA-Readable-Repos": "payments-service,mobile-bff"}
    for url in (f"/api/v1/impact-analyses/{rep['analysis_id']}/evidence", f"/api/v1/evidence-snapshots/{rep['evidence_snapshot_id']}"):
        blob = json.dumps(c.get(url, headers=h).json())
        assert "reconciliation-service" not in blob and "audit-reporting" not in blob and "ledger-sync" not in blob
    r = reproduce(c, rep["analysis_id"], headers=h).json()
    assert "reconciliation-service" not in json.dumps(r["report"])
