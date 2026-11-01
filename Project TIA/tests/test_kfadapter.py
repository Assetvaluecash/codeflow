"""Knowledge Fabric V2.5 adapter: TIA reports as KF QueryResponse objects (TIA_KF_V2_INTEGRATION.docx sections 5-6, requirements F1-F8)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import bearer, make_auth_app, token_for  # noqa: E402
from tia import kfadapter  # noqa: E402
from tia.impact import AnalysisRequest, analyze  # noqa: E402

# the field lists exactly as the integration document prints them (section 5.2-5.4), written out independently of tia/kfadapter.py
TOP = {"query", "domain", "detected_domains", "chunks", "reranked_chunks", "paths", "reranked_paths", "metadata"}
META = {"intent", "pattern", "max_depth", "latency_ms", "chunks_considered", "paths_considered", "errors"}
CHUNK = {"text", "source_file", "category", "score", "weaviate_uuid", "block_id", "page_number", "block_index", "block_type", "kg_node_type", "domain",
         "extracted_fields", "extracted_knowledge"}
PATH = {"path_text", "score", "from_entity", "relationship_type", "to_entity", "source_block_id", "source_file", "source_page", "source_evidence"}


@pytest.fixture(scope="module")
def reports(fx):
    run = lambda head: analyze(AnalysisRequest(repos=fx["paths"], target="payments-service", base=fx["shas"]["base"], head=fx["shas"][head]), None)
    return {h: run(h) for h in ("breaking", "syntax_error", "docs_only")}


def project(rep, **kw):
    return json.loads(kfadapter.to_query_response(rep, kfadapter.QueryRequest(domain="corporate_commercial", query="q", **kw)).model_dump_json())


def test_the_response_has_exactly_the_documented_shape(reports):
    out = project(reports["breaking"])
    assert set(out) == TOP and set(out["metadata"]) == META
    assert out["chunks"] and out["paths"] and out["reranked_chunks"] and out["reranked_paths"]
    for c in out["chunks"] + out["reranked_chunks"]:
        assert set(c) == CHUNK
        assert c["extracted_fields"] is None or isinstance(json.loads(c["extracted_fields"]), dict)          # stringified JSON, the V2 convention
        assert c["page_number"] is None and c["weaviate_uuid"] == ""
    for p in out["paths"] + out["reranked_paths"]:
        assert set(p) == PATH and set(p["from_entity"]) == {"name", "type"} and set(p["to_entity"]) == {"name", "type"}
    assert out["detected_domains"] == [{"domain": "corporate_commercial", "confidence": 1.0}]
    assert out["metadata"]["intent"] == "software_tia" and reports["breaking"]["analysis_id"] in out["metadata"]["pattern"]


def test_extra_fields_are_impossible():
    with pytest.raises(Exception):
        kfadapter.ChunkItem(**{k: "" for k in CHUNK}, surprise=1)


def test_evidence_status_is_carried_and_never_upgraded(reports):
    rep = reports["breaking"]
    out = project(rep)
    by_block = {c["block_id"]: c for c in out["chunks"]}
    for i, f in enumerate(rep["findings"]):
        c = by_block[f"{rep['analysis_id']}#{i}"]
        assert json.loads(c["extracted_fields"])["evidence_status"] == f["evidence_status"]
        if f["evidence_status"] == "inferred":
            assert "INFERRED" in c["text"] and "lead, not a fact" in c["text"]
            assert c["score"] < kfadapter.SEVERITY_SCORE[f["severity"]]                      # an inferred finding never scores like a confirmed one
    assert any(json.loads(c["extracted_fields"]).get("evidence_status") == "inferred" for c in out["chunks"] if c["block_type"] == "tia_finding")
    for p in out["paths"]:
        assert p["source_evidence"].split(":")[0].split(" ")[0] in ("source_confirmed", "contract_confirmed", "inferred", "stale", "corroborated")


def test_relationships_are_tias_own_and_point_the_way_tia_stores_them(reports):
    out = project(reports["breaking"])
    rels = {p["relationship_type"] for p in out["paths"]}
    assert rels <= {"CONSUMES_API", "CALLS", "HANDLES", "USES_SCHEMA", "TESTS", "MAPS_TO", "MAPS_COLUMN", "READS", "WRITES", "ISSUES"}
    assert "CONSUMED_BY" not in json.dumps(out)                                               # the document's example name does not exist in TIA
    consumes = [p for p in out["paths"] if "CONSUMES_API" in p["path_text"]]
    assert consumes and all("<-[:CONSUMES_API]-" in p["path_text"] for p in consumes)         # consumer -> provider, read from the change outward


def test_material_findings_rank_first_and_top_k_is_respected(reports):
    rep = reports["breaking"]
    out = project(rep, top_k_semantic=2, top_k_graph=1)
    findings = [c for c in out["reranked_chunks"] if c["block_type"] == "tia_finding"]
    assert len(findings) <= 2 and len(out["reranked_paths"]) <= 1
    mats = [json.loads(c["extracted_fields"])["material"] for c in findings]
    assert mats == sorted(mats, reverse=True)


def test_unknowns_are_never_dropped_even_with_a_small_top_k(reports):
    rep = reports["breaking"]
    out = project(rep, top_k_semantic=1)
    unknown = [c for c in out["reranked_chunks"] if c["category"] == "unknown"]
    assert len(unknown) == len(rep["unknowns"]) > 0 and all(c["text"].startswith("UNKNOWN (") for c in unknown)
    assert any(e.startswith("unknown: ") for e in out["metadata"]["errors"])


def test_a_partial_report_says_so(reports):
    rep = reports["syntax_error"]
    out = project(rep)
    assert rep["status"] == "partial" and out["metadata"]["errors"][0].startswith("report status partial") and "INCOMPLETE" in out["metadata"]["errors"][0]
    assert any("changes_not_evaluated" in e or "parse_failures" in e for e in out["metadata"]["errors"])


def test_a_report_with_nothing_to_say_is_still_a_valid_response(reports):
    out = project(reports["docs_only"])
    assert out["paths"] == [] and set(out) == TOP


def test_a_redacted_report_says_findings_were_left_out(reports):
    from tia.report import redact
    red = redact(reports["breaking"], ["payments-service", "mobile-bff"])
    out = project(red)
    assert "reconciliation-service" not in json.dumps(out) and any("omitted" in e for e in out["metadata"]["errors"])


# ---- through the application ------------------------------------------------------------------------------------------------------
@pytest.fixture
def served(fx, tmp_path):
    app, _ = make_auth_app(tmp_path)
    tid = conftest.new_tenant()
    c = TestClient(app)
    h = bearer(token_for(tid, ["engineer"], name="eng"))
    for n, p in fx["paths"].items():
        c.post("/api/v1/repositories", headers=h, json={"path": p, "name": n})
    aid = c.post("/api/v1/impact-analyses", headers=h, json={"target": "payments-service", "head": "breaking"}).json()["analysis_id"]
    return c, tid, h, aid


def test_by_analysis_id_and_by_repository(served, fx):
    c, tid, h, aid = served
    a = c.post("/query/tia", headers=h, json={"intent": "software_tia", "query": "what breaks?", "domain": "d", "analysis_id": aid}).json()
    assert set(a) == TOP and aid in a["metadata"]["pattern"] and a["reranked_chunks"]
    b = c.post("/query/tia", headers=h, json={"query": "x", "domain": "d", "repository": "payments-service", "revision": fx["shas"]["breaking"][:9]}).json()
    assert aid in b["metadata"]["pattern"]


def test_the_question_text_alone_selects_nothing(served):
    c, tid, h, aid = served
    out = c.post("/query/tia", headers=h, json={"query": "Impact of the PaymentStatus change in payments-service", "domain": "d"}).json()
    assert out["chunks"] == [] and "does not infer" in out["metadata"]["errors"][0]


def test_other_intents_are_not_served_here(served):
    c, tid, h, aid = served
    out = c.post("/query/tia", headers=h, json={"intent": "change_impact", "query": "x", "domain": "d", "analysis_id": aid}).json()
    assert out["chunks"] == [] and "not served by TIA" in out["metadata"]["errors"][0]


def test_access_rules_apply(served, fx, tmp_path):
    c, tid, h, aid = served
    assert c.post("/query/tia", json={"analysis_id": aid}).status_code == 401
    scoped = bearer(token_for(tid, ["viewer"], name="scoped", repos=["mobile-bff"]))
    out = c.post("/query/tia", headers=scoped, json={"analysis_id": aid, "domain": "d"}).json()
    assert out["chunks"] == [] and out["metadata"]["errors"] == ["no analysis found for that selection"]        # the target is not theirs: no oracle
    other = bearer(token_for(conftest.new_tenant(), ["viewer"]))
    assert c.post("/query/tia", headers=other, json={"analysis_id": aid}).json()["chunks"] == []
    narrow = bearer(token_for(tid, ["viewer"], name="narrow", repos=["payments-service", "mobile-bff"]))
    out = c.post("/query/tia", headers=narrow, json={"analysis_id": aid, "domain": "d"}).json()
    assert out["chunks"] and "reconciliation-service" not in json.dumps(out)


def test_unknown_fields_in_the_request_are_refused(served):
    c, tid, h, aid = served
    assert c.post("/query/tia", headers=h, json={"analysis_id": aid, "run_analysis": True}).status_code == 422
