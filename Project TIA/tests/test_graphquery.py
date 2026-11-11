"""Direct graph questions (tia.graphquery) and the testing tools of MCP and the SDK: every channel gives the REST answer, for the caller."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tia import mcp
from tia.extract_python import extract_repo
from tia.graph import build_graph
from tia.graphquery import query

SHARED = "GET /accounts/{account_id}/payments"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("gq")))
    c = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    aid = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "contract", "base": "main"}).json()["analysis_id"]
    return c, aid


def ask(c, question, component, **kw):
    r = c.get("/api/v1/graph/query", params={"question": question, "component": component, **kw.pop("params", {})}, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def names(rows):
    return [r["name"] for r in rows]


def test_who_consumes_an_endpoint(world):
    c, _ = world
    a = ask(c, "consumers", SHARED)
    assert a["status"] == "ok" and a["component"]["type"] == "endpoint" and a["component"]["service"] == "payments-service"
    res = a["results"]
    assert res["services"] == ["customer-portal", "statement-service"]
    assert sorted(names(res["entry_points"])) == ["GET /portal/accounts/{account_id}/payments", "GET /statements/{account_id}/pdf"]
    assert {"test_list_payments", "test_portal_payments", "test_statement_pdf"} <= set(names(res["tests"]))
    assert all(r["evidence_status"] == "source_confirmed" for r in res["consumers"])
    assert any("outside the analysed repositories" in x for x in a["limits"]) and any("could not be resolved" in x for x in a["limits"])
    # the normalised path and a service name work too; a service stands for its endpoints
    assert ask(c, "consumers", "GET /accounts/{}/payments")["component"]["node"] == a["component"]["node"]
    assert ask(c, "consumers", "payments-service")["results"]["services"] == ["customer-portal", "statement-service"]


def test_what_a_component_depends_on(world):
    c, _ = world
    a = ask(c, "dependencies", "GET /statements/{account_id}/pdf")
    res = a["results"]
    assert res["services"] == ["payments-service"] and names(res["endpoints_of_other_services"]) == [SHARED]
    assert {"statement_pdf", "statement_rows", "fetch_payments", "list_payments", "ordered"} <= set(names(res["dependencies"]))
    depth = {r["name"]: r["depth"] for r in res["dependencies"]}
    assert depth["statement_pdf"] == 1 and depth["ordered"] > depth["list_payments"] > depth["fetch_payments"]


def test_which_tests_cover_a_function_and_what_a_test_covers(world):
    c, _ = world
    a = ask(c, "tests", "ordered")
    by = {t["name"]: t["covers"] for t in a["results"]["tests"]}
    assert by["test_list_payments"] == "through GET /accounts/{account_id}/payments" and "test_statement_pdf" in by
    assert a["results"]["direct"] == 0 and a["results"]["through_callers"] == len(by) and "not because it was seen to execute" in a["limits"][0]
    direct = ask(c, "tests", SHARED)["results"]
    assert direct["direct"] >= 2 and {t["name"] for t in direct["tests"] if t["covers"] == "directly"} >= {"test_list_payments", "test_unknown_account_is_404"}
    t = ask(c, "covers", "test_statement_pdf")
    assert names(t["results"]["attached_to"]) == ["GET /statements/{account_id}/pdf"]
    assert t["results"]["services"] == ["payments-service", "statement-service"] and "ordered" in names(t["results"]["also_exercises"])
    assert ask(c, "covers", "ordered")["status"] == "not_found"                     # 'covers' is asked of a test


def test_an_ambiguous_or_unknown_name_is_never_guessed(world):
    c, _ = world
    a = ask(c, "consumers", "fetch_payments")                                       # one in customer-portal, one in statement-service
    assert a["status"] == "ambiguous" and a["results"] == [] and sorted(x["repo"] for x in a["candidates"]) == ["customer-portal", "statement-service"]
    one = ask(c, "consumers", "fetch_payments", params={"repository": "statement-service"})
    assert one["status"] == "ok" and "GET /statements/{account_id}/pdf" in names(one["results"]["entry_points"])
    assert ask(c, "consumers", a["candidates"][0]["node"])["status"] == "ok"        # or by the candidate's node id
    assert ask(c, "consumers", "no_such_thing")["status"] == "not_found"
    assert c.get("/api/v1/graph/query", params={"question": "guess", "component": "x"}).status_code == 400
    assert c.get("/api/v1/graph/query", params={"question": "tests", "component": " "}).status_code == 400


def test_a_reader_is_answered_from_readable_repositories_only(world):
    c, _ = world
    h = {"X-TIA-Readable-Repos": "payments-service"}
    text = c.get("/api/v1/graph/query", params={"question": "consumers", "component": SHARED}, headers=h).text
    assert "customer-portal" not in text and "statement" not in text and json.loads(text)["results"]["services"] == []
    assert json.loads(c.get("/api/v1/graph/query", params={"question": "tests", "component": "test_statement_pdf"}, headers=h).text)["status"] == "not_found"


def test_inferred_links_are_kept_weak_and_confirmed_paths_win():
    files = {"a.py": "def helper():\n    return 1\n", "b.py": "def helper():\n    return 2\n", "c.py": "def run():\n    return helper()\n",
             "d.py": "def top():\n    return run()\n", "tia-services.yaml": "service: demo\n"}
    g = build_graph({"demo": extract_repo("demo", "c0", files)})
    a = query(g, "consumers", "demo:a.py::helper")
    st = {r["name"]: r["evidence_status"] for r in a["results"]["consumers"]}
    assert st == {"run": "inferred", "top": "inferred"} and any("leads, not facts" in x for x in a["limits"])     # weakest link of the path
    assert {r["name"]: r["evidence_status"] for r in query(g, "consumers", "run")["results"]["consumers"]} == {"top": "source_confirmed"}
    assert query(g, "dependencies", "top", limit=1)["truncated"]
    with pytest.raises(ValueError):
        query(g, "anything", "run")


# ---- MCP: the same answers as tools ------------------------------------------------------------------------------------------------------
def call(c, name, arguments, headers=None):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, headers=headers or {})
    return r.json()


def test_mcp_offers_the_testing_tools_read_only(world):
    c, _ = world
    tools = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_impact_report", "find_impact_report", "get_test_scope", "get_policy_outcome", "get_tester_pack", "get_coverage",
                                          "get_api_inventory", "get_ui_inventory", "check_test_case", "get_shared_components", "query_graph"}
    assert all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] and t["inputSchema"]["additionalProperties"] is False for t in tools)
    assert "never guessed" in mcp.INSTRUCTIONS and "not that it passes" in mcp.INSTRUCTIONS


def test_mcp_tools_give_the_rest_answers(world):
    c, aid = world
    out = lambda name, args, **kw: call(c, name, args, **kw)["result"]                       # noqa: E731
    assert out("get_tester_pack", {"analysis_id": aid})["structuredContent"] == c.get(f"/api/v1/impact-analyses/{aid}/tester-pack").json()
    assert out("get_coverage", {})["structuredContent"]["summary"] == c.get("/api/v1/coverage").json()["summary"]
    assert out("get_api_inventory", {"repositories": "payments-service"})["structuredContent"]["summary"]["endpoints"] == 2
    assert out("get_ui_inventory", {})["structuredContent"]["summary"]["pages"] == 2
    chk = out("check_test_case", {"title": "Amount above the limit", "entry_point": "POST /accounts/{id}/payments",
                                  "steps": "Send a payment with amount 10001", "expected": "refused with 422"})["structuredContent"]
    assert chk["matched"] and any("'amount'" in x["title"] for x in chk["matched"][0]["covers"])
    assert len(out("get_shared_components", {})["structuredContent"]["rule_gaps"]) == 1
    q = out("query_graph", {"question": "consumers", "component": SHARED})["structuredContent"]
    assert q == ask(c, "consumers", SHARED)
    # errors are tool errors, bad arguments are refused, and visibility is the caller's
    miss = out("get_tester_pack", {"analysis_id": "nope"})
    assert miss["isError"] and "unknown analysis" in miss["content"][0]["text"]
    assert out("query_graph", {"question": "guess", "component": "x"})["isError"]
    assert call(c, "query_graph", {"question": "tests"})["error"]["code"] == -32602
    assert call(c, "get_coverage", {"write": True})["error"]["code"] == -32602
    hidden = json.dumps(out("query_graph", {"question": "consumers", "component": SHARED}, headers={"X-TIA-Readable-Repos": "payments-service"}))
    assert "customer-portal" not in hidden


# ---- SDK ----------------------------------------------------------------------------------------------------------------------------------
def test_the_sdk_has_the_testing_calls(world):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk" / "python"))
    from tia_client import TiaClient
    c, aid = world

    def transport(method, url, headers, body, timeout):
        r = c.request(method, url.replace("http://testserver", ""), headers=headers, content=body)
        return r.status_code, r.content
    tia = TiaClient("http://testserver", transport=transport)
    assert tia.tester_pack(aid)["gate"]["outcome"] == "gaps"
    assert tia.coverage()["summary"]["aspects"] == 35 and tia.coverage(["payments-service"])["summary"]["entry_points"] == 2
    assert tia.api_inventory()["summary"]["cases"] == 29 and tia.ui_inventory()["summary"]["elements"] == 14
    assert tia.check_test_case(title="Order history", entry_point="GET /orders/{id}")["matched"] == []
    assert tia.shared_components()["shared_components"][0]["name"] == "list_payments"
    assert tia.consumers_of(SHARED)["results"]["services"] == ["customer-portal", "statement-service"]
    assert tia.dependencies_of("GET /statements/{account_id}/pdf")["results"]["services"] == ["payments-service"]
    assert tia.tests_covering("ordered")["results"]["through_callers"] >= 3
    assert tia.covered_by_test("test_statement_pdf")["results"]["attached_to"][0]["name"] == "GET /statements/{account_id}/pdf"
    assert tia.consumers_of("fetch_payments")["status"] == "ambiguous" and tia.consumers_of("fetch_payments", "customer-portal")["status"] == "ok"
    cats = {ch["category"] for ch in tia.agent_answer(aid)["chunks"]}
    assert {"coverage_gate", "api_matrix", "ui_locators"} <= cats and "coverage_gate" not in {ch["category"] for ch in tia.agent_answer(aid, False)["chunks"]}
