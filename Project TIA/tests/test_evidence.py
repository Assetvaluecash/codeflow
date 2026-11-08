"""Release 2 evidence (PRD FR-022 to FR-029, AC-007, AC-008, AC-027, AC-029): CI results, coverage, runtime observations and deployments,
ingested before an analysis, pinned in its snapshot and never queried live."""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tia.evidence import EvidenceError, parse_coverage, parse_deployments, parse_junit, parse_otlp, parse_test_json

PORTAL, PDF, SHARED = "GET /portal/accounts/{account_id}/payments", "GET /statements/{account_id}/pdf", "GET /accounts/{account_id}/payments"

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_payments" name="test_list_payments" file="tests/test_payments.py" time="0.12"/>
  <testcase classname="tests.test_payments" name="test_unknown_account_is_404" time="0.05"><failure message="assert 500 == 404">boom</failure></testcase>
  <testcase classname="tests.test_payments" name="test_create_payment" time="0.01"><skipped message="needs db"/></testcase>
</testsuite></testsuites>"""


def otlp(spans):
    """OTLP/JSON with one resource per (service, environment, version)."""
    by = {}
    for s in spans:
        by.setdefault((s["service"], s.get("env", "production"), s.get("version", "1.0")), []).append(s)
    return json.dumps({"resourceSpans": [
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}},
                                     {"key": "deployment.environment", "value": {"stringValue": env}},
                                     {"key": "service.version", "value": {"stringValue": ver}}]},
         "scopeSpans": [{"spans": [{"traceId": "t", "spanId": s["id"], "parentSpanId": s.get("parent", ""), "kind": s.get("kind", 3),
                                    "startTimeUnixNano": "1793000000000000000", "endTimeUnixNano": "1793000001000000000",
                                    "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in s.get("attrs", {}).items()]}
                                   for s in ss]}]} for (svc, env, ver), ss in by.items()]})


# ---- parsers ----------------------------------------------------------------------------------------------------------------------------
def test_junit_results():
    tests = {t["name"]: t for t in parse_junit(JUNIT)}
    assert tests["test_list_payments"]["result"] == "passed" and tests["test_list_payments"]["duration_s"] == 0.12
    assert tests["test_unknown_account_is_404"]["result"] == "failed" and tests["test_unknown_account_is_404"]["message"] == "assert 500 == 404"
    assert tests["test_create_payment"]["result"] == "skipped"


def test_unsafe_or_empty_xml_is_refused():
    with pytest.raises(EvidenceError, match="DOCTYPE"):
        parse_junit('<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="&a;"/></testsuite>')
    with pytest.raises(EvidenceError, match="no <testcase>"):
        parse_junit("<testsuite/>")
    with pytest.raises(EvidenceError, match="not valid XML"):
        parse_junit("<testsuite>")


def test_pytest_json_and_plain_lists():
    t = parse_test_json(json.dumps({"tests": [{"nodeid": "tests/test_a.py::TestX::test_y[1]", "outcome": "passed", "duration": 0.2}]}))[0]
    assert (t["name"], t["classname"], t["file"], t["result"]) == ("test_y", "tests.test_a.TestX", "tests/test_a.py", "passed")
    with pytest.raises(EvidenceError, match="result must be"):
        parse_test_json(json.dumps([{"name": "x", "result": "green"}]))


def test_coverage_formats():
    cob = parse_coverage('<coverage><packages><package><classes><class filename="app.py"><lines><line number="1" hits="1"/>'
                         '<line number="2" hits="0"/><line number="3" hits="4"/></lines></class></classes></package></packages></coverage>')
    assert cob["format"] == "cobertura" and cob["files"]["app.py"] == {"covered": [[1, 1], [3, 3]], "lines": 3}
    jac = parse_coverage('<report name="x"><package name="com/acme"><sourcefile name="C.java"><line nr="5" mi="0" ci="2"/>'
                         '<line nr="6" mi="1" ci="0"/></sourcefile></package></report>')
    assert jac["format"] == "jacoco" and jac["files"]["com/acme/C.java"]["covered"] == [[5, 5]]
    cp = parse_coverage(json.dumps({"files": {"app.py": {"executed_lines": [1, 2, 3], "missing_lines": [9],
                                                        "contexts": {"2": ["tests/test_a.py::test_b|run"], "3": ["tests/test_a.py::test_b|run", ""]}}}}))
    assert cp["per_test"] == {"tests/test_a.py::test_b": {"app.py": [[2, 3]]}} and cp["files"]["app.py"]["lines"] == 4


def test_otlp_traces_become_observed_calls():
    edges = parse_otlp(otlp([
        {"service": "statement-service", "id": "a", "kind": 3, "attrs": {}},
        {"service": "payments-service", "id": "b", "parent": "a", "kind": 2, "attrs": {"http.route": "/accounts/{account_id}/payments", "http.request.method": "GET"}},
        {"service": "payments-service", "id": "c", "parent": "b", "kind": 1, "attrs": {}},                  # internal: not a service call
    ]))
    assert edges == [{"from_service": "statement-service", "to_service": "payments-service", "method": "GET", "path": "/accounts/{}/payments",
                      "route": "/accounts/{account_id}/payments", "environment": "production", "count": 1,
                      "from_ts": "2026-10-26T07:33:20+00:00", "to_ts": "2026-10-26T07:33:21+00:00", "from_versions": ["1.0"], "to_versions": ["1.0"]}]
    with pytest.raises(EvidenceError):
        parse_otlp("{}")


def test_deployments_need_a_commit():
    d = parse_deployments([{"repo": "x", "environment": "Production", "revision": "ABCDEF1", "deployed_at": "2026-11-01T10:00:00Z"}])[0]
    assert d["environment"] == "production" and d["revision"] == "abcdef1" and d["deployed_at"] == "2026-11-01T10:00:00+00:00"
    with pytest.raises(EvidenceError, match="commit id"):
        parse_deployments([{"repo": "x", "environment": "prod", "revision": "v1.2"}])


# ---- end to end over HTTP ------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("ev")))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    head = land["shas"]["behaviour"]
    stmt_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=land["paths"]["statement-service"], capture_output=True, text=True).stdout.strip()
    r = c.post(f"/api/v1/evidence/test-results?repo=payments-service&revision={head}&build_id=ci-77&environment=ci", content=JUNIT)
    assert r.status_code == 200 and r.json()["counts"] == {"passed": 1, "failed": 1, "error": 0, "skipped": 1}
    cov = {"files": {"app.py": {"executed_lines": list(range(1, 80)), "missing_lines": [],
                                "contexts": {str(n): ["tests/test_payments.py::test_list_payments|run"] for n in range(40, 60)}}}}
    assert c.post(f"/api/v1/evidence/coverage?repo=payments-service&revision={head}", content=json.dumps(cov)).json()["tests_with_contexts"] == 1
    spans = [{"service": "statement-service", "id": "a"},
             {"service": "payments-service", "id": "b", "parent": "a", "kind": 2, "attrs": {"http.route": "/accounts/{account_id}/payments", "http.method": "GET"}},
             {"service": "mobile-app", "id": "m", "env": "production"},
             {"service": "payments-service", "id": "n", "parent": "m", "kind": 2, "attrs": {"url.path": "/accounts/123/payments", "http.method": "GET"}}]
    assert c.post("/api/v1/evidence/runtime?format=otlp&source=otel-collector", content=otlp(spans)).json()["edges"] == 2
    deps = [{"repo": "statement-service", "environment": "production", "revision": "0" * 40},
            {"repo": "statement-service", "environment": "staging", "revision": stmt_head},
            {"repo": "payments-service", "environment": "production", "revision": land["shas"]["base"]}]
    assert c.post("/api/v1/evidence/deployments", content=json.dumps(deps)).json()["deployments"] == 3
    rep = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "behaviour", "base": "main"}).json()["report"]
    return c, land, rep


def entry(rep, name):
    return next(e for e in rep["test_scope"]["entry_points"] if e["entry_point"] == name)


def test_ci_results_give_distinct_test_states(world):
    c, land, rep = world
    tests = {t["name"]: t for t in entry(rep, SHARED)["tests"]}
    ok = tests["test_list_payments"]
    assert ok["result"] == "passed" and ok["run"]["build"] == "ci-77" and ok["run"]["of_analysed_revision"] is True
    assert ok["states"] == ["exists", "associated", "passed", "executed", "validated_contract"]
    bad = tests["test_unknown_account_is_404"]
    assert bad["result"] == "failed" and "executed" not in bad["states"] and not bad["validated_contract"]
    assert bad["executed_component"] is False                                   # per-test coverage exists and does not include it
    portal = entry(rep, PORTAL)["tests"][0]
    assert portal["result"] == "no_ci_results" and portal["states"] == ["exists", "associated"] and portal["executed_component"] is None
    unk = {u["area"]: u for u in rep["unknowns"]}
    assert {i["repo"] for i in unk["ci_results_unavailable"]["items"]} == {"customer-portal", "statement-service"}


def test_coverage_drives_regression_recommendations(world):
    c, land, rep = world
    recs = rep["test_recommendations"]
    top = recs["tests"][0]
    assert top["test"] == "test_list_payments" and top["priority"] == 1 and "executed the changed function ordered" in top["why"][0]
    assert {r["test"] for r in recs["tests"]} >= {"test_portal_payments", "test_statement_pdf"} and "not shown to be safe to skip" in recs["note"]
    ordered = next(x for x in rep["change"]["changed_components"] if x["type"] == "function")
    assert ordered["coverage"]["tests_executing"] == ["tests/test_payments.py::test_list_payments"]


def test_runtime_observations_corroborate_and_add_runtime_only_callers(world):
    c, land, rep = world
    stmt = next(f for f in rep["findings"] if f["component"] == "statement-service" and f["node_type"] == "callsite")
    assert stmt["evidence_status"] == "corroborated" and stmt["runtime"]["environments"] == ["production"] and stmt["runtime"]["calls_observed"] == 1
    portal = next(f for f in rep["findings"] if f["component"] == "customer-portal" and f["node_type"] == "callsite")
    assert portal["evidence_status"] == "source_confirmed" and "runtime" not in portal      # not observed: still reported (AC-008)
    ext = next(f for f in rep["findings"] if f["node_type"] == "runtime_ref")
    assert ext["component"] == "mobile-app" and ext["repository"] == "(outside the analysed repositories)" and ext["evidence_status"] == "runtime_observed"
    assert ext["resolution_strategy"] == "R6" and ext["runtime"]["window"] == ["2026-10-26T07:33:20+00:00", "2026-10-26T07:33:21+00:00"]
    assert any("known only from runtime telemetry" in x for x in ext["limitations"])
    top = {e["entry_point"] for e in rep["test_scope"]["code_without_entry_point"]}
    assert "mobile-app (observed at runtime)" in top
    assert any(u["area"] == "runtime_evidence_scope" for u in rep["unknowns"])


def test_deployments_distinguish_proposed_and_deployed_revisions(world):
    c, land, rep = world
    d = rep["deployments"]["by_repo"]
    assert d["payments-service"]["production"]["is_base_revision"] and not d["payments-service"]["production"]["is_proposed_revision"]
    assert d["statement-service"]["staging"]["is_analysed_revision"] and not d["statement-service"]["production"]["is_analysed_revision"]
    stmt = next(f for f in rep["findings"] if f["component"] == "statement-service" and f["node_type"] == "callsite")
    assert any(x.startswith("production runs statement-service at 0000000") for x in stmt["limitations"])
    unk = next(u for u in rep["unknowns"] if u["area"] == "environment_mapping_incomplete")
    assert {"repo": "customer-portal", "environments": ["development", "testing", "staging", "production"]} in unk["items"]
    env = c.get("/api/v1/environments").json()
    assert env["by_repo"]["statement-service"]["staging"]["is_analysed_revision"] and env["runtime_edges_by_environment"] == {"production": 1}


def test_a_reproduction_reads_the_pinned_evidence(world):
    c, land, rep = world
    r = c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/reproduce").json()
    assert r["identical"], r["differences"]
    late = c.post("/api/v1/evidence/runtime?format=edges", content=json.dumps([{"from_service": "customer-portal", "to_service": "payments-service",
                                                                               "method": "GET", "path": "/accounts/{id}/payments"}])).json()
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/reproduce").json()["identical"]      # newer evidence does not change it
    assert c.delete(f"/api/v1/evidence/{late['ids'][0]}").status_code == 200


def test_restricted_readers_never_see_outside_callers_or_other_repositories(world):
    c, land, rep = world
    limited = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}", headers={"X-TIA-Readable-Repos": "payments-service,customer-portal"}).json()
    assert "mobile-app" not in json.dumps(limited["findings"]) and "statement-service" not in json.dumps(limited["findings"])
    ev = c.get("/api/v1/evidence?kind=test_run", headers={"X-TIA-Readable-Repos": "customer-portal"}).json()
    assert ev["records"] == []
    assert c.get("/api/v1/evidence?kind=test_run").json()["records"][0]["data"]["counts"]["failed"] == 1


def test_bad_evidence_is_refused(world):
    c, land, rep = world
    assert c.post("/api/v1/evidence/test-results?repo=payments-service", content="<nope").status_code == 400
    assert c.post("/api/v1/evidence/test-results?repo=ghost", content=JUNIT).status_code == 404
    assert c.post("/api/v1/evidence/deployments", content='[{"repo": "ghost", "environment": "x", "revision": "abcdef1"}]').status_code == 404
    assert c.get("/api/v1/evidence?kind=nope").status_code == 400
    assert c.delete("/api/v1/evidence/999999").status_code == 404


def test_without_evidence_nothing_is_concluded_from_its_absence(run):
    rep = run("breaking", use_store=False)
    areas = {u["area"] for u in rep["unknowns"]}
    assert {"runtime_evidence_absent", "deployment_evidence_absent", "coverage_unavailable"} <= areas
    assert all(t["result"] == "no_ci_results" for f in rep["findings"] for t in f["tests"])
