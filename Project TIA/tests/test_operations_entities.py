"""PRD FR-007 execution and operations entities: build artifacts, rollbacks and failure signatures (alongside deployments, incidents,
runtime edges and change records, tested elsewhere). Ingested first, pinned in the analysis snapshot, redacted per reader."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tia.enrich import deployment_view, failure_signatures
from tia.evidence import EvidenceError, failure_signature, parse_builds, parse_deployments
from tia.models import RepoExtract


def junit(fail_msg: str) -> str:
    return ('<?xml version="1.0"?><testsuites><testsuite name="pytest">'
            '<testcase classname="tests.test_payments" name="test_list_payments" file="tests/test_payments.py"/>'
            f'<testcase classname="tests.test_payments" name="test_unknown_account_is_404" file="tests/test_payments.py">'
            f'<failure message="{fail_msg}">x</failure></testcase></testsuite></testsuites>')


# ---- units -------------------------------------------------------------------------------------------------------------------------------
def test_builds_are_validated():
    b = parse_builds([{"repo": "x", "revision": "ABCDEF1", "build_id": "b-1", "status": "Failed",
                       "artifacts": ["x-api", {"name": "x-worker", "version": "1.2", "digest": "sha256:aa"}]}])[0]
    assert b["revision"] == "abcdef1" and b["status"] == "failed" and b["artifacts"] == [{"name": "x-api"}, {"name": "x-worker", "version": "1.2", "digest": "sha256:aa"}]
    for bad, msg in (([{"repo": "x", "revision": "v1", "build_id": "b"}], "commit id"), ([{"repo": "x", "revision": "abcdef1"}], "build_id"),
                     ([{"repo": "x", "revision": "abcdef1", "build_id": "b", "status": "green"}], "status")):
        with pytest.raises(EvidenceError, match=msg):
            parse_builds(bad)


def test_a_failure_signature_ignores_ids_numbers_values_and_paths():
    a = failure_signature("AssertionError: order 4711 for '9f8e7d6c5b4a3210' at /srv/app/orders.py:88 returned 500")
    b = failure_signature("AssertionError: order 12 for 'zzz' at C:\\ci\\app\\orders.py:3 returned 503\nsecond line ignored")
    c = failure_signature("KeyError: 'status'")
    assert a["id"] == b["id"] and a["id"] != c["id"] and "<n>" in a["pattern"] and failure_signature("  ") is None


def _ex(*names):
    return {n: RepoExtract(repo=n, service=n, commit="c" * 40, extractor_version="v") for n in names}


def _rec(i, data, kind="deployment"):
    return {"id": i, "kind": kind, "data": data, "observed_to": data.get("deployed_at") or data.get("finished_at"), "repo": data.get("repo"),
            "revision": data.get("revision")}


def test_rollbacks_are_found_explicit_or_by_returning_to_an_earlier_revision():
    deps = parse_deployments([
        {"repo": "svc", "environment": "production", "revision": "aaaaaaa", "deployed_at": "2026-11-01T10:00:00Z"},
        {"repo": "svc", "environment": "production", "revision": "bbbbbbb", "deployed_at": "2026-11-02T10:00:00Z", "artifact": "svc:2"},
        {"repo": "svc", "environment": "production", "revision": "aaaaaaa", "deployed_at": "2026-11-03T10:00:00Z", "artifact": "svc:1"},
        {"repo": "svc", "environment": "staging", "revision": "ccccccc", "deployed_at": "2026-11-03T10:00:00Z"},
        {"repo": "svc", "environment": "staging", "revision": "ddddddd", "deployed_at": "2026-11-04T10:00:00Z", "rollback": True}])
    builds = {"_all": {"svc": [{"build_id": "b1", "revision": "eeeeeee", "status": "succeeded", "evidence_id": 9,
                                "artifacts": [{"name": "svc", "version": "1"}]}]}}
    v = deployment_view(_ex("svc"), [_rec(i, d) for i, d in enumerate(deps)], {"svc": "c" * 40}, "", "", "", builds)["by_repo"]["svc"]
    prod, stg = v["production"], v["staging"]
    assert prod["rollbacks"] == [{"from_revision": "bbbbbbb", "to_revision": "aaaaaaa", "at": "2026-11-03T10:00:00+00:00", "explicit": False, "evidence_id": 2}]
    assert stg["rollbacks"][0]["explicit"] and stg["rollbacks"][0]["to_revision"] == "ddddddd"
    assert prod["build"]["build_id"] == "b1" and prod["artifact_revision_mismatch"]          # svc:1 was built from eeeeeee, deployed as aaaaaaa
    assert "rollbacks" not in v.get("development", {})


def test_failure_signatures_say_whether_a_failure_predates_the_change():
    base, head = "b" * 40, "h" * 40
    runs = [{"id": 1, "repo": "svc", "revision": base, "observed_to": "2026-11-01", "data": {"tests": [
                {"name": "t1", "file": "f.py", "result": "failed", "message": "timeout after 30s"}]}},
            {"id": 2, "repo": "svc", "revision": head, "observed_to": "2026-11-02", "data": {"tests": [
                {"name": "t1", "file": "f.py", "result": "failed", "message": "timeout after 31s"},
                {"name": "t2", "file": "f.py", "result": "error", "message": "KeyError: 'status'"}]}}]
    inc = [{"id": 7, "data": {"id": "INC-9", "title": "orders down", "failure_signature": failure_signature("KeyError: 'state'")}}]
    fs = {s["pattern"]: s for s in failure_signatures(runs, inc, "svc", base, head)["signatures"]}
    old, new = fs["timeout after <n>s"], fs["KeyError: <value>"]
    assert old["assessment"].startswith("already failing on the base revision") and old["runs"] == 2
    assert new["assessment"] == "new on the proposed revision" and new["incidents"] == [{"id": "INC-9", "title": "orders down", "evidence_id": 7}]


# ---- through the API: ingested, used, pinned, redacted ----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("ops")))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    base, head = land["shas"]["base"], land["shas"]["behaviour"]
    for rev, msg in ((base, "assert 500 == 404"), (head, "assert 503 == 404")):
        assert c.post(f"/api/v1/evidence/test-results?repo=payments-service&revision={rev}&build_id=ci-{rev[:4]}", content=junit(msg)).status_code == 200
    r = c.post("/api/v1/evidence/builds", content=json.dumps([
        {"repo": "payments-service", "revision": head, "build_id": "ci-900", "pipeline": "main", "status": "failed", "artifacts": [{"name": "payments", "version": "9"}]},
        {"repo": "payments-service", "revision": base, "build_id": "ci-800", "status": "succeeded", "artifacts": [{"name": "payments", "version": "8"}]}]))
    assert r.status_code == 200 and r.json()["builds"] == 2
    deps = [{"repo": "payments-service", "environment": "production", "revision": base, "deployed_at": "2026-11-01T10:00:00Z", "artifact": "payments:8"},
            {"repo": "payments-service", "environment": "production", "revision": head, "deployed_at": "2026-11-02T10:00:00Z", "artifact": "payments:9"},
            {"repo": "payments-service", "environment": "production", "revision": base, "deployed_at": "2026-11-03T10:00:00Z", "artifact": "payments:8"}]
    assert c.post("/api/v1/evidence/deployments", content=json.dumps(deps)).status_code == 200
    assert c.post("/api/v1/incidents", content=json.dumps({"id": "INC-77", "title": "404s turned into 5xx", "service": "payments-service",
                                                          "started_at": "2026-11-02T11:00:00Z", "error": "assert 502 == 404"})).status_code == 200
    out = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "behaviour", "base": "main"}).json()
    return c, out


def test_the_report_carries_builds_rollbacks_and_failure_signatures(world):
    c, out = world
    rep = out["report"]
    b = rep["builds"]["by_repo"]["payments-service"]
    assert b["proposed_revision"]["build_id"] == "ci-900" and b["proposed_revision"]["status"] == "failed" and rep["builds"]["proposed_build_failed"]
    assert b["base_revision"]["artifacts"] == [{"name": "payments", "version": "8"}]
    prod = rep["deployments"]["by_repo"]["payments-service"]["production"]
    assert prod["is_base_revision"] and prod["rollbacks"][-1]["explicit"] is False and prod["build"]["build_id"] == "ci-800"
    sig = rep["failure_signatures"]["signatures"][0]
    assert sig["pattern"] == "assert <n> == <n>" and sig["assessment"].startswith("already failing on the base revision")
    assert sig["incidents"][0]["id"] == "INC-77"
    t = next(t for f in rep["findings"] for t in f.get("tests", []) if t["name"] == "test_unknown_account_is_404") if any(
        t["name"] == "test_unknown_account_is_404" for f in rep["findings"] for t in f.get("tests", [])) else None
    if t:
        assert t["failure_signature"] == sig["signature"]
    areas = {u["area"] for u in rep["unknowns"]}
    assert "build_evidence_absent" not in areas and "proposed_revision_not_built" not in areas


def test_the_new_evidence_is_pinned_and_reproduces(world):
    c, out = world
    aid = out["analysis_id"]
    snap = c.get(f"/api/v1/evidence-snapshots/{out['report']['evidence_snapshot_id']}").json()
    assert snap["evidence_ids"]["build"] and snap["evidence_ids"]["incident"]
    assert c.post(f"/api/v1/impact-analyses/{aid}/reproduce").json()["identical"] is True


def test_the_shareable_page_shows_builds_rollbacks_and_signatures(world):
    c, out = world
    page = c.get(f"/analyses/{out['analysis_id']}").text
    assert "<h2>Builds</h2>" in page and "ci-900" in page and "<b>failed</b>" in page
    assert "<b>rolled back</b>" in page and "artifact built by ci-800" in page
    assert "<h2>Failure signatures</h2>" in page and "assert &lt;n&gt; == &lt;n&gt;" in page and "INC-77" in page
    assert "<script" not in page


def test_a_reader_who_cannot_read_the_repository_sees_none_of_it(world):
    c, out = world
    rep = c.get(f"/api/v1/impact-analyses/{out['analysis_id']}", headers={"X-TIA-Readable-Repos": "statement-service"}).json()
    rep = rep.get("report", rep)
    assert "payments-service" not in rep["builds"]["by_repo"]
    assert rep["failure_signatures"]["signatures"] == []
