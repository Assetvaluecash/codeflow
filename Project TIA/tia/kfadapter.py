"""Knowledge Fabric V2.5 adapter: a finished Change Impact Report in the shape KF agents already consume (QueryResponse).

Contract source: TIA_KF_V2_INTEGRATION.docx section 5 (the field lists of QueryRequest / QueryResponse / ChunkItem / PathItem /
RetrievalMetadata in kbfabric_ingest/retrieval/retriever_api.py). The KF source file itself was not available; the models below follow the
document field by field and must be re-checked against retriever_api.py before production use.

Rules this adapter keeps (document F1-F8, N3-N5):
* It serves FINISHED reports and never starts an analysis: an agent's question is not parsed to guess a repository or a change (no invented
  entities). The caller names the analysis (`analysis_id`) or the repository plus a pull request / revision, and gets the latest such report.
* Evidence status is never upgraded: it is carried in every chunk (extracted_fields) and path (source_evidence), and inferred or stale findings
  score lower than confirmed ones.
* Gaps stay visible: every unknown becomes a chunk (category "unknown"), and a partial or gapped report is stated in metadata.errors.
* Redaction is the caller's: the report is the one this principal may read, already redacted.
* The tester's view rides along: every entry point to test, rule gap and consistency check of the report's test scope is a chunk
  (categories test_scope / rule_gap / consistency_check) and every entry point's impact path a path, so a KF agent asked "what must I test
  for this change" gets the entry points, not the functions.
* Directions are TIA's own: TIA's edges point consumer -> provider; an impact path runs from the change outward, so a hop shows
  (changed)<-[:CONSUMES_API]-(consumer), never a relationship name TIA does not have.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

SEVERITY_SCORE = {"critical": 1.0, "high": 0.85, "medium": 0.6, "low": 0.35}
WEAK_EVIDENCE = {"inferred": 0.5, "stale": 0.5, "unknown": 0.3}
INTENT = "software_tia"
FORWARD = {"HANDLES", "PUBLISHES"}                       # the one traversed relationship whose stored direction is the direction of impact


# ---- the KF V2 wire format (document section 5); extra keys are refused so the contract cannot drift silently -----------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryRequest(_Strict):
    intent: str = INTENT
    query: str = ""
    domain: str = ""
    top_k_semantic: int = Field(10, ge=1, le=100)
    top_k_graph: int = Field(10, ge=1, le=100)
    # additive selectors for the TIA door (document 5.1 says software_tia is additive); the question text is never parsed
    analysis_id: Optional[str] = None
    repository: Optional[str] = None
    pull_request: Optional[int] = None
    revision: Optional[str] = None


class EntityRef(_Strict):
    name: str
    type: str


class ChunkItem(_Strict):
    text: str
    source_file: str
    category: str
    score: float
    weaviate_uuid: str
    block_id: str
    page_number: Optional[int]
    block_index: Optional[int]
    block_type: str
    kg_node_type: str
    domain: str
    extracted_fields: Optional[str]
    extracted_knowledge: Optional[str]


class PathItem(_Strict):
    path_text: str
    score: float
    from_entity: EntityRef
    relationship_type: str
    to_entity: EntityRef
    source_block_id: str
    source_file: str
    source_page: Optional[int]
    source_evidence: str


class DetectedDomain(_Strict):
    domain: str
    confidence: float


class RetrievalMetadata(_Strict):
    intent: str
    pattern: str
    max_depth: int
    latency_ms: float
    chunks_considered: int
    paths_considered: int
    errors: list[str]


class QueryResponse(_Strict):
    query: str
    domain: str
    detected_domains: list[DetectedDomain]
    chunks: list[ChunkItem]
    reranked_chunks: list[ChunkItem]
    paths: list[PathItem]
    reranked_paths: list[PathItem]
    metadata: RetrievalMetadata


# ---- projection ------------------------------------------------------------------------------------------------------------------
def score_of(f: dict) -> float:
    s = SEVERITY_SCORE.get(f.get("severity"), 0.2) * WEAK_EVIDENCE.get(f.get("evidence_status"), 1.0)
    return round(s, 4)


def _node_label(p: dict) -> str:
    return str(p.get("node") or "[redacted]")


def path_text(f: dict) -> str:
    """The impact path as a symbolic chain, each hop in TIA's stored direction."""
    nodes, edges = f.get("path") or [], f.get("path_edges") or []
    if not nodes:
        return ""
    out = f'({nodes[0].get("type")}:"{_node_label(nodes[0])}")'
    for i, e in enumerate(edges):
        nxt = nodes[i + 1] if i + 1 < len(nodes) else {"type": "unknown", "node": "?"}
        rel = e.get("rel", "RELATED")
        hop = f'-[:{rel}]->' if rel in FORWARD else f'<-[:{rel}]-'
        out += f'{hop}({nxt.get("type")}:"{_node_label(nxt)}")'
    return out


def _fields(rep: dict, f: dict, extra: Optional[dict] = None) -> str:
    d = {"analysis_id": rep["analysis_id"], "finding": f.get("id"), "severity": f.get("severity"), "material": bool(f.get("material")),
         "evidence_status": f.get("evidence_status"), "impact_type": f.get("impact_type"), "component": f.get("component"),
         "repository": f.get("repository"), "severity_rule": f.get("severity_rule"), "resolution_strategy": f.get("resolution_strategy"),
         "inference_rule": f.get("inference_rule"), "report_status": rep.get("status"), "configuration_version": rep.get("configuration_version"),
         **(extra or {})}
    return json.dumps({k: v for k, v in d.items() if v is not None}, sort_keys=True)


def chunk_for(rep: dict, i: int, f: dict, domain: str) -> ChunkItem:
    ev = f.get("evidence") or {}
    verify = " ".join(f.get("required_verification") or [])
    status = f.get("evidence_status", "unknown")
    caveat = "" if status in ("source_confirmed", "contract_confirmed", "corroborated") else f" Evidence is {status.upper()}: treat this as a lead, not a fact."
    text = (f"{(f.get('severity') or '').upper()} {f.get('impact_type', '')} impact on {f.get('component')} ({status})."
            f" {f.get('reason', '')}{caveat}" + (f" Verify: {verify}" if verify else "")).strip()
    return ChunkItem(text=text, source_file=str(ev.get("file") or ""), category=str(f.get("impact_type") or "finding"), score=score_of(f), weaviate_uuid="",
                     block_id=f"{rep['analysis_id']}#{i}", page_number=None, block_index=i, block_type="tia_finding", kg_node_type=str(f.get("node_type") or ""),
                     domain=domain, extracted_fields=_fields(rep, f), extracted_knowledge=None)


def path_for(rep: dict, i: int, f: dict) -> Optional[PathItem]:
    nodes, edges = f.get("path") or [], f.get("path_edges") or []
    if len(nodes) < 2 or not edges:
        return None
    ev = f.get("evidence") or {}
    first, last = nodes[0], nodes[-1]
    strategy = f" via {f['resolution_strategy']}" if f.get("resolution_strategy") else ""
    evidence = (f"{f.get('evidence_status')}{strategy}: {f.get('reason', '')}"
                + (f" [{ev.get('repo')}:{ev.get('file')}:{ev.get('line')}]" if ev.get("file") else ""))
    return PathItem(path_text=path_text(f), score=score_of(f), from_entity=EntityRef(name=_node_label(first), type=str(first.get("type") or "unknown")),
                    relationship_type=str(edges[0].get("rel") or "RELATED"), to_entity=EntityRef(name=_node_label(last), type=str(last.get("type") or "unknown")),
                    source_block_id=f"{rep['analysis_id']}#{i}", source_file=str(ev.get("file") or ""), source_page=None, source_evidence=evidence)


def unknown_chunk(rep: dict, j: int, u: dict, domain: str) -> ChunkItem:
    items = u.get("items") or []
    text = f"UNKNOWN ({u.get('area')}): {u.get('reason', '')}" + (f" {len(items)} item(s)." if items else "")
    return ChunkItem(text=text, source_file="", category="unknown", score=0.0, weaviate_uuid="", block_id=f"{rep['analysis_id']}#unknown-{j}", page_number=None,
                     block_index=None, block_type="tia_unknown", kg_node_type="unknown", domain=domain,
                     extracted_fields=json.dumps({"analysis_id": rep["analysis_id"], "area": u.get("area"), "affects_confidence": bool(u.get("affects_confidence")),
                                                  "items": len(items)}, sort_keys=True), extracted_knowledge=None)


# ---- the tester's view (report["test_scope"]): entry points to test, rule gaps, consistency checks -------------------------------------
EFFECT_SCORE = {"breaking": 0.9, "behaviour_change": 0.6, "unknown": 0.4, "compatible_change": 0.3, "no_functional_change": 0.1}
EFFECT_TEXT = {"breaking": "BREAKING change", "behaviour_change": "behaviour change", "compatible_change": "compatible change",
               "no_functional_change": "no functional change", "unknown": "unclassified (file-level) change"}


def _caveat(status: str) -> str:
    return "" if status in ("source_confirmed", "contract_confirmed", "corroborated") else f" Evidence is {str(status).upper()}: treat this as a lead, not a fact."


def scope_chunks(rep: dict, domain: str) -> list:
    """One chunk per entry point to test, rule gap and consistency check, highest priority first."""
    ts = rep.get("test_scope") or {}
    aid, out = rep["analysis_id"], []
    for k, e in enumerate(ts.get("entry_points", []) + ts.get("code_without_entry_point", [])):
        tests = ", ".join(t["name"] for t in e["tests"][:5]) or "none found in the analysed sources"
        label = f"{e['label']}: " if e.get("label") else ""
        text = f"TEST {label}{e['entry_point']} ({e['service']}) - {EFFECT_TEXT[e['change_effect']]}. {e['why']} Tests found: {tests}.{_caveat(e['evidence_status'])}"
        fields = {"analysis_id": aid, "entry_point": e["entry_point"], "service": e["service"], "repository": e["repo"], "change_effect": e["change_effect"],
                  "evidence_status": e["evidence_status"], "cross_repository": e["cross_repository"], "has_entry_point": e["type"] == "endpoint",
                  "tests": len(e["tests"]), **({"label": e["label"]} if e.get("label") else {})}
        out.append(ChunkItem(text=text, source_file=str(e.get("file") or ""), category="test_scope",
                             score=round(EFFECT_SCORE[e["change_effect"]] * WEAK_EVIDENCE.get(e["evidence_status"], 1.0), 4), weaviate_uuid="",
                             block_id=f"{aid}#test-{k}", page_number=None, block_index=None, block_type="tia_test_target", kg_node_type=e["type"],
                             domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    for k, gp in enumerate(ts.get("rule_gaps", [])):
        head = "RULE MOVED INTO SHARED SERVICE" if gp.get("resolved_by_this_change") else "POSSIBLE RULE GAP"
        fields = {"analysis_id": aid, "shared_entry_point": gp["shared_entry_point"], "service": gp["service"], "rule_fields": gp["rule"]["fields"],
                  "rule": gp["rule"]["expr"], "applied_by": [a["entry_point"] for a in gp["applied_by"]], "missing_in": [m["entry_point"] for m in gp["missing_in"]],
                  "resolved_by_this_change": bool(gp.get("resolved_by_this_change")), "evidence_status": gp["evidence_status"]}
        out.append(ChunkItem(text=f"{head}: {gp['reason']}", source_file=str(gp["applied_by"][0].get("function_file") or ""), category="rule_gap", score=0.8,
                             weaviate_uuid="", block_id=f"{aid}#rule-{k}", page_number=None, block_index=None, block_type="tia_rule_gap",
                             kg_node_type="endpoint", domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    for k, c in enumerate(ts.get("consistency_checks", [])):
        fields = {"analysis_id": aid, "shared_entry_point": c["shared_entry_point"], "service": c["service"],
                  "entry_points": [x["entry_point"] for x in c["entry_points"]]}
        out.append(ChunkItem(text=f"CONSISTENCY CHECK: {c['check']}", source_file="", category="consistency_check", score=0.5, weaviate_uuid="",
                             block_id=f"{aid}#check-{k}", page_number=None, block_index=None, block_type="tia_consistency_check",
                             kg_node_type="endpoint", domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    return out


def scope_paths(rep: dict) -> list:
    """The impact path to each entry point to test, from the change to the entry point."""
    ts = rep.get("test_scope") or {}
    out = []
    for k, e in enumerate(ts.get("entry_points", []) + ts.get("code_without_entry_point", [])):
        nodes, edges = e.get("path") or [], e.get("path_edges") or []
        if len(nodes) < 2 or not edges:
            continue
        out.append(PathItem(path_text=path_text(e), score=round(EFFECT_SCORE[e["change_effect"]] * WEAK_EVIDENCE.get(e["evidence_status"], 1.0), 4),
                            from_entity=EntityRef(name=_node_label(nodes[0]), type=str(nodes[0].get("type") or "unknown")),
                            relationship_type=str(edges[0].get("rel") or "RELATED"),
                            to_entity=EntityRef(name=_node_label(nodes[-1]), type=str(nodes[-1].get("type") or "unknown")),
                            source_block_id=f"{rep['analysis_id']}#test-{k}", source_file=str(e.get("file") or ""), source_page=None,
                            source_evidence=f"{e['evidence_status']}: {e['why']}"))
    return out


def empty(req: QueryRequest, errors: list[str], t0: float) -> QueryResponse:
    return QueryResponse(query=req.query, domain=req.domain, detected_domains=[DetectedDomain(domain=req.domain, confidence=1.0)] if req.domain else [],
                         chunks=[], reranked_chunks=[], paths=[], reranked_paths=[],
                         metadata=RetrievalMetadata(intent=req.intent, pattern="", max_depth=0, latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                                                    chunks_considered=0, paths_considered=0, errors=errors))


def to_query_response(rep: dict, req: QueryRequest, t0: Optional[float] = None) -> QueryResponse:
    """Project one (already redacted) report. Material findings rank first in reranked_*; unknowns are always included."""
    t0 = t0 if t0 is not None else time.perf_counter()
    findings = rep.get("findings") or []
    chunks = [chunk_for(rep, i, f, req.domain) for i, f in enumerate(findings)]
    unknown_chunks = [unknown_chunk(rep, j, u, req.domain) for j, u in enumerate(rep.get("unknowns") or [])]
    paths = [p for p in (path_for(rep, i, f) for i, f in enumerate(findings)) if p is not None]
    order = sorted(range(len(findings)), key=lambda i: (not findings[i].get("material"), -score_of(findings[i]), i))
    reranked_chunks = [chunks[i] for i in order][: req.top_k_semantic]
    by_block = {p.source_block_id: p for p in paths}
    reranked_paths = [by_block[f"{rep['analysis_id']}#{i}"] for i in order if f"{rep['analysis_id']}#{i}" in by_block]
    # the tester's view: what to test because of the change, ranked on its own (rule gaps first) and bounded by the same top_k
    tchunks = scope_chunks(rep, req.domain)
    tpaths = scope_paths(rep)
    rank = {"rule_gap": 0, "test_scope": 1, "consistency_check": 2}
    reranked_chunks = reranked_chunks + sorted(tchunks, key=lambda c: (rank[c.category], -c.score, c.block_id))[: req.top_k_semantic]
    reranked_paths = (reranked_paths + sorted(tpaths, key=lambda p: (-p.score, p.source_block_id)))[: req.top_k_graph]
    chunks, paths = chunks + tchunks, paths + tpaths
    # unknowns are never ranked away: they ride along after the findings, whatever top_k says
    reranked_chunks = reranked_chunks + unknown_chunks
    errors = []
    if rep.get("status") in ("partial", "complete_with_gaps", "failed"):
        errors.append(f"report status {rep['status']}: " + ("the analysis is INCOMPLETE" if rep["status"] == "partial" else
                                                            "the analysis FAILED" if rep["status"] == "failed" else "finished with unknowns that limit confidence"))
    errors += [f"unknown: {u.get('area')}" for u in rep.get("unknowns") or [] if u.get("affects_confidence")]
    if (rep.get("redacted") or {}).get("findings"):
        errors.append(rep["redacted"]["note"])
    trav = (rep.get("coverage") or {}).get("traversal") or {}
    if trav.get("truncated"):
        errors.append("traversal truncated by the configured limits")
    return QueryResponse(
        query=req.query, domain=req.domain, detected_domains=[DetectedDomain(domain=req.domain, confidence=1.0)] if req.domain else [],
        chunks=chunks + unknown_chunks, reranked_chunks=reranked_chunks, paths=paths, reranked_paths=reranked_paths,
        metadata=RetrievalMetadata(intent=req.intent, pattern=f"{rep['analysis_id']} ({rep.get('status')}) {rep.get('change', {}).get('repository', '')}".strip(),
                                   max_depth=int(trav.get("max_depth") or 0), latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                                   chunks_considered=len(chunks) + len(unknown_chunks), paths_considered=len(paths), errors=errors))
