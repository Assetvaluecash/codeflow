"""PRD 9.3 report filters: severity, service/repository, direct vs transitive, impact kind, evidence status, environment, policy result and
evidence gaps - as a pure function and through GET /api/v1/impact-analyses/{aid}/findings."""
import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_polyglot_fixture import build as build_poly
from tia.findingfilter import apply

REPORT = {
    "findings": [
        {"id": "F1", "severity": "high", "component": "orders", "repository": "orders-repo", "impact_type": "contract", "depth": 2,
         "evidence_status": "source_confirmed", "tests": [{"name": "t"}], "limitations": []},
        {"id": "F2", "severity": "medium", "component": "billing", "repository": "billing-repo", "impact_type": "transitive", "depth": 5,
         "evidence_status": "inferred", "limitations": ["Relationship is inferred"]},
        {"id": "F3", "severity": "high", "component": "ledger", "repository": "ledger-repo", "impact_type": "data", "depth": 3,
         "evidence_status": "stale", "data": {"table": "accounts"}, "runtime": {"environments": ["production"]}, "limitations": []},
    ],
    "deployments": {"by_repo": {"orders-repo": {"staging": {"revision": "a"}}}},
    "policy_results": [{"id": "nb", "outcome": "fail", "evidence": [{"finding": "F1"}]}, {"id": "tests", "outcome": "requires_review", "evidence": []}],
}


@pytest.mark.parametrize("kw,ids", [
    ({}, ["F1", "F2", "F3"]),
    ({"severity": "high"}, ["F1", "F3"]),
    ({"severity": "high,medium", "service": "billing"}, ["F2"]),
    ({"service": "ledger-repo"}, ["F3"]),
    ({"reach": "transitive"}, ["F2"]),
    ({"reach": "direct"}, ["F1", "F3"]),
    ({"kind": "contract"}, ["F1"]),
    ({"kind": "data,test"}, ["F1", "F3"]),
    ({"kind": "runtime"}, ["F3"]),
    ({"kind": "operational"}, ["F1"]),
    ({"evidence": "inferred,stale"}, ["F2", "F3"]),
    ({"environment": "production"}, ["F3"]),
    ({"environment": "staging"}, ["F1"]),
    ({"policy": "fail"}, ["F1"]),
    ({"policy": "requires_review"}, ["F1", "F2", "F3"]),        # a rule naming no finding concerns the whole change
    ({"issue": "stale"}, ["F3"]),
    ({"issue": "unresolved"}, ["F2"]),
    ({"severity": "high", "reach": "direct", "kind": "data"}, ["F3"]),
])
def test_filters(kw, ids):
    r = apply(REPORT, **kw)
    assert [f["id"] for f in r["findings"]] == ids and r["total"] == 3 and r["matched"] == len(ids)


def test_unknown_filter_values_are_refused():
    for kw in ({"reach": "sideways"}, {"kind": "vibes"}, {"issue": "bad"}):
        with pytest.raises(ValueError):
            apply(REPORT, **kw)


def test_the_api_filters_the_stored_report(tmp_path):
    poly = build_poly(str(tmp_path / "land"))
    c = TestClient(make_app(tmp_path / "ws", allow_local=True))
    for n, p in poly["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    rep = c.post("/api/v1/impact-analyses", json={"target": "pricing-service", "head": "field_removed"}).json()["report"]
    url = f"/api/v1/impact-analyses/{rep['analysis_id']}/findings"
    allf = c.get(url).json()
    assert allf["matched"] == allf["total"] == len(rep["findings"])
    tr = c.get(url, params={"reach": "transitive"}).json()
    assert tr["findings"] and all(f["impact_type"] == "transitive" for f in tr["findings"]) and tr["filters"] == {"reach": ["transitive"]}
    assert {f["component"] for f in c.get(url, params={"service": "billing-service"}).json()["findings"]} == {"billing-service"}
    assert c.get(url, params={"kind": "nonsense"}).status_code == 400
    assert c.get("/api/v1/impact-analyses/nope/findings").status_code == 404
