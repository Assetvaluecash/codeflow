"""Relationship history and staleness (UC1-12, AC-011, FR-005).

A relationship is a confirmed cross-repository dependency: a consumer file that calls (or declares a contract with) a
provider endpoint. Each one is tracked as:

  current  confirmed at the analysed revisions of both repositories
  stale    no longer confirmed; stale_reason says why:
             scan_failed       a repository's latest scan failed, so the relationship cannot be verified against its
                               latest source (it may still hold: treat as potentially affected)
             parse_failed      the consumer file no longer parses at the analysed revision (same consequence)
             evidence_removed  the evidence is gone at the analysed revision: the call, file, endpoint or repository
                               was removed (the relationship ended)

A stale relationship is never traversed as current and never presented as current."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .graph import Graph
from .store import Store

UNVERIFIABLE = ("scan_failed", "parse_failed")        # stale relationships that may still be real


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(epoch: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def observed(graph: Graph) -> dict:
    """Cross-repository relationships present in a graph, keyed by (consumer repo, consumer file, provider endpoint)."""
    out = {}
    for e in graph.edges:
        if e.rel != "CONSUMES_API":
            continue
        src, dst = graph.nodes[e.src], graph.nodes[e.dst]
        if src["repo"] == dst["repo"]:
            continue
        key = f"{src['repo']}|{src.get('file')}|{e.dst}"
        rec = out.get(key)
        line = src.get("line") or 0
        if rec is None or (line and line < rec["consumer_line"]):
            out[key] = {"key": key, "consumer_repo": src["repo"], "consumer_service": src["service"],
                        "consumer_file": src.get("file"), "consumer_line": line, "provider_repo": dst["repo"],
                        "provider_service": dst["service"], "provider_endpoint": e.dst, "method": dst.get("method"),
                        "path": dst.get("path"), "strategy": e.strategy, "evidence_status": e.status}
    return out


def _stale_reason(old: dict, scans: dict, extracts: dict) -> tuple[str, str]:
    c, p = old["consumer_repo"], old["provider_repo"]
    for r in (c, p):
        if r not in scans:
            return "evidence_removed", f"repository '{r}' was removed from TIA"
    for r in (c, p):
        if scans[r].scan_status == "failed" or r not in extracts:
            err = scans[r].scan_error or "extraction failed"
            return "scan_failed", f"the latest scan of '{r}' failed ({err[:160]}); last confirmed at {old.get('last_confirmed_commit', '?')[:7]}"
    if any(f["file"] == old["consumer_file"] for f in extracts[c].files_failed):
        return "parse_failed", f"{old['consumer_file']} in '{c}' no longer parses at {extracts[c].commit[:7]}"
    return "evidence_removed", f"no longer found in '{c}' at {extracts[c].commit[:7]}"


def reconcile(store: Store, graph: Graph, extracts: dict, repos: list, graph_id: str) -> dict:
    """Bring the relationship history in line with the current graph and the repositories' scan outcomes."""
    ts = _now()
    scans = {r.name: r for r in repos}
    prev = {r["key"]: r for r in store.list_relationships()}
    seen = observed(graph)
    write, became_stale, recovered, new = [], [], [], []

    for key, rec in seen.items():
        old = prev.get(key)
        c, p = rec["consumer_repo"], rec["provider_repo"]
        commits = {"last_confirmed_consumer_commit": extracts[c].commit, "last_confirmed_provider_commit": extracts[p].commit}
        bad = next((r for r in (c, p) if scans.get(r) and scans[r].scan_status == "failed"), None)
        if bad:
            # seen at the last analysed revision, but that revision is no longer known to be the repository's latest
            confirmed_at = (old or {}).get("last_confirmed_at") or _iso(scans[bad].last_successful_scan) or ts
            doc = {**rec, "state": "stale", "stale_reason": "scan_failed", "graph_id": graph_id,
                   "stale_detail": f"the latest scan of '{bad}' failed ({(scans[bad].scan_error or 'unknown error')[:160]}); "
                                   f"last confirmed at {extracts[c].commit[:7]}",
                   "stale_since": (old or {}).get("stale_since") if (old or {}).get("state") == "stale" else ts,
                   "first_seen_at": (old or {}).get("first_seen_at", confirmed_at), "last_confirmed_at": confirmed_at,
                   **{k: (old or {}).get(k, v) for k, v in commits.items()}}
            if not old or old["state"] != "stale" or old.get("stale_reason") != "scan_failed":
                became_stale.append(key)
        else:
            doc = {**rec, "state": "current", "stale_reason": None, "stale_detail": None, "stale_since": None,
                   "graph_id": graph_id, "first_seen_at": (old or {}).get("first_seen_at", ts), "last_confirmed_at": ts,
                   **commits}
            if old and old["state"] == "current" and all(old.get(k) == v for k, v in commits.items()) \
                    and old.get("evidence_status") == rec["evidence_status"] and old.get("strategy") == rec["strategy"]:
                continue                                  # unchanged: do not rewrite
            if old and old["state"] == "stale":
                recovered.append(key)
            elif not old:
                new.append(key)
        write.append(doc)

    for key, old in prev.items():
        if key in seen:
            continue
        reason, detail = _stale_reason(old, scans, extracts)
        if old["state"] == "stale" and old.get("stale_reason") == reason and old.get("stale_detail") == detail:
            continue
        if old["state"] == "current":
            became_stale.append(key)
        write.append({**old, "state": "stale", "stale_reason": reason, "stale_detail": detail, "graph_id": graph_id,
                      "stale_since": old.get("stale_since") or ts})

    store.put_relationships(write)
    rows = store.list_relationships()
    summary = {"current": sum(1 for r in rows if r["state"] == "current"),
               "stale": sum(1 for r in rows if r["state"] == "stale"),
               "became_stale": sorted(became_stale), "recovered": sorted(recovered), "new": sorted(new)}
    if became_stale or recovered or new:
        store.audit("relationships_reconciled", graph=graph_id, current=summary["current"], stale=summary["stale"],
                    became_stale=len(became_stale), recovered=len(recovered), new=len(new))
    return summary
