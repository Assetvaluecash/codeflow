"""The tester pack of one analysis (tia.testerpack): for the entry points a change reaches, what to retest with a coverage gate, the
locator pack of the pages calling them and the API test matrix; as JSON and as Knowledge Fabric chunks. Demo landscape of DEMO-DATA.md."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tools.demo_seed import seed

PACK_CATEGORIES = ("coverage_gate", "coverage_gap", "api_matrix", "ui_locators")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("pack")))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    seed(c)

    def analyse(head):
        r = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": head, "base": "main"})
        assert r.status_code == 200, r.text
        return r.json()["analysis_id"]
    return c, {h: analyse(h) for h in ("contract", "docstring")}


def pack(c, aid, **headers):
    r = c.get(f"/api/v1/impact-analyses/{aid}/tester-pack", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_gate_covers_only_what_the_change_reaches(world):
    c, aids = world
    p = pack(c, aids["contract"])
    g = p["gate"]
    assert g["outcome"] == "gaps" and g["counts"] == {"covered": 2, "gaps": 3, "not_evaluated": 1}
    rows = {r["entry_point"]: r for r in g["entry_points"]}
    assert rows["GET /portal/accounts/{account_id}/payments"]["outcome"] == "covered" and rows["GET /portal/accounts/{account_id}/payments"]["label"] == "Payments screen"
    shared = rows["GET /accounts/{account_id}/payments"]
    assert shared["outcome"] == "gaps" and (shared["covered"], shared["total"], shared["tests_found"]) == (2, 4, 2)
    assert any("'status'" in a for a in shared["missing_aspects"])
    # a consumer known only from production traffic cannot be measured: it is said, never counted as covered
    assert rows["mobile-app (observed at runtime)"]["outcome"] == "not_evaluated"
    # pages that call a touched endpoint are part of the pack
    assert rows["page /accounts/:id/payments"]["kind"] == "page" and rows["page /statements"]["outcome"] == "gaps"
    # the endpoint the change does not reach (create a payment) is in no part of the pack
    assert "POST /accounts/{account_id}/payments" not in json.dumps([p["api"], p["coverage"], g])
    assert p["coverage"]["summary"]["entry_points"] == 5 and p["coverage"]["summary"]["aspects"] == 20
    assert len(p["retest"]["rule_gaps"]) == 1 and len(p["retest"]["consistency_checks"]) == 1 and p["unknowns"]


def test_api_matrix_and_locator_pack_of_the_touched_surface(world):
    c, aids = world
    p = pack(c, aids["contract"])
    assert [e["entry_point"] for e in p["api"]["endpoints"]] == [
        "GET /portal/accounts/{account_id}/payments", "GET /accounts/{account_id}/payments", "GET /statements/{account_id}/pdf"]
    shared = p["api"]["endpoints"][1]
    status = next(i for i in shared["inputs"] if i["name"] == "status")
    assert not status["required"] and [v["value"] for v in status["valid"]] == ["SETTLED", "PENDING"] and shared["error_statuses"][0]["status"] == 404
    assert p["ui"]["summary"] == {"pages": 2, "elements": 14, "without_unique_locator": 2}
    page = next(x for x in p["ui"]["pages"] if x["page"] == "/accounts/:id/payments")
    amount = next(e for e in page["elements"] if e.get("label") == "Amount")
    assert amount["locator"]["css"] == "#amount" and amount["mandatory"] and amount["locator"]["playwright"]
    assert sum(1 for e in page["elements"] if e["locator"]["strategy"] == "position (fragile)") == 2      # the two Export buttons


def test_a_change_that_reaches_nothing_gives_an_empty_pack(world):
    c, aids = world
    p = pack(c, aids["docstring"])
    assert p["gate"]["entry_points"] == [] and "nothing to retest" in p["gate"]["headline"]
    assert p["api"]["endpoints"] == [] and p["ui"]["pages"] == [] and p["coverage"]["entry_points"] == []
    assert p["unknowns"]                                              # the confidence limits still ride along
    assert c.get("/api/v1/impact-analyses/tia-nope/tester-pack").status_code == 404


def test_a_reader_sees_only_readable_repositories(world):
    c, aids = world
    text = json.dumps(pack(c, aids["contract"], **{"X-TIA-Readable-Repos": "payments-service"}))
    for hidden in ("customer-portal", "statement-service", "/portal/", "/statements", "#amount"):
        assert hidden not in text, hidden
    p = json.loads(text)
    assert [e["entry_point"] for e in p["api"]["endpoints"]] == ["GET /accounts/{account_id}/payments"] and p["ui"]["pages"] == []


def test_knowledge_fabric_carries_the_pack_as_chunks(world):
    c, aids = world
    q = c.post("/query/tia", json={"analysis_id": aids["contract"]}).json()
    cats = {}
    for ch in q["chunks"]:
        cats[ch["category"]] = cats.get(ch["category"], 0) + 1
    assert {k: cats[k] for k in PACK_CATEGORIES} == {"coverage_gate": 1, "coverage_gap": 4, "api_matrix": 3, "ui_locators": 2}
    assert cats["test_scope"] and cats["rule_gap"] == 1 and cats["unknown"]         # what was there before still is
    by = {ch["block_id"].split("#")[1]: ch for ch in q["chunks"]}
    assert by["gate"]["text"].startswith("COVERAGE GATE: GAPS") and json.loads(by["gate"]["extracted_fields"])["outcome"] == "gaps"
    not_eval = next(ch for ch in q["chunks"] if ch["category"] == "coverage_gap" and "mobile-app" in ch["text"])
    assert not_eval["text"].startswith("COVERAGE NOT EVALUATED")
    matrix = next(ch for ch in q["chunks"] if ch["category"] == "api_matrix" and "(payments-service)" in ch["text"])
    know = json.loads(matrix["extracted_knowledge"])
    assert know["method"] == "GET" and len(know["cases"]) == 3 and {i["name"] for i in know["inputs"]} == {"account_id", "status"}
    loc = json.loads(next(ch for ch in q["chunks"] if ch["category"] == "ui_locators" and "/statements" in ch["text"])["extracted_knowledge"])
    assert {e["name"]: e["locator"]["css"] for e in loc["elements"]}["Download PDF"] == '[data-testid="download-statement"]'
    assert next(e for e in loc["elements"] if e["name"] == "Month")["options"] == ["2026-09", "2026-10", "2026-11"]
    # ranked answers: the gate first among the pack chunks, unknowns always last, each kind bounded by top_k
    ranked = [ch["category"] for ch in q["reranked_chunks"]]
    assert ranked.index("coverage_gate") < ranked.index("coverage_gap") < ranked.index("api_matrix") < ranked.index("ui_locators") < ranked.index("unknown")
    small = c.post("/query/tia", json={"analysis_id": aids["contract"], "top_k_semantic": 1}).json()
    assert sum(1 for ch in small["reranked_chunks"] if ch["category"] == "coverage_gap") == 1
    # opting out gives exactly the earlier answer
    plain = c.post("/query/tia", json={"analysis_id": aids["contract"], "include_tester_pack": False}).json()
    assert not [ch for ch in plain["chunks"] if ch["category"] in PACK_CATEGORIES] and len(plain["chunks"]) == len(q["chunks"]) - 10
    # a restricted reader's agent answer names nothing of the hidden repositories
    hidden = c.post("/query/tia", json={"analysis_id": aids["contract"]}, headers={"X-TIA-Readable-Repos": "payments-service"}).text
    assert "customer-portal" not in hidden and "#amount" not in hidden


def test_the_report_tables_explain_why_in_parts_and_draw_each_row(world):
    """The page builds 'Why' from these report fields and draws each row's path: a finding and a test-scope row must carry them."""
    from pathlib import Path
    c, aids = world
    rep = c.get(f"/api/v1/impact-analyses/{aids['contract']}").json()
    f = rep["findings"][0]
    assert ". Change: " in f["reason"] and len(f["path"]) >= 2 and len(f["path_edges"]) == len(f["path"]) - 1 and f["node_type"] and f["evidence"]["file"]
    e = rep["test_scope"]["entry_points"][0]
    assert e["changes"] and len(e["path"]) >= 2 and len(e["path_edges"]) == len(e["path"]) - 1 and e["type"] == "endpoint"
    assert all(p["type"] for p in f["path"] + e["path"])
    html = (Path(__file__).resolve().parent.parent / "tia" / "web" / "index.html").read_text(encoding="utf-8")
    for needle in ("function whyHtml", "What changed", "Why this is affected", "Path from the change", "<th style=\"width:8%\">Graph</th>",
                   "function showPathGraph", "const TYPE_COLOR", 'id="typelegend"', "node[tcolor]", "table-layout:fixed"):
        assert needle in html, needle


def test_the_page_has_the_tester_pack_block():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "tia" / "web" / "index.html").read_text(encoding="utf-8")
    for needle in ("Tester pack for this change", 'id="packgate"', 'data-ptab="retest"', 'data-ptab="ui"', 'data-ptab="api"', 'id="packjson"',
                   'id="kfjson"', "function renderPack", "/tester-pack", "const covHtml", "const apiHtml", "const uiHtml"):
        assert needle in html, needle
