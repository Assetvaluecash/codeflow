"""UC-3 automation inventory and UC-2 technical coverage (docs/CASE-STUDY.md) on the tester landscape (fixtures/build_lanes_fixture.py):
API inputs and their rules, generated valid and invalid values, test matrices, pages with locators, written test cases, and which
technical aspects the tests address."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_lanes_fixture import build as build_lanes
from tia.automation import api_inventory, rules_of, sample_for_pattern, ui_inventory, values, violates
from tia.coverage import assess_test_case, coverage_report
from tia.extract_java import extract_file as java_file
from tia.extract_python import extract_file as py_file, extract_repo, extract_source
from tia.extract_ui import extract_ui_file, scan, template_elements
from tia.graph import build_graph
from tia.impact import _extract
from tia.testcases import parse_testcases
from tia.testscope import labels_of

ROOT = Path(__file__).resolve().parent.parent
POST = "POST /accounts/{account_id}/payments"


@pytest.fixture(scope="module")
def lanes(tmp_path_factory):
    return build_lanes(str(tmp_path_factory.mktemp("tester")))


@pytest.fixture(scope="module")
def ex(lanes):
    return {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(lanes["paths"].items())}


@pytest.fixture(scope="module")
def api(ex):
    return {i["entry_point"]: i for i in api_inventory(ex, labels_of(ex))}


@pytest.fixture(scope="module")
def pages(ex):
    return {p["page"]: p for p in ui_inventory(ex)}


@pytest.fixture(scope="module")
def cov(ex):
    rep = coverage_report(ex, build_graph(ex))
    return rep, {(e["entry_point"], a["id"].split("#", 1)[1]): a for e in rep["entry_points"] for a in e["aspects"]}


def inputs(item):
    return {i["name"]: i for i in item["inputs"]}


# ---- UC-3: API inputs and rules --------------------------------------------------------------------------------------------------
def test_python_fields_parameters_and_enums():
    src = """from enum import Enum
from typing import Annotated, List, Literal, Optional
from fastapi import FastAPI, Header, Path, Query
from pydantic import BaseModel, EmailStr, Field, constr
app = FastAPI()
class Kind(str, Enum):
    A = "A"
    B = "B"
class Req(BaseModel):
    name: str = Field(..., min_length=2, max_length=5)
    mode: Literal["fast", "slow"]
    note: Optional[str] = None
    tags: List[str] = []
    code: constr(pattern=r"^[0-9]{3}$")
    mail: EmailStr
    size: Annotated[int, Field(ge=1, le=9)] = 3
    kind: Kind
@app.post("/x/{xid}")
def h(body: Req, xid: int = Path(..., ge=1), q: Optional[str] = Query(None, max_length=4), token: str = Header(...)):
    return 1
"""
    part = py_file("r", "svc", "app.py", src)
    f = next(s for s in part.schemas if s.name == "Req").fields
    assert f["name"] == {"type": "str", "required": True, "constraints": {"min_length": 2, "max_length": 5}}
    assert f["mode"]["constraints"] == {"allowed": ["fast", "slow"]} and f["mode"]["required"]
    assert f["note"] == {"type": "Optional[str]", "required": False, "nullable": True}
    assert f["tags"]["multi"] and not f["tags"]["required"]
    assert f["code"]["constraints"] == {"pattern": "^[0-9]{3}$"} and f["mail"]["constraints"] == {"format": "email"}
    assert f["size"] == {"type": "Annotated[int, Field(ge=1, le=9)]", "required": False, "constraints": {"minimum": 1, "maximum": 9}, "default": 3}
    assert part.enums == {"Kind": ["A", "B"]}
    [ep] = part.endpoints
    ps = {p["name"]: p for p in ep.params}
    assert ps["xid"]["in"] == "path" and ps["xid"]["required"] and ps["xid"]["constraints"] == {"minimum": 1}
    assert ps["q"] == {"name": "q", "in": "query", "type": "Optional[str]", "required": False, "nullable": True, "constraints": {"max_length": 4}}
    assert ps["token"]["in"] == "header" and ps["token"]["required"]
    assert "body" in ps                                          # the model parameter is dropped at assembly, when models are known
    full = extract_repo("r", "c", {"app.py": src})
    assert [p["name"] for p in full.endpoints[0].params] == ["xid", "q", "token"]


def test_python_statuses_and_test_facts():
    src = """from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
def h(x):
    if not x:
        raise HTTPException(status_code=404, detail="no such thing")
    if x == 2:
        raise HTTPException(status.HTTP_409_CONFLICT)
    return JSONResponse(status_code=422, content={})
"""
    f = py_file("r", "s", "m.py", src).functions[0]
    assert [(s["status"], s.get("detail")) for s in f.statuses] == [(404, "no such thing"), (409, None), (422, None)]
    t = py_file("r", "s", "tests/test_m.py", """def test_x(client):
    r = client.post("/a/1", json={"amount": 0, "currency": "EUR", "ref": some_var})
    assert r.status_code in (400, 422)
""").tests[0]
    assert t.statuses == [400, 422] and t.bodies == [["POST", "/a/1", ["amount", "currency", "ref"], {"amount": 0, "currency": "EUR"}]]
    assert {"/a/1", "0", "EUR"} <= set(t.literals)


def test_java_bean_validation_enums_parameters_and_statuses():
    dto = """package com.acme;
public class PaymentRequest {
  @NotNull @DecimalMin("0.01") @Max(10000) private BigDecimal amount;
  @NotBlank @Size(max = 18) @Pattern(regexp = "^[A-Z0-9]+$") private String reference;
  @Email private String mail;
  @Size(min = 1, max = 3) private List<String> tags;
  private Currency currency;
  public enum Currency { EUR, USD }
}"""
    part = java_file("r", "svc", "src/main/java/com/acme/PaymentRequest.java", dto)
    f = next(s for s in part.schemas if s.name == "PaymentRequest").fields
    assert f["amount"]["required"] and f["amount"]["constraints"] == {"minimum": 0.01, "maximum": 10000}
    assert f["reference"]["constraints"] == {"max_length": 18, "pattern": "^[A-Z0-9]+$", "min_length": 1}
    assert f["mail"]["constraints"] == {"format": "email"} and f["tags"]["multi"] and f["tags"]["constraints"] == {"min_items": 1, "max_items": 3}
    assert part.enums == {"Currency": ["EUR", "USD"]}
    ctl = """package com.acme;
@RestController
public class C {
  @GetMapping("/accounts/{id}/payments")
  public List<Payment> list(@PathVariable("id") String id, @RequestParam(required = false) String status,
                            @RequestParam(defaultValue = "10") @Max(50) int size) {
    if (id.isEmpty()) { throw new ResponseStatusException(HttpStatus.NOT_FOUND, "unknown"); }
    if (size == 0) { return ResponseEntity.badRequest().build(); }
    return List.of();
  }
}"""
    part = java_file("r", "svc", "src/main/java/com/acme/C.java", ctl)
    ps = {p["name"]: p for p in part.endpoints[0].params}
    assert ps["id"]["in"] == "path" and ps["id"]["required"]
    assert ps["status"]["in"] == "query" and not ps["status"]["required"]
    assert not ps["size"]["required"] and ps["size"]["default"] == "10" and ps["size"]["constraints"] == {"maximum": 50}
    assert [s["status"] for s in part.functions[0].statuses] == [404, 400]
    test = """package com.acme;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
class CTest {
  @Test void unknown() throws Exception {
    mvc.perform(get("/accounts/x/payments")).andExpect(status().isNotFound());
  }
}"""
    t = java_file("r", "svc", "src/test/java/com/acme/CTest.java", test).tests[0]
    assert t.statuses == [404] and "/accounts/x/payments" in t.literals


@pytest.mark.parametrize("pattern, expected", [("^[0-9]{1,9}$", "0"), (r"^\d{4}$", "0000"), ("^[A-Z]{2}[0-9]{2}$", "AA00"),
                                               ("^(EUR|USD)$", "EUR"), ("^[A-Z0-9]+$", "A"), (r"^INV-\d+$", "INV-0")])
def test_values_from_patterns(pattern, expected):
    assert sample_for_pattern(pattern) == expected


def test_a_pattern_beyond_the_generator_is_stated_not_guessed():
    assert sample_for_pattern(r"^(?=.*\d)[a-z]{8,}$") is None
    valid, _ = values(rules_of({"type": "str", "required": True, "constraints": {"pattern": r"^(?=.*\d)[a-z]{8,}$"}}, {}), "pw")
    assert valid == [{"value": None, "why": r"a value matching ^(?=.*\d)[a-z]{8,}$ (TIA cannot generate one)"}]


def test_value_rules():
    r = rules_of({"type": "int", "required": True, "constraints": {"minimum": 1, "maximum": 9}}, {})
    valid, invalid = values(r, "n")
    assert [v["value"] for v in valid] == [1, 9] and [v["value"] for v in invalid] == ["<missing>", 0, 10, 1.5, "abc"]
    assert violates(r, 10) == "maximum 9" and violates(r, 5) is None and violates(r, "x") == "not a number"
    s = rules_of({"type": "str", "constraints": {"min_length": 2, "max_length": 3}}, {})
    assert violates(s, "a") == "shorter than 2" and violates(s, "abcd") == "longer than 3" and violates(s, "ab") is None
    e = rules_of({"type": "Status"}, {"Status": ["A", "B"]})
    assert e["kind"] == "enum" and violates(e, "C") == "not an allowed value"


def test_api_inventory_of_the_payment_endpoint(api):
    item = api[POST]
    i = inputs(item)
    assert set(i) == {"account_id", "amount", "currency", "reference", "express", "tags"}
    assert i["account_id"]["in"] == "path" and i["account_id"]["rules"] == "matches ^[0-9]{1,9}$"
    assert i["amount"]["required"] and i["amount"]["rules"] == "<= 10000; > 0"
    assert [v["value"] for v in i["amount"]["invalid"]] == ["<missing>", 0.0, 10000.01, "abc"]
    assert i["currency"]["kind"] == "enum" and i["currency"]["allowed"] == ["EUR", "USD", "GBP"]
    assert [v["value"] for v in i["reference"]["valid"]] == ["A", "A" * 18]
    assert [v["value"] for v in i["reference"]["invalid"]] == ["<missing>", "!@#", "", "A" * 19]
    assert not i["express"]["required"] and i["tags"]["multi"]
    assert [s["status"] for s in item["error_statuses"]] == [404, 409]
    c1 = item["cases"][0]
    assert c1["kind"] == "valid baseline" and c1["expect"] == "accepted"
    assert c1["input"] == {"path": {"account_id": "0"}, "body": {"amount": 0.01, "currency": "EUR", "reference": "A", "express": True, "tags": ["sample text"]}}
    too_big = next(c for c in item["cases"] if c.get("field") == "amount" and c["input"]["body"].get("amount") == 10000.01)
    assert too_big["expect"] == "rejected (422)" and too_big["why"] == "above the maximum 10000"
    missing = next(c for c in item["cases"] if c.get("field") == "reference" and "reference" not in c["input"]["body"])
    assert missing["expect"] == "rejected (422)"
    assert item["case_count"] == len(item["cases"]) == 19 and item["valid_combinations"] == 24
    assert {f["name"] for f in api["GET /accounts/{account_id}/payments"]["response_fields"]} == {"payment_id", "status", "amount", "disputed"}
    assert inputs(api["GET /accounts/{account_id}/payments"])["status"]["allowed"] == ["SETTLED", "PENDING"]


# ---- UC-3: pages, elements, locators -------------------------------------------------------------------------------------------
def test_angular_page(pages):
    p = pages["/accounts/:id/payments"]
    assert p["framework"] == "angular" and p["component"] == "PaymentsComponent"
    els = {(e["tag"], e.get("label") or e.get("text") or e.get("name")): e for e in p["elements"]}
    amount = els[("input", "Amount")]
    assert amount["locator"]["css"] == "#amount" and amount["locator"]["unique"] and amount["mandatory"]
    assert amount["constraints"] == {"minimum": 0.01, "maximum": 10000} and amount["valid"][0]["value"] == 0.01
    cur = els[("select", "Currency")]
    assert [o["value"] for o in cur["options"]] == ["EUR", "USD", "GBP"] and "literal list 'currencies'" in cur["options_source"]
    ref = next(e for e in p["elements"] if e.get("name") == "reference")
    assert ref["mandatory"] and ref["mandatory_source"].startswith("form validator")
    assert ref["constraints"] == {"pattern": "^[A-Z0-9]+$", "max_length": 18}                     # from the validators and the template
    assert ref["locator"]["css"] == '[formcontrolname="reference"]'
    assert els[("button", "Pay")]["locator"]["playwright"] == "getByTestId('pay-submit')"
    assert els[("button", "Refresh")]["locator"]["xpath"] == "//button[normalize-space()='Refresh']"
    exports = [e for e in p["elements"] if e.get("text") == "Export"]
    assert len(exports) == 2 and all(e["locator"]["strategy"] == "position (fragile)" for e in exports)
    assert p["summary"]["without_unique_locator"] == 2
    details = next(e for e in p["elements"] if e.get("text") == "Details")
    assert details["repeated"] and "within its row" in details["locator_note"]
    status = els[("select", "Status")]
    assert status["rules"] == "one of SETTLED, PENDING"                                         # "All" (value "") is the no-filter placeholder
    calls = {(c["method"], c["path"]): [x["entry_point"] for x in c["endpoints"]] for c in p["api_calls"]}
    assert calls == {("GET", "/portal/accounts/{}/payments"): ["GET /portal/accounts/{account_id}/payments"],
                     ("POST", "/portal/accounts/{}/payments"): ["POST /portal/accounts/{account_id}/payments"]}


def test_react_page(pages):
    p = pages["/statements"]
    assert p["framework"] == "react" and p["component"] == "StatementRequest"
    els = {e.get("label") or e.get("text"): e for e in p["elements"]}
    acc = els["Account number"]
    assert acc["mandatory"] and acc["constraints"] == {"max_length": 9, "pattern": "[0-9]{1,9}"} and acc["locator"]["css"] == "#account"
    month = els["Month"]
    assert month["rules"] == "one of 2026-09, 2026-10, 2026-11" and "MONTHS" in month["options_source"]
    assert els["Download PDF"]["locator"]["strategy"] == "test id"
    assert p["api_calls"][0]["endpoints"] == [{"entry_point": "GET /statements/{account_id}/pdf", "service": "statement-service", "repo": "statement-service"}]


def test_scanner_is_not_fooled_by_code():
    jsx = """import { useState } from "react";
// <input id="commented-out">
export default function P() {
  const [v, setV] = useState<string>("");
  const big = a < b && c > d;
  return (<div>{/* <button>no</button> */}
    <input id="a" onChange={e => setV(e.target.value > 3 ? "x" : "y")} title="1 > 0" />
    <button onClick={() => { if (v) { go(); } }}>Go</button>
  </div>);
}"""
    els = template_elements(jsx, script=True)
    assert [(e["tag"], e["attrs"].get("id"), e.get("text")) for e in els] == [("input", "a", ""), ("button", None, "Go")]
    assert [t["open"] for t in scan("<script>if (a<b) {}</script><p>x</p>") if "open" in t] == ["script", "p"]   # no <b> from the script
    dyn = template_elements('<select id="s"><option *ngFor="let x of fromApi$ | async" [value]="x.id">{{x.name}}</option></select>')
    assert dyn[0]["_dynamic_options"] == ["let x of fromApi$ | async"]


def test_a_dropdown_filled_at_run_time_is_reported_as_dynamic():
    facts = extract_ui_file("app/x.component.ts", """@Component({ selector: 'x', template: `<select id="s"><option *ngFor="let c of codes" [value]="c">{{c}}</option></select>` })
export class X { codes$ = this.api.codes(); }""")
    from tia.extract_ui import assemble_pages
    [page] = assemble_pages(facts, "r")
    assert page["elements"][0]["options"] == [] and page["elements"][0]["options_source"].startswith("dynamic: codes")


# ---- written test cases ---------------------------------------------------------------------------------------------------------
def test_written_test_cases_are_read_from_csv_yaml_and_json():
    rows = parse_testcases("testcases/a.csv", "Test Case ID;Summary;Test Steps;Expected Result\nT1;Pay;Open, pay;Done\n")
    assert rows == [{"id": "T1", "title": "Pay", "steps": "Open, pay", "expected": "Done", "entry_point": "", "priority": "", "type": "",
                     "file": "testcases/a.csv", "line": 2}]
    rows = parse_testcases("testcases/b.yaml", "test_cases:\n  - title: X\n    steps: [a, b]\n")
    assert rows[0]["steps"] == "a\nb" and rows[0]["id"] == "testcases/b.yaml#1"
    assert parse_testcases("testcases/c.json", '[{"name": "Y", "endpoint": "GET /x"}]')[0]["entry_point"] == "GET /x"
    bad = extract_source("r", "s", "testcases/d.yaml", "a: [unclosed")
    assert bad.files_failed and bad.files_failed[0]["error"].startswith("test cases: not valid YAML")


def test_secrets_in_test_data_and_written_cases_are_withheld():
    token = "ghp_" + "A1b2C3d4E5" * 3 + "abcdef"                   # built at run time: no credential-shaped string in the repository
    part = extract_source("r", "s", "testcases/x.csv", f"ID,Title,Steps\nT1,Login,Use token {token}\n")
    assert token not in json.dumps(part.manual_tests) and part.secrets_withheld == 1
    part = extract_source("r", "s", "tests/test_x.py", f"def test_x(client):\n    client.post('/a', json={{'t': '{token}'}})\n")
    assert token not in json.dumps(part.tests[0].literals + part.tests[0].bodies)


# ---- UC-2: technical coverage --------------------------------------------------------------------------------------------------
def test_coverage_of_the_payment_endpoint(cov):
    rep, a = cov
    st = lambda aid: a[(POST, aid)]["status"]
    assert st("call") == "covered" and st("missing:reference") == "covered"
    assert a[(POST, "missing:reference")]["covered_by"][0]["why"] == "sends a body without 'reference' and expects a rejection"
    assert st("missing:amount") == "missing" and st("invalid:amount") == "missing"            # a test that leaves out the reference
    assert st("invalid:reference") == "missing" and st("status:409") == "missing"            # does not test an invalid amount
    assert st("values:currency") == "partial" and a[(POST, "values:currency")]["values_missing"] == ["USD", "GBP"]
    assert st("status:404") == "covered" and a[(POST, "status:404")]["covered_by"][0]["id"] == "TC-105"


def test_a_written_case_for_a_page_counts_for_the_page_not_for_an_api_with_the_same_path(cov):
    rep, a = cov
    assert a[("page /accounts/:id/payments", "missing:Amount")]["covered_by"][0]["id"] == "TC-102"
    assert all(x.get("id") not in ("TC-102", "TC-103", "TC-104") for x in a[(POST, "call")]["covered_by"])


def test_a_providers_tests_do_not_exercise_a_consumer(cov):
    rep, a = cov
    by = a[("POST /portal/accounts/{account_id}/payments", "call")]
    assert by["status"] == "missing" and by["covered_by"] == []
    assert {x["name"] for x in a[("GET /portal/accounts/{account_id}/payments", "call")]["covered_by"] if x["type"] == "automated"} == {"test_portal_payments"}


def test_rules_and_pages_and_unmatched_cases(cov):
    rep, a = cov
    rule = next(v for (e, k), v in a.items() if e == "GET /statements/{account_id}/pdf" and k.startswith("rule:"))
    assert rule["status"] == "covered" and rule["covered_by"][0]["id"] == "TC-201"
    assert a[("page /statements", "invalid:Account number")]["covered_by"][0]["id"] == "TC-203"
    assert a[("page /statements", "values:Month")]["values_covered"] == ["2026-10"]
    assert [u["id"] for u in rep["unmatched_written_tests"]] == ["TC-204"]
    s = rep["summary"]
    assert s["entry_points"] == 7 and s["automated_tests"] == 6 and s["written_tests"] == 9 and s["covered"] + s["partial"] + s["missing"] == s["aspects"]
    assert 0 < s["percent"] < 100 and s["by_kind"]["call"]["missing"] == 1


def test_one_test_case_as_a_query(ex):
    out = assess_test_case(ex, build_graph(ex), {"title": "Pay in GBP", "entry_point": POST, "steps": "Send amount 20, currency GBP, reference ABC1",
                                                "expected": "201 created"})
    [m] = out["matched"]
    assert m["entry_point"] == POST and {c["id"].split("#")[1] for c in m["covers"]} == {"call", "values:currency"}
    assert "status:409" in {x["id"].split("#")[1] for x in m["misses"]} and m["percent_of_area"] < 20
    none = assess_test_case(ex, build_graph(ex), {"title": "Something unrelated"})
    assert none["matched"] == [] and "does not name any analysed entry point" in none["note"]


# ---- over HTTP and from the command line -----------------------------------------------------------------------------------------
def test_tester_views_over_http(lanes, tmp_path):
    c = TestClient(make_app(tmp_path / "ws", allow_local=True))
    for n, p in lanes["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    api_ = c.get("/api/v1/automation/api").json()
    assert api_["summary"]["endpoints"] == 5 and api_["summary"]["cases"] > 20
    ui = c.get("/api/v1/automation/ui").json()
    assert ui["summary"] == {"pages": 2, "elements": 14, "without_unique_locator": 2}
    cov = c.get("/api/v1/coverage").json()
    assert cov["summary"]["entry_points"] == 7 and cov["unmatched_written_tests"][0]["id"] == "TC-204"
    q = c.post("/api/v1/coverage/test-case", json={"title": "Unknown account", "entry_point": POST, "steps": "account 999", "expected": "404"}).json()
    assert {x["id"].split("#")[1] for x in q["matched"][0]["covers"]} == {"call", "status:404"}
    only = c.get("/api/v1/coverage", headers={"X-TIA-Readable-Repos": "payments-service"}).json()
    assert {e["repo"] for e in only["entry_points"]} == {"payments-service"} and "TC-1" not in json.dumps(only)
    assert c.get("/api/v1/automation/ui?repos=statement-service").json()["summary"]["pages"] == 1
    assert c.get("/api/v1/coverage?repos=ghost").status_code == 404
    assert c.post("/api/v1/coverage/test-case", json={"title": "x" * 600}).status_code == 422


def test_tester_views_from_the_command_line(lanes, tmp_path):
    repos = [a for n, p in lanes["paths"].items() for a in ("--repo", f"{n}={p}")]
    run = lambda *a: subprocess.run([sys.executable, "-m", "tia.cli", *a], cwd=ROOT, capture_output=True, text=True, timeout=180)
    out = run("automation", *repos)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["summary"]["endpoints"] == 5
    assert len(json.loads(run("automation", "--ui", *repos).stdout)["pages"]) == 2
    assert json.loads(run("coverage", *repos).stdout)["summary"]["unmatched_written_tests"] == 1
    tc = tmp_path / "tc.yaml"
    tc.write_text("title: Statement\nentry_point: GET /statements/{account_id}/pdf\nsteps: disputed payment\n", encoding="utf-8")
    m = json.loads(run("coverage", "--test-case", str(tc), *repos).stdout)["matched"][0]
    assert {c["id"].split("#")[1].split(":")[0] for c in m["covers"]} == {"call", "rule"}
