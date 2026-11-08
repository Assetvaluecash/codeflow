"""Release 2 contracts: full REST comparison with configurable compatibility rules and OpenAPI ingestion (PRD FR-018 full), event and
message contracts with producer/consumer discovery (FR-019, FR-020 events, AC-028)."""
import json

import pytest

from fixtures.build_events_fixture import build as build_events
from fixtures.build_fixture import _commit, _run, _write
from tia.contracts import diff_contracts
from tia.extract_python import extract_repo
from tia.impact import AnalysisRequest, analyze
from tia.tenantconfig import ConfigError, parse


def py(files):
    return extract_repo("svc", "c", {"tia-services.yaml": "service: svc\n", **files})


BASE = '''from enum import Enum
from typing import List, Optional
from fastapi import FastAPI, Depends, Query
from pydantic import BaseModel, Field
app = FastAPI()
class Kind(str, Enum):
    A = "A"
    B = "B"
class Req(BaseModel):
    name: str = Field(..., max_length=20)
    kind: Kind
class Res(BaseModel):
    state: Kind
@app.post("/items", response_model=Res, status_code=201)
def make(req: Req, page: Optional[int] = Query(None)):
    return 1
@app.get("/items", response_model=List[Res])
def all_items():
    return []
'''


def changes(head_src, base_src=BASE, rules=None):
    return {c["kind"]: c for c in diff_contracts(py({"app.py": base_src}), py({"app.py": head_src}), rules)}


# ---- REST: full comparison ---------------------------------------------------------------------------------------------------------
def test_enum_narrowing_and_widening():
    c = changes(BASE.replace('    B = "B"\n', ""))                                  # Kind loses B: request narrowed
    assert c["request_enum_value_removed"]["breaking"] and "no longer accepts B" in c["request_enum_value_removed"]["detail"]
    c = changes(BASE.replace('    B = "B"\n', '    B = "B"\n    C = "C"\n'))           # Kind gains C: the response may now carry it
    assert c["response_enum_value_added"]["breaking"] and "may now be C" in c["response_enum_value_added"]["detail"]


def test_tightened_constraints_and_required_parameters():
    c = changes(BASE.replace("max_length=20", "max_length=10"))
    assert c["request_constraint_tightened"]["detail"] == "Req.name: max length 20 -> 10"
    c = changes(BASE.replace("page: Optional[int] = Query(None)", "page: int = Query(...)"))
    assert c["request_param_required"]["detail"] == "POST /items: query parameter 'page' is now required"


def test_shape_status_and_authorisation_changes():
    c = changes(BASE.replace("response_model=List[Res])", "response_model=Res)"))
    assert c["response_shape_changed"]["detail"] == "GET /items: response list -> object"
    c = changes(BASE.replace("status_code=201", "status_code=200"))
    assert c["success_status_changed"]["detail"] == "POST /items: success status 201 -> 200"
    c = changes(BASE.replace("def all_items():", "def all_items(user=Depends(get_current_user)):"))
    assert c["auth_added"]["breaking"] and "none -> Depends(get_current_user)" in c["auth_added"]["detail"]
    c = changes(BASE.replace("def all_items(user=Depends(get_current_user)):", "def all_items():"),
                BASE.replace("def all_items():", "def all_items(user=Depends(get_current_user)):"))
    assert not c["auth_removed"]["breaking"] and c["auth_removed"]["security_review"]


def test_compatibility_rules_are_configurable():
    head = BASE.replace('    B = "B"\n', '    B = "B"\n    C = "C"\n')
    assert not changes(head, rules={"response_enum_value_added": "compatible"})["response_enum_value_added"]["breaking"]
    cfg = parse({"compatibility": {"rules": {"response_enum_value_added": "compatible", "event_field_added": "breaking"}}})
    assert cfg.compatibility == {"event_field_added": "breaking", "response_enum_value_added": "compatible"}
    with pytest.raises(ConfigError) as e:
        parse({"compatibility": {"rules": {"response_enum_widen": "compatible", "auth_added": "maybe"}}})
    assert any("did you mean 'response_enum_value_added'" in p for p in e.value.problems) and any("breaking or compatible" in p for p in e.value.problems)


# ---- OpenAPI: a provider declared only by its specification ----------------------------------------------------------------------------
SPEC = {"openapi": "3.0.3", "paths": {"/rates/{currency}": {"get": {
    "operationId": "getRate", "parameters": [{"name": "currency", "in": "path", "required": True, "schema": {"type": "string", "enum": ["EUR", "USD"]}},
                                              {"name": "date", "in": "query", "schema": {"type": "string", "maxLength": 10}}],
    "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Rate"}}}}},
    "security": [{"oauth": ["rates:read"]}]}}},
    "components": {"schemas": {"Rate": {"type": "object", "required": ["value"], "properties": {"value": {"type": "number", "minimum": 0},
                                                                                               "source": {"type": "string", "enum": ["ECB", "FED"]}}}}}}


def test_openapi_specification_is_extracted_like_code():
    ex = extract_repo("rates", "c", {"openapi.yaml": json.dumps(SPEC), "tia-services.yaml": "service: rates\n"})
    [e] = ex.endpoints
    assert (e.method, e.path, e.source, e.success_status, e.response_schema, e.response_shape) == ("GET", "/rates/{currency}", "openapi", 200, "Rate", "object")
    assert e.auth == ["security:oauth[rates:read]"] and {p["name"]: p["in"] for p in e.params} == {"currency": "path", "date": "query"}
    rate = next(s for s in ex.schemas if s.name == "Rate")
    assert rate.fields["value"] == {"type": "number", "required": True, "constraints": {"minimum": 0}}
    assert rate.fields["source"]["constraints"] == {"allowed": ["ECB", "FED"]}


def test_code_wins_over_a_stale_specification():
    code = 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/rates/{currency}")\ndef rate(currency: str):\n    return 1\n'
    ex = extract_repo("rates", "c", {"app.py": code, "openapi.yaml": json.dumps(SPEC), "tia-services.yaml": "service: rates\n"})
    assert [(e.source, e.handler) for e in ex.endpoints] == [("code", "rate")]


def test_a_specification_change_reaches_a_consumer(tmp_path):
    def repo(name, files):
        p = tmp_path / name
        p.mkdir()
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        return str(p), _commit(p, "init")
    rates, base = repo("rates", {"openapi.yaml": json.dumps(SPEC), "tia-services.yaml": "service: rates\n"})
    client, _ = repo("pricing", {"tia-services.yaml": "service: pricing\nconfig_keys:\n  RATES_URL: rates\n",
                                 "pricing.py": 'import os\nimport requests\nRATES_URL = os.getenv("RATES_URL")\n\n\ndef rate(c):\n'
                                               '    return requests.get(f"{RATES_URL}/rates/{c}").json()\n'})
    spec2 = json.loads(json.dumps(SPEC))
    spec2["components"]["schemas"]["Rate"]["properties"]["source"]["enum"].append("BOE")
    _run(tmp_path / "rates", "checkout", "-q", "-b", "widen")
    _write(tmp_path / "rates", {"openapi.yaml": json.dumps(spec2)})
    head = _commit(tmp_path / "rates", "widen")
    rep = analyze(AnalysisRequest(repos={"rates": rates, "pricing": client}, target="rates", base=base, head=head))
    [c] = [x for x in rep["change"]["changed_components"] if x["type"].startswith("api")]
    assert c["change"] == "response_enum_value_added" and c["breaking"] and c["rule"] == "response_enum_value_added"
    f = next(f for f in rep["findings"] if f["component"] == "pricing")
    assert f["change_nature"] == "breaking" and f["impact_type"] == "contract"


# ---- events --------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ev(tmp_path_factory):
    fx = build_events(str(tmp_path_factory.mktemp("events")))
    run = lambda b: analyze(AnalysisRequest(repos=fx["paths"], target="orders-service", base=fx["shas"]["base"], head=fx["shas"][b]))
    return fx, {b: run(b) for b in ("field_removed", "field_added", "enum_added", "stopped")}


def consumers(rep):
    return {f["component"]: f for f in rep["findings"] if f["impact_type"] == "event"}


def test_producers_consumers_and_schemas_are_extracted(ev):
    fx, _ = ev
    o = extract_repo("orders-service", "c", {p.relative_to(fx["paths"]["orders-service"]).as_posix(): p.read_text(encoding="utf-8")
                                             for p in __import__("pathlib").Path(fx["paths"]["orders-service"]).rglob("*") if p.is_file() and ".git" not in p.parts})
    kinds = {(e["kind"], e["topic"], e.get("declared", False)) for e in o.events}
    assert kinds == {("produces", "orders.created", False), ("produces", "orders.created", True)}
    [s] = o.event_schemas
    assert s["format"] == "avro" and s["topic"] == "orders.created" and s["topic_source"] == "declared"
    assert s["fields"]["note"] == {"type": "string", "required": False, "has_default": True}
    assert s["fields"]["status"]["allowed"] == ["NEW", "PAID"]


def test_a_removed_event_field_reaches_every_known_consumer(ev):
    fx, reps = ev
    rep = reps["field_removed"]
    [c] = [x for x in rep["change"]["changed_components"] if x["type"] == "event_schema"]
    assert c["change"] == "event_field_removed" and c["breaking"] and "field amount removed" in c["detail"]
    cons = consumers(rep)
    assert set(cons) == {"billing-service", "shipping-service", "audit-service"}
    assert cons["billing-service"]["evidence_status"] == "contract_confirmed" and cons["billing-service"]["resolution_strategy"] == "E1"
    assert "consumes topic orders.created at billing-service:src/main/java/com/acme/billing/OrderListener.java" in cons["billing-service"]["reason"]
    assert cons["audit-service"]["resolution_strategy"] == "E2" and "declares in tia-services.yaml" in cons["audit-service"]["reason"]
    assert all(f["change_nature"] == "breaking" and f["material"] for f in cons.values())
    assert rep["change"]["change_type"] == "event_change"
    ship = cons["shipping-service"]
    assert ship["path"][-1]["type"] == "listener" and [e["rel"] for e in ship["path_edges"]] == ["USES_SCHEMA", "CONSUMES_EVENT"]


def test_compatible_and_widening_event_changes(ev):
    fx, reps = ev
    added = reps["field_added"]
    assert [c["change"] for c in added["change"]["changed_components"] if c["type"] == "event_schema"] == ["event_field_added"]
    assert all(f["change_nature"] == "nonbreaking" for f in consumers(added).values())
    enum = reps["enum_added"]
    assert any(c["change"] == "event_enum_value_added" and c["breaking"] for c in enum["change"]["changed_components"])


def test_a_topic_no_longer_produced(ev):
    fx, reps = ev
    rep = reps["stopped"]
    assert any(c["change"] == "event_topic_no_longer_produced" and c["breaking"] for c in rep["change"]["changed_components"])
    assert set(consumers(rep)) == {"billing-service", "shipping-service", "audit-service"}


def test_test_scope_and_unknowns_for_events(ev):
    fx, reps = ev
    rep = reps["field_removed"]
    top = {e["service"] for e in rep["test_scope"]["code_without_entry_point"]}
    assert {"billing-service", "shipping-service"} <= top
    ship = next(e for e in rep["test_scope"]["code_without_entry_point"] if e["service"] == "shipping-service")
    assert ship["entry_point"] == "poll" and ship["tests"] == []          # test_ship tests ship(), which poll() calls: not on the impact path
    assert any(g.get("entry_point") is None and g["component"] == "shipping-service" for g in rep["test_gaps"])
    assert not any(u["area"] == "event_consumers_unknown" for u in rep["unknowns"])
    lone = analyze(AnalysisRequest(repos={"orders-service": fx["paths"]["orders-service"]}, target="orders-service",
                                   base=fx["shas"]["base"], head=fx["shas"]["field_removed"]))
    u = next(u for u in lone["unknowns"] if u["area"] == "event_consumers_unknown")
    assert u["items"] == [{"topic": "orders.created"}] and u["affects_confidence"]
