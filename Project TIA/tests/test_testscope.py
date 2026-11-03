"""UC-1 test scope (docs/CASE-STUDY.md): what a change means for a tester - entry points to test, consistency checks, rule gaps between
the consumers of a shared endpoint, common components, and breaking / behaviour / compatible / no-functional-change per changed function."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import CLIENT, PAYMENTS, PORTAL_APP, STATEMENT_APP, build as build_lanes
from tia import kfadapter
from tia.extract_java import extract_file as java_file
from tia.extract_python import extract_file as py_file, extract_repo
from tia.graph import build_graph
from tia.impact import AnalysisRequest, _extract, analyze
from tia.report import redact, render_comment
from tia.reportpage import render_page
from tia.testscope import classify_function, rule_gaps, shared_components, signature_diff

ROOT = Path(__file__).resolve().parent.parent
PORTAL, PDF, SHARED = "GET /portal/accounts/{account_id}/payments", "GET /statements/{account_id}/pdf", "GET /accounts/{account_id}/payments"


@pytest.fixture(scope="module")
def lanes(tmp_path_factory):
    return build_lanes(str(tmp_path_factory.mktemp("lanes")))


@pytest.fixture(scope="module")
def reps(lanes):
    run = lambda b: analyze(AnalysisRequest(repos=lanes["paths"], target="payments-service", base=lanes["shas"]["base"], head=lanes["shas"][b]))
    return {b: run(b) for b in ("behaviour", "move_rule", "signature", "signature_unfixed", "docstring", "contract")}


def eps(rep):
    return {e["entry_point"]: e for e in rep["test_scope"]["entry_points"]}


def fn(src, name="f"):
    return next(f for f in py_file("r", "svc", "m.py", src).functions if f.qualname == name)


# ---- extraction: signatures, documentation-only hashes, item filters ---------------------------------------------------------------
def test_python_signature_and_docstring_free_hash():
    a = fn("def f(a, b=1, *args, c, d=2, **kw) -> int:\n    '''doc'''\n    return a\n")
    assert a.signature == {"params": [{"name": "a", "kind": "positional", "default": False}, {"name": "b", "kind": "positional", "default": True},
                                      {"name": "args", "kind": "var_positional", "default": True}, {"name": "c", "kind": "keyword", "default": False},
                                      {"name": "d", "kind": "keyword", "default": True}, {"name": "kw", "kind": "var_keyword", "default": True}],
                           "returns": "int"}
    b = fn("def f(a, b=1, *args, c, d=2, **kw) -> int:\n    '''other words'''\n    # a comment\n    return a\n")
    c = fn("def f(a, b=1, *args, c, d=2, **kw) -> int:\n    '''doc'''\n    return b\n")
    assert a.hash != b.hash and a.body_hash == b.body_hash           # docstring and comments only
    assert a.body_hash != c.body_hash


@pytest.mark.parametrize("body, expr, fields", [
    ("return [p for p in rows if not p['disputed']]", "not p['disputed']", ["disputed"]),
    ("return [p for p in rows if p.get('status') == 'OK' and p.amount > 0]", "p.get('status') == 'OK' and p.amount > 0", ["amount", "status"]),
    ("return list(filter(lambda x: x['region'] == 'EU', rows))", "x['region'] == 'EU'", ["region"]),
    ("out = []\n    for r in rows:\n        if r['on_notice']:\n            continue\n        out.append(r)\n    return out", "not (r['on_notice'])", ["on_notice"]),
    ("out = []\n    for r in rows:\n        if r['active']:\n            out.append(r)\n    return out", "r['active']", ["active"]),
])
def test_python_item_filters(body, expr, fields):
    f = fn(f"def f(rows):\n    {body}\n")
    assert [(x["expr"], x["fields"]) for x in f.filters] == [(expr, fields)]


def test_conditions_that_do_not_test_item_fields_are_not_rules():
    assert fn("def f(rows, limit):\n    return [p for p in rows if limit > 3]\n").filters == []
    assert fn("def f(rows):\n    return [p for p in rows]\n").filters == []


def test_java_signature_and_stream_filters():
    src = """package com.acme;
public class S {
  public List<Payment> visible(List<Payment> rows, String region) {
    rows.removeIf(p -> p.isDisputed());
    return rows.stream().filter(p -> p.getRegion().equals(region) && p.amount > 0).collect(Collectors.toList());
  }
}"""
    f = next(x for x in java_file("r", "svc", "src/main/java/com/acme/S.java", src).functions if x.qualname.endswith("visible"))
    assert f.signature == {"params": [{"name": "rows", "kind": "positional", "type": "List<Payment>"},
                                      {"name": "region", "kind": "positional", "type": "String"}], "returns": "List<Payment>"}
    assert [x["fields"] for x in f.filters] == [["disputed"], ["amount", "region"]]
    assert f.filters[0]["expr"].startswith("!(")                       # removeIf drops what matches: the kept items are the negation


def test_signature_comparison():
    p = lambda *ps: {"params": [{"name": n, "kind": "positional", "default": d} for n, d in ps], "returns": None}
    assert signature_diff(p(("a", False)), p(("a", False), ("b", True))) == ([], ["parameter 'b' added"])
    assert signature_diff(p(("a", False)), p(("a", False), ("b", False)))[0] == ["parameter 'b' added (required)"]
    assert signature_diff(p(("a", False), ("b", True)), p(("a", False)))[0] == ["parameter 'b' removed"]
    assert signature_diff(p(("a", False), ("b", True)), p(("a", False), ("b", False)))[0] == ["parameter 'b' is no longer optional"]
    assert signature_diff(p(("a", False), ("b", False)), p(("b", False), ("a", False)))[0] == ["positional parameter order changed"]
    j = lambda *ts: {"params": [{"name": f"x{i}", "kind": "positional", "type": t} for i, t in enumerate(ts)], "returns": "void"}
    assert signature_diff(j("int"), {"params": [{"name": "renamed", "kind": "positional", "type": "int"}], "returns": "void"}) == ([], [])
    assert signature_diff(j("int"), j("long"))[0] == ["parameter types (int) -> (long)"]


def test_a_removed_function_is_breaking_only_while_something_still_calls_it():
    base = {f.id: f for f in py_file("r", "s", "m.py", "def a():\n    return 1\n\ndef b():\n    return a()\n").functions}
    gone = {f.id: f for f in py_file("r", "s", "m.py", "def b():\n    return a()\n").functions}
    both = {f.id: f for f in py_file("r", "s", "m.py", "def b():\n    return 2\n").functions}
    assert classify_function("r:m.py::a", base, gone)[0] == "breaking"
    assert classify_function("r:m.py::a", base, both)[0] == "compatible_change"


# ---- the UC-1 landscape: one shared endpoint, two consumers, a rule only one applies ---------------------------------------------
def test_a_behaviour_change_lists_every_entry_point_to_retest_with_its_tests(reps):
    rep = reps["behaviour"]
    comp = next(c for c in rep["change"]["changed_components"] if c["type"] == "function")
    assert comp["effect"] == "behaviour_change" and "logic changed (same signature)" in comp["detail"]
    e = eps(rep)
    assert set(e) == {PORTAL, PDF, SHARED}
    assert e[PORTAL]["label"] == "Payments screen" and e[PDF]["label"] == "Monthly statement PDF"
    assert all(x["change_effect"] == "behaviour_change" and x["evidence_status"] == "source_confirmed" for x in e.values())
    assert [t["name"] for t in e[PORTAL]["tests"]] == ["test_portal_payments"]
    assert [t["name"] for t in e[PDF]["tests"]] == ["test_statement_pdf"]
    assert [t["name"] for t in e[SHARED]["tests"]] == ["test_list_payments", "test_unknown_account_is_404"]   # consumers' tests: under the consumers
    assert e[PDF]["cross_repository"] and not e[SHARED]["cross_repository"]
    assert "statement_rows" in e[PDF]["why"] and "fetch_payments" in e[PDF]["why"] and SHARED in e[PDF]["why"]
    order = [x["entry_point"] for x in rep["test_scope"]["entry_points"]]
    assert order.index(SHARED) == 2                                                 # consumers in other services first
    assert rep["test_scope"]["summary"] == {"entry_points": 3, "by_change_effect": {"behaviour_change": 3}, "code_without_entry_point": 0,
                                            "consistency_checks": 1, "rule_gaps": 1, "tests_linked": 4}


def test_consumers_of_the_same_changed_endpoint_get_a_consistency_check(reps):
    [c] = reps["behaviour"]["test_scope"]["consistency_checks"]
    assert c["shared_entry_point"] == SHARED and {x["entry_point"] for x in c["entry_points"]} == {PORTAL, PDF}
    assert "same records" in c["check"]


def test_a_rule_one_consumer_applies_and_the_other_does_not_is_a_rule_gap(reps):
    [gp] = reps["behaviour"]["test_scope"]["rule_gaps"]
    assert gp["shared_entry_point"] == SHARED and gp["rule"] == {"fields": ["disputed"], "expr": "not p['disputed']"}
    [a] = gp["applied_by"]
    assert a["entry_point"] == PDF and a["function"] == "statement_rows" and a["function_file"] == "app.py" and a["label"] == "Monthly statement PDF"
    assert [m["entry_point"] for m in gp["missing_in"]] == [PORTAL]
    assert gp["resolved_by_this_change"] is False and gp["evidence_status"] == "source_confirmed"


def test_moving_the_rule_into_the_shared_endpoint_is_recognised(reps):
    rep = reps["move_rule"]
    assert {c["detail"].split(":")[0]: c["effect"] for c in rep["change"]["changed_components"]} == {
        "list_payments": "behaviour_change", "exclude_disputed": "compatible_change"}
    [gp] = rep["test_scope"]["rule_gaps"]
    assert gp["resolved_by_this_change"] is True and "apply the rule itself" in gp["reason"]
    assert set(eps(rep)) == {PORTAL, PDF, SHARED}                          # both consumers change behaviour: validate both


def test_after_the_rule_moved_there_is_no_gap(lanes):
    ex = {"payments-service": _extract("payments-service", lanes["paths"]["payments-service"], lanes["shas"]["move_rule"], None, ())}
    ex.update({n: _extract(n, p, "HEAD", None, ()) for n, p in lanes["paths"].items() if n != "payments-service"})
    assert rule_gaps(build_graph(ex), ex) == []                             # the statement still filters too, but the provider applies it for all


def test_signature_changes(reps):
    fixed = next(c for c in reps["signature"]["change"]["changed_components"] if c["detail"].startswith("ordered"))
    assert fixed["effect"] == "behaviour_change" and "updated in the same change" in fixed["detail"]
    broken = next(c for c in reps["signature_unfixed"]["change"]["changed_components"] if c["detail"].startswith("ordered"))
    assert broken["effect"] == "breaking" and "1 caller(s) were not changed (list_payments)" in broken["detail"]
    assert all(e["change_effect"] == "breaking" for e in reps["signature_unfixed"]["test_scope"]["entry_points"])
    cross = [f for f in reps["signature_unfixed"]["findings"] if f["repository"] != "payments-service"]
    assert cross and all(f["change_nature"] == "breaking" and f["material"] for f in cross)
    assert all(f["change_nature"] == "nonbreaking" for f in reps["signature"]["findings"])


def test_a_documentation_only_change_needs_no_retest(reps):
    rep = reps["docstring"]
    [c] = rep["change"]["changed_components"]
    assert c["effect"] == "no_functional_change" and rep["change"]["change_type"] == "no_functional_change"
    assert rep["findings"] == [] and rep["test_scope"]["entry_points"] == [] and "nothing needs retesting" in rep["summary"]["message"]


def test_a_contract_break_is_breaking_for_every_entry_point(reps):
    assert {e["change_effect"] for e in reps["contract"]["test_scope"]["entry_points"]} == {"breaking"}


def test_the_test_scope_is_deterministic(lanes, reps):
    again = analyze(AnalysisRequest(repos=lanes["paths"], target="payments-service", base=lanes["shas"]["base"], head=lanes["shas"]["behaviour"]))
    assert again["test_scope"] == reps["behaviour"]["test_scope"]


# ---- the payments landscape: resolution strategies and consumers without an analysed entry point ----------------------------------
def test_payments_landscape_entry_points_and_code_without_one(run):
    rep = run("removed_endpoint", use_store=False)
    ts = rep["test_scope"]
    assert {(e["entry_point"], e["change_effect"]) for e in ts["entry_points"]} == {
        ("GET /reconciliations/{rid}", "breaking"), ("GET /payments/{payment_id}/status", "breaking")}
    recon = next(e for e in ts["entry_points"] if e["service"] == "reconciliation-service")
    assert [t["name"] for t in recon["tests"]] == ["test_fetch_status"]           # a test of the code between the entry point and the change
    top = {e["service"]: e for e in ts["code_without_entry_point"]}
    assert set(top) == {"mobile-bff", "ledger-sync", "audit-reporting"}
    assert top["ledger-sync"]["evidence_status"] == "inferred" and "no analysed API endpoint" in top["mobile-bff"]["why"]
    assert top["audit-reporting"]["type"] == "contract_ref" and "declares a contract" in top["audit-reporting"]["why"]


# ---- audiences, renderings, Knowledge Fabric ------------------------------------------------------------------------------------
def test_redaction_hides_unreadable_entry_points_checks_and_gaps(reps):
    r = redact(reps["behaviour"], ["payments-service", "statement-service"])
    ts = r["test_scope"]
    assert {e["entry_point"] for e in ts["entry_points"]} == {PDF, SHARED}
    assert ts["consistency_checks"] == [] and ts["rule_gaps"] == [] and ts["redacted"]["items"] == 3
    assert "customer-portal" not in json.dumps(ts) and ts["summary"]["entry_points"] == 2
    only_portal = redact(reps["behaviour"], ["payments-service", "customer-portal"])["test_scope"]
    assert "statement" not in json.dumps(only_portal).lower()


def test_redaction_masks_path_steps_through_unreadable_repositories(run):
    ts = redact(run("breaking", use_store=False), ["payments-service", "reconciliation-service"])["test_scope"]
    assert {e["service"] for e in ts["entry_points"] + ts["code_without_entry_point"]} <= {"payments-service", "reconciliation-service"}


def test_the_comment_starts_with_what_to_test(reps):
    md = render_comment(reps["behaviour"])
    assert md.index("**What to test**") < md.index("| Severity |")
    assert "| Payments screen (`GET /portal/accounts/{account_id}/payments`) | customer-portal | behaviour change | `test_portal_payments` |" in md
    assert "- **Possible rule gap** - " in md and "- **Consistency check** - " in md
    assert "- **Rule moved into the shared service** - " in render_comment(reps["move_rule"])
    assert "What to test" not in render_comment(reps["docstring"])


def test_the_report_page_has_a_what_to_test_section(reps):
    page = render_page(reps["behaviour"], "/api/v1/impact-analyses/x")
    assert page.index("<h2>What to test</h2>") < page.index("<h2>Findings</h2>")
    assert "Possible rule gap" in page and "statement_rows" in page and "Monthly statement PDF" in page
    assert "<b>behaviour change</b> ordered: logic changed" in page
    bad = json.loads(json.dumps(reps["behaviour"]))
    bad["test_scope"]["entry_points"][0]["why"] = "<script>alert(1)</script>"
    assert "<script>alert" not in render_page(bad)


def test_knowledge_fabric_gets_the_tester_view(reps):
    out = kfadapter.to_query_response(reps["behaviour"], kfadapter.QueryRequest(analysis_id="x")).model_dump()
    cats = [c["category"] for c in out["reranked_chunks"]]
    assert {"test_scope", "rule_gap", "consistency_check"} <= set(cats)
    assert cats.index("rule_gap") < cats.index("test_scope") < cats.index("consistency_check")
    tests = [c for c in out["chunks"] if c["block_type"] == "tia_test_target"]
    assert len(tests) == 3 and any(c["text"].startswith("TEST Payments screen: GET /portal/accounts/") for c in tests)
    f = json.loads(next(c for c in tests if "statement" in c["text"])["extracted_fields"])
    assert f["change_effect"] == "behaviour_change" and f["label"] == "Monthly statement PDF" and f["tests"] == 1
    gap = json.loads(next(c for c in out["chunks"] if c["category"] == "rule_gap")["extracted_fields"])
    assert gap["missing_in"] == [PORTAL] and gap["rule_fields"] == ["disputed"]
    assert any(p["source_block_id"].endswith("#test-0") for p in out["paths"])
    small = kfadapter.to_query_response(reps["behaviour"], kfadapter.QueryRequest(analysis_id="x", top_k_semantic=1, top_k_graph=1)).model_dump()
    assert len(small["reranked_paths"]) <= 1 and sum(1 for c in small["reranked_chunks"] if c["block_type"].startswith("tia_") and
                                                     c["category"] in ("test_scope", "rule_gap", "consistency_check")) == 1


# ---- common components and rule gaps across the landscape, before any change ------------------------------------------------------
def test_shared_components_api(lanes, tmp_path):
    c = TestClient(make_app(tmp_path / "ws", allow_local=True))
    assert c.get("/api/v1/graph/shared-components").json()["shared_components"] == []
    for n, p in lanes["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    body = c.get("/api/v1/graph/shared-components").json()
    rows = {r["name"]: r for r in body["shared_components"]}
    # the provider's handler and helpers serve its own endpoint and both consumers; the endpoint itself serves the two consumers
    assert [(r["name"], r["entry_points"]) for r in body["shared_components"]][:3] == [("list_payments", 3), ("load_payments", 3), ("ordered", 3)]
    assert rows[SHARED]["entry_points"] == 2 and rows[SHARED]["services"] == 2
    assert {u["entry_point"] for u in rows[SHARED]["used_by"]} == {PORTAL, PDF}
    assert "statement_rows" not in rows                                                  # used by one entry point only
    assert [g["shared_entry_point"] for g in body["rule_gaps"]] == [SHARED] and body["withheld"] == 0
    limited = c.get("/api/v1/graph/shared-components", headers={"X-TIA-Readable-Repos": "payments-service,statement-service"}).json()
    lrows = {r["name"]: r for r in limited["shared_components"]}
    assert limited["rule_gaps"] == [] and lrows[SHARED]["used_by_withheld"] == 1 and limited["withheld"] >= 1
    assert "customer-portal" not in json.dumps(limited)


def test_shared_components_cli(lanes):
    args = [sys.executable, "-m", "tia.cli", "shared-components"] + [a for n, p in lanes["paths"].items() for a in ("--repo", f"{n}={p}")]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    body = json.loads(out.stdout)
    assert SHARED in {r["name"] for r in body["shared_components"]} and len(body["rule_gaps"]) == 1


# ---- noise guards: a gap is claimed only on a declared response schema's fields, never on unrelated conditions ---------------------
def _landscape(payments=PAYMENTS, statement=STATEMENT_APP, portal=PORTAL_APP):
    cfg = lambda s: f"service: {s}\nconfig_keys:\n  PAYMENTS_BASE_URL: payments-service\n"
    ex = {"payments-service": extract_repo("payments-service", "c1", {"app.py": payments, "tia-services.yaml": "service: payments-service\n"}),
          "statement-service": extract_repo("statement-service", "c2", {"app.py": statement, "client.py": CLIENT, "tia-services.yaml": cfg("statement-service")}),
          "customer-portal": extract_repo("customer-portal", "c3", {"app.py": portal, "client.py": CLIENT, "tia-services.yaml": cfg("customer-portal")})}
    return build_graph(ex), ex


def test_rule_gap_guards():
    g, ex = _landscape()
    assert len(rule_gaps(g, ex)) == 1
    g, ex = _landscape(payments=PAYMENTS.replace(", response_model=List[Payment]", ""))
    assert rule_gaps(g, ex) == []                                                       # no declared response: the rule's data link is unknown
    g, ex = _landscape(statement=STATEMENT_APP.replace('not p["disputed"]', 'p["region"] == "EU"'))
    assert rule_gaps(g, ex) == []                                                       # not a field of the shared response
    g, ex = _landscape(portal=PORTAL_APP.replace("for p in payments]", 'for p in payments if not p.get("disputed")]'))
    assert rule_gaps(g, ex) == []                                                       # both consumers apply it
    assert SHARED in {r["name"] for r in shared_components(*_landscape())}
