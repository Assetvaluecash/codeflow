"""Temporal relationships and release comparisons (PRD FR-011, FR-030, AC-033).

A release is a named set of repository revisions ({repo: commit}); TIA builds the dependency graph at exactly those revisions and keeps it
(graph versions are content-addressed, so the same revisions are the same graph). Two releases are compared relationship by relationship:
introduced, removed, or modified (same two ends, different evidence status or resolution strategy). The graph of a release answers "which
consumers were known to use this contract at the time of that release", and deployment records answer "which version of a service ran
in an environment at a given time".
"""
from __future__ import annotations

from typing import Optional

DEPENDENCY_RELS = ("CONSUMES_API", "CONSUMES_EVENT", "PUBLISHES", "USES_SCHEMA", "READS", "WRITES", "MAPS_TO", "CALLS", "TESTS")


def _key(g, e) -> tuple:
    """A relationship's identity across versions: its kind and its two ends, with call sites identified by their service and target (a
    call site moving to another line is the same dependency)."""
    def end(n):
        a = g.nodes.get(n) or {}
        if a.get("type") in ("callsite", "listener", "runtime_ref", "contract_ref"):
            return f"{a.get('type')}@{a.get('service') or a.get('repo')}"
        return n
    return (e.rel, end(e.src), e.dst)


def relationships(g, rels=DEPENDENCY_RELS) -> dict:
    out = {}
    for e in g.edges:
        if e.rel in rels:
            k = _key(g, e)
            out.setdefault(k, {"rel": e.rel, "from": e.src, "to": e.dst, "status": e.status, "strategy": e.strategy,
                               "from_service": (g.nodes.get(e.src) or {}).get("service"), "to_service": (g.nodes.get(e.dst) or {}).get("service"),
                               "evidence": e.evidence})
    return out


def compare(g_from, g_to, rels=DEPENDENCY_RELS, cross_service_only: bool = False) -> dict:
    """AC-033: the dependencies introduced, removed and modified between two graphs."""
    a, b = relationships(g_from, rels), relationships(g_to, rels)
    keep = (lambda r: r["from_service"] != r["to_service"]) if cross_service_only else (lambda r: True)
    introduced = [b[k] for k in sorted(set(b) - set(a), key=str) if keep(b[k])]
    removed = [a[k] for k in sorted(set(a) - set(b), key=str) if keep(a[k])]
    modified = [{"rel": b[k]["rel"], "from": b[k]["from"], "to": b[k]["to"], "from_service": b[k]["from_service"], "to_service": b[k]["to_service"],
                 "before": {"status": a[k]["status"], "strategy": a[k]["strategy"]}, "after": {"status": b[k]["status"], "strategy": b[k]["strategy"]}}
                for k in sorted(set(a) & set(b), key=str) if (a[k]["status"], a[k]["strategy"]) != (b[k]["status"], b[k]["strategy"]) and keep(b[k])]
    return {"introduced": introduced, "removed": removed, "modified": modified,
            "summary": {"introduced": len(introduced), "removed": len(removed), "modified": len(modified)}}


def consumers_of(g, node: str) -> list:
    """Who depended on an endpoint, schema or topic in this graph (direct consumers, with evidence)."""
    out = []
    for n, e in g.impacted_by(node):
        if e.rel in ("CONSUMES_API", "CONSUMES_EVENT", "USES_SCHEMA"):
            a = g.nodes.get(n) or {}
            out.append({"node": n, "type": a.get("type"), "service": a.get("service"), "repo": a.get("repo"), "file": a.get("file"),
                        "line": a.get("line"), "rel": e.rel, "status": e.status, "strategy": e.strategy})
    return sorted(out, key=lambda x: (x["service"] or "", x["node"]))


def deployed_at(records: list, at: str, repo: Optional[str] = None) -> dict:
    """FR-011: per repository and environment, the revision that was running at `at` (the latest deployment at or before it)."""
    out: dict = {}
    for r in sorted(records, key=lambda r: (r["data"]["deployed_at"], r["id"])):
        d = r["data"]
        key = d.get("repo") or d.get("service")
        if (repo and key != repo) or d["deployed_at"] > at:
            continue
        out.setdefault(key, {})[d["environment"]] = {"revision": d["revision"], "deployed_at": d["deployed_at"], "artifact": d.get("artifact"),
                                                     "evidence_id": r["id"]}
    return out
