"""Release 2 governance: release-policy rules and evaluation (PRD FR-033, FR-034, AC-013, AC-014), advisory and enforcement modes (FR-036,
AC-030) on the check run and the Azure DevOps status, owner approvals, and the ownership/criticality registry connector."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tia.github import check_run_payload
from tia.policy import ado_state, check_conclusion
from tia.registry import parse_registry
from tia.report import render_comment

POLICY = {"mode": "enforcing", "rules": [
    {"id": "no-breaking-critical", "type": "no_breaking_change", "applies_to": {"criticality": ["critical"]}},
    {"id": "tests-pass", "type": "tests_passed"},
    {"id": "contracts", "type": "contract_validated", "mandatory": False},
    {"id": "owners", "type": "owner_approval"},
    {"id": "complete", "type": "analysis_complete", "mandatory": False},
    {"id": "staging", "type": "production_evidence", "mandatory": False, "params": {"environment": "staging"}}]}
JUNIT = """<testsuite><testcase classname="tests.test_payments" name="test_list_payments"/>
<testcase classname="tests.test_payments" name="test_unknown_account_is_404"><failure message="500 != 404"/></testcase></testsuite>"""


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("pol")))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    assert c.put("/api/v1/tenant/config", json={"config": {"policies": POLICY}, "comment": "policy"}).status_code == 200
    run = lambda head: c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": head, "base": "main"}).json()["report"]
    return c, land, run


def results(rep):
    return {r["id"]: r for r in rep["policy_results"]}


def test_without_ci_results_test_rules_are_not_evaluated(world):
    c, land, run = world
    rep = run("behaviour")
    r = results(rep)
    assert rep["policy"]["mode"] == "enforcing"
    assert r["tests-pass"]["outcome"] == "not_evaluated" and "No CI results" in r["tests-pass"]["reason"]          # AC-014: never pass
    assert r["contracts"]["outcome"] == "pass"                                    # a behaviour change alters no contract
    assert r["no-breaking-critical"]["outcome"] == "pass" and r["complete"]["outcome"] == "requires_review"
    assert r["staging"]["outcome"] == "not_evaluated"


def test_a_failed_test_fails_the_rule_with_the_supporting_result(world):
    c, land, run = world
    head = land["shas"]["behaviour"]
    assert c.post(f"/api/v1/evidence/test-results?repo=payments-service&revision={head}&build_id=ci-9", content=JUNIT).status_code == 200
    rep = run("behaviour")
    r = results(rep)["tests-pass"]
    assert r["outcome"] == "fail" and r["evidence"] == [{"test": "test_unknown_account_is_404", "repo": "payments-service", "result": "failed"}]  # AC-013
    assert rep["policy"]["blocking"] is True and check_conclusion({**rep["policy"], "results": rep["policy_results"]}) == "failure"


def test_a_breaking_change_to_a_critical_service_fails(world):
    c, land, run = world
    rep = run("signature_unfixed")
    r = results(rep)["no-breaking-critical"]
    assert r["outcome"] == "fail" and {e["component"] for e in r["evidence"]} >= {"customer-portal", "statement-service"}
    payload = check_run_payload(rep, land["shas"]["signature_unfixed"])
    assert payload["conclusion"] == "failure" and "policy **no-breaking-critical** (mandatory): **fail**" in payload["output"]["summary"]
    assert "Required verification:" in payload["output"]["summary"] and "repository protection decides" in payload["output"]["summary"]
    md = render_comment(rep)
    assert "**Release policy (enforcing): fail** - a mandatory policy failed" in md and "- FAIL `no-breaking-critical`" in md


def test_advisory_mode_never_blocks(world):
    c, land, run = world
    rep = run("signature_unfixed")
    advisory = {**rep, "policy": {**rep["policy"], "mode": "advisory"}}
    assert check_run_payload(advisory, "x")["conclusion"] == "neutral" and "never blocks a merge" in check_run_payload(advisory, "x")["output"]["summary"]
    assert ado_state({**rep["policy"], "mode": "advisory", "results": rep["policy_results"]}) == "notApplicable"
    assert ado_state({**rep["policy"], "results": rep["policy_results"]}) == "failed"
    assert check_conclusion(None) == "neutral"


def test_owner_approvals(world):
    c, land, run = world
    rep = run("behaviour")
    assert rep["owners"] == {"customer-portal": "team-portal", "payments-service": "team-payments", "statement-service": "team-statements"}
    r = results(rep)["owners"]
    assert r["outcome"] == "requires_review" and "team-portal, team-statements" in r["reason"]
    aid = rep["analysis_id"]
    assert c.post(f"/api/v1/impact-analyses/{aid}/approvals", json={"owner": "team-ghost", "decision": "approve"}).status_code == 400
    one = c.post(f"/api/v1/impact-analyses/{aid}/approvals", json={"owner": "team-portal", "decision": "approve"}).json()
    assert next(x for x in one["policy"]["results"] if x["id"] == "owners")["outcome"] == "requires_review"
    two = c.post(f"/api/v1/impact-analyses/{aid}/approvals", json={"owner": "team-statements", "decision": "approve", "comment": "ok"}).json()
    assert next(x for x in two["policy"]["results"] if x["id"] == "owners")["outcome"] == "pass"
    assert c.get(f"/api/v1/analyses/{aid}").status_code in (200, 404)                             # the stored report itself never changes
    assert results(c.get(f"/api/v1/impact-analyses/{aid}").json())["owners"]["outcome"] == "requires_review"
    c.post(f"/api/v1/impact-analyses/{aid}/approvals", json={"owner": "team-statements", "decision": "reject"})
    pol = c.get(f"/api/v1/impact-analyses/{aid}/policy").json()
    assert next(x for x in pol["results"] if x["id"] == "owners")["outcome"] == "fail"
    rerun = run("behaviour")                                                                    # a new analysis of the same revision sees them
    assert results(rerun)["owners"]["outcome"] == "fail"


def test_policy_evidence_is_redacted_for_restricted_readers(world):
    c, land, run = world
    rep = run("signature_unfixed")
    limited = c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}", headers={"X-TIA-Readable-Repos": "payments-service"}).json()
    r = results(limited)["no-breaking-critical"]
    assert r["outcome"] == "fail" and r["withheld_evidence"] >= 2 and "statement" not in json.dumps(r) and "portal" not in json.dumps(r)
    assert {e["component"] for e in r["evidence"]} == {"payments-service"}                    # the reader's own repository stays visible


def test_bad_policies_are_refused(world):
    c, land, run = world
    r = c.put("/api/v1/tenant/config", json={"config": {"policies": {"mode": "block", "rules": [{"id": "x", "type": "magic"}]}}})
    assert r.status_code == 422 and len(r.json()["detail"]["problems"]) == 2


def test_registry_import(world):
    c, land, run = world
    catalog = """apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: statement-service, annotations: {tia/criticality: high}}
spec: {owner: group:default/team-docs, type: service}
---
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: customer-portal, labels: {tier: tier-1}}
spec: {owner: team-web}
---
kind: Group
metadata: {name: team-web}
"""
    r = c.post("/api/v1/registry/import?format=backstage", content=catalog)
    assert r.status_code == 200 and r.json()["created"] and r.json()["services"] == 2
    cfg = c.get("/api/v1/tenant/config").json()["config"]
    assert cfg["ownership"] == {"customer-portal": "team-web", "statement-service": "team-docs"}
    assert cfg["criticality"] == {"customer-portal": "critical", "statement-service": "high"} and cfg["policies"]["mode"] == "enforcing"
    rep = run("behaviour")
    assert rep["owners"]["statement-service"] == "team-docs"
    assert parse_registry("service,owner,criticality\nx,team-x,critical\n", "csv") == [{"service": "x", "owner": "team-x", "criticality": "critical"}]
    assert c.post("/api/v1/registry/import?format=csv", content="service,owner,criticality\nx,t,urgent\n").status_code == 400


def test_a_repositorys_own_catalog_descriptor_is_a_fallback():
    from tia.extract_python import extract_repo
    ex = extract_repo("r", "c", {"catalog-info.yaml": "kind: Component\nmetadata: {name: ledger, labels: {tier: '2'}}\nspec: {owner: team-ledger}\n"})
    assert ex.service == "ledger" and ex.service_map["owner"] == "team-ledger" and ex.service_map["criticality"] == "high"
    ex = extract_repo("r", "c", {"catalog-info.yaml": "kind: Component\nmetadata: {name: ledger}\nspec: {owner: team-ledger}\n",
                                 "tia-services.yaml": "service: ledger-svc\nowner: team-own\n"})
    assert ex.service == "ledger-svc" and ex.service_map["owner"] == "team-own"                       # tia-services.yaml wins
