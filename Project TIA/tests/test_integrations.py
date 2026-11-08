"""Release 2 integrations: ServiceNow change-record attachment (PRD FR-038, AC-031) and the read-only MCP server for coding assistants
(FR-037, AC-032)."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from fakesnow import FakeServiceNow
from fixtures.build_lanes_fixture import build as build_lanes
from tia.itsm import ItsmError, ServiceNow, summary
from tlsutil import make_pki


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    land = build_lanes(str(tmp_path_factory.mktemp("integ")))
    app = make_app(tmp_path_factory.mktemp("ws"), allow_local=True)
    c = TestClient(app)
    for n, p in land["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    rep = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "behaviour", "base": "main",
                                                  "change_record": "CHG0031234"}).json()["report"]
    return c, app, land, rep


@pytest.fixture
def snow(tmp_path, monkeypatch):
    pki = make_pki(tmp_path / "pki")
    monkeypatch.setenv("TIA_IDP_CA_FILE", pki["ca"])
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_ITSM_HOSTS", "1")
    s = FakeServiceNow(pki)
    yield s
    s.close()


def configure(c, app, url):
    assert c.put("/api/v1/tenant/config", json={"config": {"itsm": {"provider": "servicenow", "instance_url": url}}}).status_code == 200


# ---- ServiceNow ---------------------------------------------------------------------------------------------------------------------
def test_the_summary_says_first_when_the_analysis_is_not_complete(world):
    c, app, land, rep = world
    text = summary(rep, "https://tia.example.com/analyses/x")
    assert text.splitlines()[0].startswith("WARNING: this impact analysis is COMPLETE_WITH_GAPS")
    assert rep["analysis_id"] in text and "Revisions:" in text and "Report: https://tia.example.com/analyses/x" in text and "Unknowns:" in text
    assert summary({**rep, "status": "complete"}, None).splitlines()[0].startswith("TIA impact analysis")


def test_servicenow_client_against_the_stand_in(world, snow):
    c, app, land, rep = world
    sn = ServiceNow(snow.url, username="tia", password="secret-pw")
    r = sn.attach("CHG0031234", rep, "https://tia.example.com/analyses/" + rep["analysis_id"])
    assert r["created"] and r["change_record"] == "CHG0031234" and r["sys_id"] == "c0ffee00000000000000000000000001"
    [a] = snow.attachments
    assert a["table_name"] == "change_request" and a["file_name"] == f"tia-impact-{rep['analysis_id']}.txt" and a["content"].startswith("WARNING")
    again = sn.attach("CHG0031234", rep, None)                                     # once per analysis
    assert not again["created"] and len(snow.attachments) == 1
    writes = [q for q in snow.requests if q["method"] != "GET"]
    assert writes == [{"method": "POST", "path": "/api/now/attachment/file", "query": {"table_name": "change_request",
                       "table_sys_id": "c0ffee00000000000000000000000001", "file_name": f"tia-impact-{rep['analysis_id']}.txt"}}]   # AC-031


def test_servicenow_refusals(world, snow):
    c, app, land, rep = world
    with pytest.raises(ItsmError, match="not found"):
        ServiceNow(snow.url, username="tia", password="secret-pw").attach("CHG9999999", rep, None)
    with pytest.raises(ItsmError, match="HTTP 401"):
        ServiceNow(snow.url, username="tia", password="wrong").attach("CHG0031234", rep, None)
    with pytest.raises(ItsmError, match="not a change-record number"):
        ServiceNow(snow.url, username="tia", password="secret-pw").attach("../sys_user", rep, None)
    with pytest.raises(ItsmError, match="https"):
        ServiceNow("ftp://x", token="t")
    with pytest.raises(ItsmError, match="credentials"):
        ServiceNow("https://acme.service-now.com")


def test_attach_over_the_api(world, snow, monkeypatch):
    c, app, land, rep = world
    configure(c, app, snow.url)
    import tia.itsm as itsm_mod

    class Secrets:
        def get_text(self, name):
            return {"itsm/servicenow/username": "tia", "itsm/servicenow/password": "secret-pw"}.get(name)
    real = itsm_mod.client_for
    monkeypatch.setattr(itsm_mod, "client_for", lambda cfg, secrets: real(cfg, Secrets()))
    r = c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/change-record", json={})
    assert r.status_code == 200 and r.json()["created"] and r.json()["change_record"] == "CHG0031234"
    ev = c.get("/api/v1/evidence?kind=itsm_attachment").json()["records"]
    assert ev and ev[0]["data"]["analysis_id"] == rep["analysis_id"]
    assert c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/change-record", json={"change_record": "CHG0000001"}).status_code == 400
    assert c.put("/api/v1/tenant/config", json={"config": {"itsm": {"provider": "servicenow", "instance_url": "http://x"}}}).status_code == 422


# ---- MCP ------------------------------------------------------------------------------------------------------------------------------
def rpc(c, method, params=None, mid=1, headers=None):
    msg = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}), **({"params": params} if params is not None else {})}
    return c.post("/mcp", json=msg, headers=headers or {})


def test_mcp_handshake_and_read_only_tools(world):
    c, app, land, rep = world
    init = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).json()
    assert init["result"]["serverInfo"]["name"] == "tia" and "no blast-radius claim in a review without a TIA analysis id" in init["result"]["instructions"]
    assert rpc(c, "notifications/initialized", mid=None).status_code == 202
    tools = rpc(c, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_impact_report", "find_impact_report", "get_test_scope", "get_policy_outcome"}
    assert all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] for t in tools)      # AC-032: no write tool
    bad = rpc(c, "tools/call", {"name": "record_relationship", "arguments": {}}).json()
    assert bad["error"]["code"] == -32602 and "read-only" in bad["error"]["message"]


def test_mcp_returns_the_report_json(world):
    c, app, land, rep = world
    out = rpc(c, "tools/call", {"name": "get_impact_report", "arguments": {"analysis_id": rep["analysis_id"]}}).json()["result"]
    assert out["isError"] is False and out["structuredContent"] == c.get(f"/api/v1/impact-analyses/{rep['analysis_id']}").json()
    assert json.loads(out["content"][0]["text"])["analysis_id"] == rep["analysis_id"]
    found = rpc(c, "tools/call", {"name": "find_impact_report", "arguments": {"repository": "payments-service", "revision": land["shas"]["behaviour"][:10]}}).json()["result"]
    assert found["structuredContent"]["analysis_id"] == rep["analysis_id"]
    scope = rpc(c, "tools/call", {"name": "get_test_scope", "arguments": {"analysis_id": rep["analysis_id"]}}).json()["result"]["structuredContent"]
    assert len(scope["test_scope"]["entry_points"]) == 3 and scope["test_recommendations"]["note"]
    pol = rpc(c, "tools/call", {"name": "get_policy_outcome", "arguments": {"analysis_id": rep["analysis_id"]}}).json()["result"]["structuredContent"]
    assert pol["analysis_id"] == rep["analysis_id"] and pol["mode"] == "advisory"
    miss = rpc(c, "tools/call", {"name": "get_impact_report", "arguments": {"analysis_id": "nope"}}).json()["result"]
    assert miss["isError"] and "no analysis 'nope'" in miss["content"][0]["text"]
    assert rpc(c, "tools/call", {"name": "get_impact_report", "arguments": {"analysis_id": "x", "write": True}}).json()["error"]["code"] == -32602


def test_mcp_sees_only_what_the_caller_may_read(world):
    c, app, land, rep = world
    h = {"X-TIA-Readable-Repos": "payments-service"}
    out = rpc(c, "tools/call", {"name": "get_impact_report", "arguments": {"analysis_id": rep["analysis_id"]}}, headers=h).json()["result"]
    assert "statement-service" not in json.dumps(out["structuredContent"]["findings"]) and out["structuredContent"]["redacted"]["findings"] >= 2
    assert c.post("/mcp", content=b"not json").status_code == 400
    assert rpc(c, "resources/list").json()["error"]["code"] == -32601
