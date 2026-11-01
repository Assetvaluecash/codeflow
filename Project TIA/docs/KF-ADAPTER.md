# Knowledge Fabric V2.5 adapter

`POST /query/tia` returns a finished TIA Change Impact Report in Knowledge Fabric's agent wire format (`QueryResponse`), so agents that only
speak KF V2 can read software impact. It implements option **B** of `TIA_KF_V2_INTEGRATION.docx` (a sibling route; KF's own `/query` is untouched).
The native report (`GET /api/v1/impact-analyses/{id}`) remains the product deliverable.

## Request

```json
{"intent": "software_tia", "query": "What does PR 1842 affect?", "domain": "corporate_commercial",
 "top_k_semantic": 10, "top_k_graph": 10,
 "analysis_id": "<id>"}
```

Instead of `analysis_id`, give `repository` plus `pull_request` or `revision` (a commit or a 7+ character prefix): the newest such report is used.
**The question text is never parsed**: without a selector the answer is an empty response with an explanation in `metadata.errors`. No analysis is
started by this route; queue one with `POST /api/v1/impact-analyses` first. Unknown request fields are refused (422). Needs the `read` permission and
the usual credential; the report is redacted for the caller exactly as in the native API.

## Response mapping

| TIA | QueryResponse |
|---|---|
| each finding | a `ChunkItem` (`block_type: "tia_finding"`, `block_id: "<analysis>#<n>"`, `category`: impact type, `kg_node_type`: node type) and, when it has an impact path, a `PathItem` |
| severity x evidence | `score`: critical 1.0, high 0.85, medium 0.6, low 0.35, halved for `inferred`/`stale` |
| evidence status, rule, strategy, configuration | `extracted_fields` (stringified JSON) and `source_evidence`; text of a weak finding says "treat this as a lead, not a fact" |
| impact path | `path_text` in TIA's stored edge direction, e.g. `(endpoint:"payments-service:GET /payments/{}/status")<-[:CONSUMES_API]-(callsite:"...")`; `relationship_type` is the first hop's real TIA relationship |
| unknowns | `ChunkItem`s with `category: "unknown"`, always appended to `reranked_chunks` whatever `top_k` is; those that affect confidence also appear in `metadata.errors` |
| status `partial` / `complete_with_gaps` / `failed`, redaction, truncation | `metadata.errors` |
| material findings | ranked first in `reranked_chunks` / `reranked_paths` |

## Verification status

Tested against the field lists printed in the integration document (`tests/test_kfadapter.py`). **KF's actual `retriever_api.py` was not available**:
re-check the models in `tia/kfadapter.py` against it before connecting a real KF deployment. KF's facade or agent wiring is not part of TIA.
