"""docs/DEMO-DATA.md: the demo inputs give the outputs the page promises (seeded by tools/demo_seed.py, then each section's inputs)."""
import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tools.demo_seed import loyalty_zip, seed


def sev(rep):
    out = {}
    for f in rep["findings"]:
        out[f["severity"]] = out.get(f["severity"], 0) + 1
    return out


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("demo")))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    done = seed(c, tmp_path_factory.mktemp("zips"))
    assert done["test_results"] == {"passed": 3, "failed": 1, "error": 0, "skipped": 0} and done["deployments"] == 4
    assert done["runtime_edges"] == 3 and done["incident"] == "INC-1001"
    return c


def analyse(c, head, target="payments-service", base="main"):
    r = c.post("/api/v1/impact-analyses", json={"target": target, "head": head, "base": base})
    assert r.status_code == 200, r.text
    return r.json()["analysis_id"], r.json()["report"]


def test_graph_and_change_analyses(demo):
    c = demo
    counts = {lv: c.get(f"/api/v1/graph?level={lv}").json() for lv in ("service", "endpoint", "function")}
    assert [(len(g["nodes"]), len(g["edges"])) for g in counts.values()] == [(3, 2), (15, 14), (44, 40)]
    _, b = analyse(c, "behaviour")
    assert sev(b) == {"medium": 5, "low": 1} and b["test_scope"]["summary"]["entry_points"] == 3
    assert [g.get("entry_point") or g.get("component") for g in b["test_gaps"]] == ["mobile-app"]     # seen only in production traffic
    assert {r["test"]: r["last_result"] for r in b["test_recommendations"]["tests"]}["test_list_payments"] == "passed"
    assert sev(analyse(c, "contract")[1]) == {"high": 5}
    assert sev(analyse(c, "signature_unfixed")[1]) == {"high": 5, "low": 1}
    assert sev(analyse(c, "signature")[1]) == {"medium": 5, "low": 1}
    assert analyse(c, "docstring")[1]["findings"] == []


def test_report_actions(demo):
    c = demo
    aid, rep = analyse(c, "contract")
    assert len(c.get(f"/api/v1/impact-analyses/{aid}/findings?severity=high").json()["findings"]) == 5
    teams = [t["team"] for t in c.get(f"/api/v1/impact-analyses/{aid}/coordination").json()["teams"]]
    assert teams == ["team-portal", "team-statements"]
    assert c.post(f"/api/v1/impact-analyses/{aid}/notifications").json()["summary"]["notification_failed"] == 2   # no channel configured
    assert c.post(f"/api/v1/impact-analyses/{aid}/acknowledgements", json={"team": "team-portal", "comment": "ok"}).status_code == 200
    assert c.post(f"/api/v1/impact-analyses/{aid}/verdicts", json={"finding_id": rep["findings"][0]["id"], "verdict": "confirmed"}).status_code == 200
    assert "TIA impact analysis" in c.get(f"/api/v1/impact-analyses/{aid}/comment").text


def test_tester_views(demo):
    c = demo
    s = c.get("/api/v1/graph/shared-components").json()
    assert s["shared_components"][0]["name"] == "list_payments" and len(s["rule_gaps"]) == 1
    cov = c.get("/api/v1/coverage").json()["summary"]
    assert (cov["aspects"], cov["covered"], cov["partial"], cov["missing"], cov["percent"]) == (35, 13, 3, 19, 41.4)
    assert c.get("/api/v1/automation/api").json()["summary"]["cases"] == 29
    assert c.get("/api/v1/automation/ui").json()["summary"] == {"pages": 2, "elements": 14, "without_unique_locator": 2}

    def check(title, entry, steps, expected):
        return c.post("/api/v1/coverage/test-case", json={"title": title, "entry_point": entry, "steps": steps, "expected": expected}).json()["matched"]
    m = check("Amount above the limit", "POST /accounts/{id}/payments", "Send a payment with amount 10001, currency EUR and reference INV778",
              "The service refuses it with 422")
    assert m[0]["percent_of_area"] == 40.9 and "an invalid 'amount' is rejected (<= 10000; > 0)" in [x["title"] for x in m[0]["covers"]]
    m = check("Duplicate reference", "POST /accounts/{id}/payments", "Send the same payment twice with reference INV779",
              "The second one is refused with 409 duplicate reference")
    assert "answers 409 (duplicate reference) - raised in create_payment" in [x["title"] for x in m[0]["covers"]]
    assert check("Pay without amount on the screen", "/accounts/:id/payments", "Open the payments page; leave Amount empty; press Pay",
                 "The form refuses to submit")[0]["percent_of_area"] == 25.0
    assert check("Order history", "GET /orders/{id}", "Open the order history", "Orders are listed") == []


def test_releases_environments_incidents(demo):
    c = demo
    d = c.get("/api/v1/releases/compare?base=2026.10&head=2026.11").json()
    assert sorted((x["from_service"], x["to"]) for x in d["introduced"]) == [
        ("customer-portal", "payments-service:GET /accounts/{}/payments"), ("customer-portal", "payments-service:POST /accounts/{}/payments")]
    assert d["removed"] == [] and d["modified"] == []
    assert c.get("/api/v1/environments/at?at=2026-10-31T12:00:00Z").json()["deployed"] == {}
    at = c.get("/api/v1/environments/at?at=2026-11-02T10:00:00Z").json()["deployed"]
    assert set(at["payments-service"]) == {"staging"}
    inv = c.get("/api/v1/incidents/INC-1001/investigation").json()
    assert sorted((x["repo"], x["associations"][0]) for x in inv["candidates"]) == [
        ("payments-service", "dependency_derived"), ("statement-service", "time_correlated")]


def test_configuration_and_upload(demo):
    c = demo
    csv = "service,owner,criticality\nstatement-service,team-statements,critical\ncustomer-portal,team-portal,high\n"
    r = c.post("/api/v1/registry/import?format=csv", content=csv, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.json()["services"] == 2
    assert sev(analyse(c, "contract")[1]) == {"critical": 2, "high": 3}
    cfg = c.get("/api/v1/tenant/config").json()["config"]
    bad = {**cfg, "limits": {**cfg["limits"], "max_depth": 0}}
    assert c.post("/api/v1/tenant/config/validate", json={"config": bad}).json()["valid"] is False
    assert c.put("/api/v1/tenant/config", json={"config": {**cfg, "limits": {**cfg["limits"], "max_depth": 1}}}).status_code == 200
    assert sev(analyse(c, "behaviour")[1]) == {"low": 1}
    assert c.put("/api/v1/tenant/config", json={"config": cfg}).status_code == 200
    for v in (1, 2):
        s = c.post("/api/v1/uploads", json={"name": "loyalty-service"}).json()
        c.put(f"/api/v1/uploads/{s['upload_id']}/archive", content=loyalty_zip(v), headers={"Content-Type": "application/zip"})
        assert c.post(f"/api/v1/uploads/{s['upload_id']}/commit").json()["files"] == 4
        if v == 1:
            assert len(c.get("/api/v1/graph?level=service").json()["nodes"]) == 4
            comps = {f["component"] for f in analyse(c, "contract")[1]["findings"]}
            assert comps == {"customer-portal", "loyalty-service", "mobile-app", "statement-service"}
    _, rep = analyse(c, "main", target="loyalty-service", base="main~1")
    assert sev(rep) == {"low": 1}
    assert [(e["entry_point"], [t["name"] for t in e["tests"]]) for e in rep["test_scope"]["entry_points"]] == [
        ("GET /loyalty/{account_id}/points", ["test_loyalty_points"])]
