"""Public API and SDK (PRD PL-008): the OpenAPI document is generated and current, every route carries its permission, and the Python client
drives the real application end to end."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fixtures.build_polyglot_fixture import build as build_poly

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "tools"))
import gen_docs  # noqa: E402
from tia import __version__, rbac  # noqa: E402
from tia_client import TiaClient, TiaError  # noqa: E402


def test_the_openapi_document_is_current_and_carries_permissions():
    text = (ROOT / "docs" / "openapi.json").read_text(encoding="utf-8")
    assert text == gen_docs.render_openapi(), "docs/openapi.json is out of date: run  python tools/gen_docs.py"
    oa = json.loads(text)
    assert oa["info"]["version"] == __version__ and oa["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    for path, ops in oa["paths"].items():
        for method, op in ops.items():
            assert op["x-tia-permission"] == rbac.classify(method.upper(), path), (method, path)
    assert "/api/v1/releases/compare" in oa["paths"] and "/api/v1/incidents/{iid}/investigation" in oa["paths"]


@pytest.fixture(scope="module")
def sdk(tmp_path_factory):
    poly = build_poly(str(tmp_path_factory.mktemp("sdk")))
    tc = TestClient(make_app(tmp_path_factory.mktemp("ws"), allow_local=True))

    def transport(method, url, headers, body, timeout):
        r = tc.request(method, url.replace("http://testserver", ""), headers=headers, content=body)
        return r.status_code, r.content
    return TiaClient("http://testserver", transport=transport), poly


def test_the_client_drives_an_analysis_and_the_release_and_incident_views(sdk):
    tia, poly = sdk
    for n, p in poly["paths"].items():
        tia.register_repository(path=p, name=n)
    assert sorted(r["name"] for r in tia.repositories()) == sorted(poly["paths"])
    rep = tia.analyze(target="pricing-service", head="field_removed")
    assert {f["component"] for f in rep["findings"]} == {"orders-service", "billing-service"}
    assert tia.test_scope(rep["analysis_id"])["entry_points"][0]["entry_point"] == "POST /orders"
    assert tia.policy(rep["analysis_id"])["mode"] == "advisory"
    job = tia.queue_analysis(target="pricing-service", head="impl_only")
    assert job["job_id"] and tia.job(job["job_id"])["status"] in ("queued", "running", "succeeded")
    rel = tia.record_release("r1", {n: "main" for n in poly["paths"]})
    assert rel["name"] == "r1" and tia.compare_releases("r1", "r1")["summary"] == {"introduced": 0, "removed": 0, "modified": 0}
    cons = tia.consumers_at_release("r1", "GET /prices/{}")
    assert sorted(c["service"] for c in cons["consumers"]) == ["billing-service", "orders-service"]
    tia.record_incident({"id": "INC-1", "service": "orders-service", "started_at": "2026-10-20T12:00:00Z"})
    assert tia.investigate_incident("INC-1")["root_cause"] is None
    assert tia.verify_audit()["ok"]


def test_errors_carry_status_and_message(sdk):
    tia, _ = sdk
    with pytest.raises(TiaError) as e:
        tia.report("no-such-analysis")
    assert e.value.status == 404
    with pytest.raises(TiaError) as e:
        tia.record_release("bad name!", {"orders-service": "main"})
    assert e.value.status == 400 and "name must be" in e.value.message


def test_the_client_refuses_to_send_a_token_over_plain_http():
    with pytest.raises(ValueError, match="plain http"):
        TiaClient("http://tia.bank.example", token="t")
    TiaClient("https://tia.bank.example", token="t")
    TiaClient("http://localhost:8000", token="t")
