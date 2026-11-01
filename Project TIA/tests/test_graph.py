import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tia import gitutil  # noqa: E402
from conftest import make_app  # noqa: E402
from tia.extract_python import extract_repo  # noqa: E402
from tia.graphview import build_view, impact_overlay, landscape_stats, restrict  # noqa: E402


@pytest.fixture(scope="module")
def extracts(fx):
    return {n: extract_repo(n, gitutil.resolve_rev(p, "main"), gitutil.read_tree(p, "main"))
            for n, p in fx["paths"].items()}


def ids(view, t=None):
    return {n["id"] for n in view["nodes"] if t is None or n["type"] == t}


def test_service_level_shows_every_consumer_with_evidence(extracts):
    v = build_view(extracts, "service")
    assert ids(v) == {f"svc:{s}" for s in ("payments-service", "reconciliation-service", "ledger-sync", "mobile-bff",
                                          "audit-reporting", "notifications")}
    by_src = {e["source"].split(":")[1]: e for e in v["edges"] if e["target"] == "svc:payments-service"}
    assert set(by_src) == {"reconciliation-service", "ledger-sync", "mobile-bff", "audit-reporting"}
    assert by_src["reconciliation-service"]["status"] == "source_confirmed" and by_src["reconciliation-service"]["strategies"] == ["R2"]
    assert by_src["mobile-bff"]["strategies"] == ["R1"]
    assert by_src["audit-reporting"]["status"] == "contract_confirmed"
    assert by_src["ledger-sync"]["status"] == "inferred" and by_src["ledger-sync"]["inferred_only"] is True
    assert "notifications" not in by_src                       # unresolved calls never become edges
    n = next(n for n in v["nodes"] if n["id"] == "svc:notifications")
    assert n["unresolved_calls"] == 1                           # but are visible as a count on the node
    assert next(n for n in v["nodes"] if n["id"] == "svc:reconciliation-service")["criticality"] == "critical"


def test_endpoint_level_shows_unresolved_and_schemas(extracts):
    v = build_view(extracts, "endpoint")
    assert "payments-service:GET /payments/{}/status" in ids(v, "endpoint")
    assert "payments-service:schema:PaymentStatus" in ids(v, "schema")
    unres = [n for n in v["nodes"] if n["type"] == "unresolved"]
    assert len(unres) == 1 and unres[0]["repo"] == "notifications" and "statically resolvable" in unres[0]["reason"]
    rels = {e["rel"] for e in v["edges"]}
    assert {"EXPOSES", "USES_SCHEMA", "CONSUMES_API", "UNRESOLVED_CALL"} <= rels
    cons = [e for e in v["edges"] if e["rel"] == "CONSUMES_API"]
    assert {e["source"] for e in cons} == {"svc:reconciliation-service", "svc:ledger-sync", "svc:mobile-bff", "svc:audit-reporting"}


def test_function_level_filters_focus_search_limit(extracts):
    full = build_view(extracts, "function", limit=5000)
    types = {n["type"] for n in full["nodes"]}
    assert {"function", "callsite", "test", "endpoint", "schema", "service", "contract_ref"} <= types
    only = build_view(extracts, "function", repos=["reconciliation-service"])
    assert {n["repo"] for n in only["nodes"]} == {"reconciliation-service"}
    f = build_view(extracts, "function", focus="reconciliation-service:client.py::fetch_status", depth=1)
    assert 1 < len(f["nodes"]) < len(full["nodes"])
    s = build_view(extracts, "endpoint", q="reconciliations")
    assert any("reconciliations" in n["label"] for n in s["nodes"])
    assert build_view(extracts, "endpoint", q="zzz-nothing")["nodes"] == []
    small = build_view(extracts, "function", limit=10)
    assert small["stats"]["truncated"] is True and len(small["nodes"]) == 10
    assert all(e["source"] in ids(small) and e["target"] in ids(small) for e in small["edges"])    # no dangling edges
    with pytest.raises(KeyError):
        build_view(extracts, "function", focus="nope")
    with pytest.raises(ValueError):
        build_view(extracts, "galaxy")


def test_view_is_deterministic(extracts):
    assert json.dumps(build_view(extracts, "endpoint"), sort_keys=True) == json.dumps(build_view(extracts, "endpoint"), sort_keys=True)


def test_restrict_hides_unreadable_repos(extracts):
    v = restrict(build_view(extracts, "service"), ["payments-service", "mobile-bff"])
    assert ids(v) == {"svc:payments-service", "svc:mobile-bff"} and len(v["edges"]) == 1
    assert v["stats"]["hidden_by_permissions"] == 4
    assert "reconciliation" not in json.dumps(v)


def test_stats(extracts):
    s = {r["repo"]: r for r in landscape_stats(extracts)}
    assert s["payments-service"]["endpoints"] == 2 and s["notifications"]["unresolved_calls"] == 1


# ---- over HTTP, including the impact overlay -------------------------------------------------------------------
def test_graph_api_and_impact_overlay(fx, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    assert c.get("/api/v1/graph").json()["nodes"] == []                   # empty landscape: empty graph, not an error
    for n, p in fx["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    v = c.get("/api/v1/graph?level=endpoint").json()
    assert v["stats"]["nodes"] > 10 and v["stats"]["unresolved_call_sites"] == 1
    assert c.get("/api/v1/graph?level=nope").status_code == 400
    assert c.get("/api/v1/graph?level=function&focus=ghost").status_code == 404
    limited = c.get("/api/v1/graph?level=service", headers={"X-TIA-Readable-Repos": "payments-service"}).json()
    assert {n["id"] for n in limited["nodes"]} == {"svc:payments-service"}
    assert len(c.get("/api/v1/graph/stats").json()) == 6
    assert c.get("/vendor/cytoscape.min.js").status_code == 200

    r = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "breaking", "base": "main"}).json()
    o = c.get(f"/api/v1/impact-analyses/{r['analysis_id']}/graph").json()
    roles = {n["id"]: n["role"] for n in o["nodes"]}
    assert "changed" in roles.values() and "affected" in roles.values()
    sev = {n["repo"]: n["severity"] for n in o["nodes"] if n["role"] == "affected" and n["type"] in ("callsite", "contract_ref")}
    assert sev["reconciliation-service"] == "critical" and sev["mobile-bff"] == "high" and sev["ledger-sync"] == "medium"
    inferred = [e for e in o["edges"] if e["status"] == "inferred"]
    assert inferred and all(e["source"] and e["target"] for e in o["edges"])
    ids_ = {n["id"] for n in o["nodes"]}
    assert all(e["source"] in ids_ and e["target"] in ids_ for e in o["edges"])
    red = c.get(f"/api/v1/impact-analyses/{r['analysis_id']}/graph", headers={"X-TIA-Readable-Repos": "payments-service,mobile-bff"}).json()
    assert {n["repo"] for n in red["nodes"]} <= {"payments-service", "mobile-bff"}


def test_overlay_of_failed_analysis_is_empty():
    assert impact_overlay({"analysis_id": "x", "status": "failed"})["nodes"] == []
