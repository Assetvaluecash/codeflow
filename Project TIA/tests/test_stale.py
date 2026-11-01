"""UC1-12: stale relationships (AC-011) and repository freshness (AC-022, FR-005), end to end through the API."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import git, make_app

BREAKING = {"target": "payments-service", "head": "breaking", "base": "main"}


def register_all(c, landscape):
    for n, p in landscape.items():
        assert c.post("/api/v1/repositories", json={"path": str(p), "name": n}).status_code == 200


def register_remotes(c, remotes):
    for n, bare in remotes.items():
        r = c.post("/api/v1/repositories", json={"url": bare.as_uri(), "name": n})
        assert r.status_code == 200, r.text


def rels(c, state=None):
    q = f"?state={state}" if state else ""
    return c.get(f"/api/v1/graph/relationships{q}").json()


def analyse(c, headers=None):
    r = c.post("/api/v1/impact-analyses", json=BREAKING, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()["report"]


def finding(rep, component):
    return next((f for f in rep["findings"] if f["component"] == component), None)


def service_edge(c, src, dst):
    v = c.get("/api/v1/graph?level=service").json()
    return next((e for e in v["edges"] if e["source"] == f"svc:{src}" and e["target"] == f"svc:{dst}"), None)


def test_relationships_start_current_and_are_listed_with_their_evidence(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register_all(c, landscape)
    r = rels(c)
    assert r["stale"] == 0 and r["current"] == 4
    by_consumer = {x["consumer_repo"]: x for x in r["relationships"]}
    assert set(by_consumer) == {"reconciliation-service", "mobile-bff", "audit-reporting", "ledger-sync"}
    rec = by_consumer["reconciliation-service"]
    assert rec["state"] == "current" and rec["consumer_file"] == "client.py" and rec["strategy"] == "R2"
    assert rec["provider_repo"] == "payments-service" and rec["last_confirmed_consumer_commit"]
    assert by_consumer["ledger-sync"]["evidence_status"] == "inferred"
    assert c.get("/api/v1/graph/relationships?state=bogus").status_code == 400
    # repositories report their scan state (FR-005)
    repo = next(x for x in c.get("/api/v1/repositories").json() if x["name"] == "payments-service")
    assert repo["scan_status"] == "ok" and repo["analysed_commit"] and repo["last_successful_scan"]


def test_failed_scan_makes_relationships_stale_and_the_report_states_the_last_good_scan(remotes, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register_remotes(c, remotes)
    healthy = analyse(c)
    assert finding(healthy, "reconciliation-service")["evidence_status"] == "source_confirmed"
    assert finding(healthy, "reconciliation-service")["severity"] == "critical"
    assert healthy["status"] == "complete_with_gaps"
    good_commit = next(x for x in healthy["coverage"]["repositories_analysed"]
                       if x["repo"] == "reconciliation-service")["commit"]

    # the consumer's remote goes away: its next scan fails
    gone = tmp_path / "offline.git"
    shutil.move(str(remotes["reconciliation-service"]), str(gone))
    r = c.post("/api/v1/repositories/reconciliation-service/sync", json={})
    assert r.status_code == 400

    repo = next(x for x in c.get("/api/v1/repositories").json() if x["name"] == "reconciliation-service")
    assert repo["scan_status"] == "failed" and repo["scan_error"] and repo["last_successful_scan"]
    assert repo["analysed_commit"] == good_commit                                   # AC-022: last good revision is kept

    stale = rels(c, "stale")["relationships"]
    assert [x["consumer_repo"] for x in stale] == ["reconciliation-service"]
    assert stale[0]["stale_reason"] == "scan_failed"
    assert "latest scan of 'reconciliation-service' failed" in stale[0]["stale_detail"] and good_commit[:7] in stale[0]["stale_detail"]
    assert rels(c)["current"] == 3
    e = service_edge(c, "reconciliation-service", "payments-service")
    assert e["status"] == "stale" and e["stale_count"] == 1                           # never drawn as a live dependency

    rep = analyse(c)
    f = finding(rep, "reconciliation-service")
    assert f["evidence_status"] == "stale" and f["severity"] == "medium" and f["severity_rule"] == "SEV-STALE-CAP"
    assert any("Stale relationship (scan_failed)" in x for x in f["limitations"])
    assert finding(rep, "mobile-bff")["evidence_status"] == "source_confirmed"        # the others are untouched
    assert rep["status"] == "partial"
    u = next(u for u in rep["unknowns"] if u["area"] == "repository_freshness")
    assert "reconciliation-service" in u["reason"] and good_commit[:7] in u["reason"] and "last successful scan" in u["reason"]
    cov = next(x for x in rep["coverage"]["repositories_analysed"] if x["repo"] == "reconciliation-service")
    assert cov["freshness"]["scan_status"] == "failed" and cov["freshness"]["analysed_revision"] == good_commit
    assert cov["freshness"]["last_successful_scan"]
    assert next(x for x in rep["coverage"]["repositories_analysed"] if x["repo"] == "mobile-bff")["freshness"]["scan_status"] == "ok"
    assert next(u for u in rep["unknowns"] if u["area"] == "stale_relationships")["count"] == 1
    assert "stale" in c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}/comment").text

    # the remote comes back: the next scan succeeds and the relationship is current again
    shutil.move(str(gone), str(remotes["reconciliation-service"]))
    assert c.post("/api/v1/repositories/reconciliation-service/sync", json={}).status_code == 200
    assert rels(c)["stale"] == 0 and rels(c)["current"] == 4
    assert service_edge(c, "reconciliation-service", "payments-service")["status"] != "stale"
    again = analyse(c)
    assert finding(again, "reconciliation-service")["evidence_status"] == "source_confirmed"
    assert finding(again, "reconciliation-service")["severity"] == "critical"
    assert not any(u["area"] == "repository_freshness" for u in again["unknowns"]) and again["status"] == "complete_with_gaps"
    events = [a["event"] for a in c.get("/api/v1/audit").json()]
    assert "repository_sync_failed" in events and events.count("relationships_reconciled") >= 3
    assert c.get("/api/v1/audit/verify").json()["ok"] is True


def test_removed_call_retires_the_relationship_as_stale_not_current(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register_all(c, landscape)
    consumer = landscape["reconciliation-service"]
    (consumer / "client.py").write_text("def fetch_status(payment_id):\n    return None\n")
    git(consumer, "commit", "-qam", "stop calling payments")
    assert c.post("/api/v1/repositories/reconciliation-service/sync", json={}).status_code == 200

    stale = rels(c, "stale")["relationships"]
    assert len(stale) == 1 and stale[0]["stale_reason"] == "evidence_removed"
    assert "no longer found in 'reconciliation-service'" in stale[0]["stale_detail"]
    assert stale[0]["stale_since"] and stale[0]["last_confirmed_consumer_commit"] != git(consumer, "rev-parse", "HEAD")
    e = service_edge(c, "reconciliation-service", "payments-service")
    assert e["status"] == "stale"                                                       # AC-011: identified as stale
    rep = analyse(c)
    assert finding(rep, "reconciliation-service") is None                               # the dependency really ended
    assert not any(u["area"] == "stale_relationships" for u in rep["unknowns"])


def test_unparseable_consumer_file_is_reported_as_a_stale_finding_not_silently_dropped(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register_all(c, landscape)
    consumer = landscape["reconciliation-service"]
    (consumer / "client.py").write_text("def fetch_status(:\n    pass\n")              # syntax error
    git(consumer, "commit", "-qam", "break the client")
    assert c.post("/api/v1/repositories/reconciliation-service/sync", json={}).status_code == 200

    stale = rels(c, "stale")["relationships"]
    assert len(stale) == 1 and stale[0]["stale_reason"] == "parse_failed"
    repo = next(x for x in c.get("/api/v1/repositories").json() if x["name"] == "reconciliation-service")
    assert repo["scan_status"] == "partial" and repo["scan_files_failed"] == 1          # a partial scan is not 'ok'

    rep = analyse(c)
    f = finding(rep, "reconciliation-service")
    assert f["evidence_status"] == "stale" and f["severity"] == "medium" and f["node_type"] == "callsite"
    assert f["evidence"]["file"] == "client.py" and f["path_edges"][0]["status"] == "stale"
    assert f["required_verification"][0].startswith("Re-scan reconciliation-service")
    assert rep["status"] == "partial"
    assert {"stale_relationships", "parse_failures"} <= {u["area"] for u in rep["unknowns"]}

    # audience without access to the consumer learns nothing about it
    h = {"X-TIA-Readable-Repos": "payments-service,mobile-bff"}
    red = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}", headers=h).json()
    assert "reconciliation-service" not in json.dumps(red)


def test_removing_a_repository_makes_its_relationships_stale(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register_all(c, landscape)
    assert c.delete("/api/v1/repositories/mobile-bff").status_code == 200
    stale = rels(c, "stale")["relationships"]
    assert [x["consumer_repo"] for x in stale] == ["mobile-bff"]
    assert stale[0]["stale_reason"] == "evidence_removed" and "was removed from TIA" in stale[0]["stale_detail"]
    assert rels(c)["current"] == 3
    # removing everything leaves only history, all of it stale
    for n in list(landscape):
        if n != "mobile-bff":
            c.delete(f"/api/v1/repositories/{n}")
    assert rels(c)["current"] == 0 and rels(c)["stale"] == 4


def test_unchanged_relationships_are_not_rewritten_on_every_refresh(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    register_all(c, landscape)
    first = {x["key"]: x["last_confirmed_at"] for x in rels(c)["relationships"]}
    c.post("/api/v1/repositories/payments-service/sync", json={})
    c.post("/api/v1/repositories/mobile-bff/sync", json={})
    assert {x["key"]: x["last_confirmed_at"] for x in rels(c)["relationships"]} == first
    assert "relationships_reconciled" in [a["event"] for a in c.get("/api/v1/audit").json()]
