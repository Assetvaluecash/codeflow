"""Identity and relationship resolution (TFD 5.4 / 5.5).

This is the only place cross-repository relationships are created, and only through
the enumerated strategies:

  R1 generated/shared client      -> source_confirmed
  R2 fully resolved HTTP call     -> source_confirmed
  R3 declared contract (pact-like)-> contract_confirmed
  R4 service map (input to R1/R2 only)
  R5 unique path match (INF-PATH-UNIQUE) -> inferred

Matching names alone never creates a relationship.
"""
from __future__ import annotations

from collections import defaultdict

from .models import (CONFIRMED, CONTRACT_CONFIRMED, CORROBORATED, INFERRED,
                     SOURCE_CONFIRMED, Edge, RepoExtract, normalize_path)


def merged_service_map(extracts: dict[str, RepoExtract]) -> dict:
    m = {"config_keys": {}, "hosts": {}, "packages": {}, "criticality": {}, "owners": {}}
    for ex in extracts.values():
        sm = ex.service_map or {}
        for k in ("config_keys", "hosts", "packages"):
            m[k].update(sm.get(k) or {})
        if sm.get("criticality"):
            m["criticality"][ex.service] = sm["criticality"]
        if sm.get("owner"):
            m["owners"][ex.service] = sm["owner"]
    return m


def resolve_consumers(extracts: dict[str, RepoExtract], strip_prefixes: tuple = ()):
    """-> (edges, unresolved, stats, synthetic_nodes)

    edges: CONSUMES_API edges (callsite|contract_ref -> endpoint)
    unresolved: [{id, repo, file, line, reason}]
    """
    smap = merged_service_map(extracts)
    services = {ex.service: ex for ex in extracts.values()}
    endpoints_by_service: dict[str, list] = defaultdict(list)
    endpoints_by_key: dict[tuple, list] = defaultdict(list)
    for ex in extracts.values():
        for e in ex.endpoints:
            endpoints_by_service[e.service].append(e)
            endpoints_by_key[(e.method, e.norm_path)].append(e)

    edges: list[Edge] = []
    unresolved: list[dict] = []
    by_strategy: dict[str, int] = defaultdict(int)
    synthetic: dict[str, dict] = {}
    resolved_ids: dict[str, Edge] = {}
    total = 0

    def ev(cs):
        return {"repo": cs.repo, "file": cs.file, "line": cs.line}

    for ex in extracts.values():
        for cs in ex.callsites:
            total += 1
            edge = None
            reason = cs.reason

            # ---- R1: generated / shared client --------------------------------------------
            if cs.client_pkg:
                prov = smap["packages"].get(cs.client_pkg)            # R4 input
                if prov and prov in endpoints_by_service:
                    cands = [e for e in endpoints_by_service[prov]
                             if e.handler.split(".")[-1] == cs.client_attr]
                    if len(cands) == 1:
                        edge = Edge(cs.id, "CONSUMES_API", cands[0].id, SOURCE_CONFIRMED, "R1",
                                    None, ev(cs))
                    else:
                        reason = (f"generated client method '{cs.client_attr}' matches "
                                  f"{len(cands)} operations in {prov}")
                else:
                    reason = f"client package '{cs.client_pkg}' is not mapped to a provider service"

            # ---- R2: fully resolved HTTP call ------------------------------------------------
            elif cs.norm_path is not None and cs.base_kind:
                prov = None
                if cs.base_kind == "config_key":
                    prov = smap["config_keys"].get(cs.base_value)      # R4
                elif cs.base_kind == "host":
                    prov = smap["hosts"].get(cs.base_value) or (cs.base_value if cs.base_value in services else None)
                if prov and prov in endpoints_by_service:
                    cands = [e for e in endpoints_by_service[prov]
                             if e.norm_path == cs.norm_path and (cs.method is None or e.method == cs.method)]
                    if len(cands) == 1:
                        edge = Edge(cs.id, "CONSUMES_API", cands[0].id, SOURCE_CONFIRMED, "R2", None, ev(cs))
                    elif cands:
                        reason = f"call matches {len(cands)} endpoints in {prov}"
                    else:
                        reason = f"no endpoint {cs.method or '*'} {cs.norm_path} found in {prov}"
                elif prov:
                    reason = f"provider {prov} not in analysed scope"

            # ---- R5: unique path match (inferred) -----------------------------------------------
            if edge is None and cs.norm_path is not None and cs.client_pkg is None:
                cands = [e for e in endpoints_by_key.get((cs.method or "GET", cs.norm_path), [])
                         if e.service != cs.service]
                if cs.method is None:
                    cands = [e for k, v in endpoints_by_key.items() if k[1] == cs.norm_path for e in v
                             if e.service != cs.service]
                if len(cands) == 1 and not (cs.base_kind and reason and "no endpoint" in reason):
                    edge = Edge(cs.id, "CONSUMES_API", cands[0].id, INFERRED, "R5", "INF-PATH-UNIQUE", ev(cs))
                elif len(cands) > 1:
                    reason = (reason or "") + f" path matches {len(cands)} endpoints (ambiguous)"
                elif not reason:
                    reason = "no endpoint with this path in analysed scope"

            if edge:
                edges.append(edge)
                resolved_ids[cs.id] = edge
            else:
                unresolved.append({"id": cs.id, "repo": cs.repo, "file": cs.file, "line": cs.line,
                                   "reason": (reason or "unresolved outbound call").strip()})

    # ---- R3: declared contracts ------------------------------------------------------------------
    for ex in extracts.values():
        for ci, c in enumerate(ex.contracts):
            prov = c.get("provider")
            for ii, it in enumerate(c.get("interactions", [])):
                method = (it.get("method") or "GET").upper()
                np_ = normalize_path(it.get("path", ""), strip_prefixes)
                cands = [e for e in endpoints_by_service.get(prov, []) if e.method == method and e.norm_path == np_]
                if len(cands) != 1:
                    unresolved.append({"id": f"{ex.repo}:{c['_file']}#{ii}", "repo": ex.repo, "file": c["_file"],
                                       "line": 0, "reason": f"contract interaction {method} {np_} "
                                       f"not found in provider '{prov}'"})
                    continue
                ep = cands[0]
                # corroborate an existing code-level edge from the same consumer repo
                hit = next((e for cs in ex.callsites if (e := resolved_ids.get(cs.id)) and e.dst == ep.id), None)
                if hit:
                    hit.strategy = f"{hit.strategy}+R3"
                    hit.status = CORROBORATED
                    hit.evidence = {**hit.evidence, "contract_file": c["_file"]}
                    continue
                nid = f"{ex.repo}:{c['_file']}#{ii}"
                synthetic[nid] = {"type": "contract_ref", "repo": ex.repo, "service": ex.service,
                                  "file": c["_file"], "line": 0}
                edges.append(Edge(nid, "CONSUMES_API", ep.id, CONTRACT_CONFIRMED, "R3", None,
                                  {"repo": ex.repo, "file": c["_file"], "line": 0}))
                by_strategy["R3"] += 1
                total += 1
    for e in edges:
        if e.src in resolved_ids and resolved_ids[e.src] is e:
            for s in (e.strategy or "").split("+"):
                if s != "R3":
                    by_strategy[s] += 1
    resolved = sum(1 for _ in resolved_ids) + sum(1 for n in synthetic)
    stats = {"outbound_call_sites": total, "outbound_call_sites_resolved": resolved,
             "resolution_rate": round(resolved / total, 3) if total else None,
             "resolution_by_strategy": dict(sorted(by_strategy.items()))}
    unresolved.sort(key=lambda u: (u["repo"], u["file"], u["line"]))
    return edges, unresolved, stats, synthetic
