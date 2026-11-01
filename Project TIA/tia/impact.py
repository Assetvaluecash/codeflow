"""Change-impact engine.

Reads only from TIA's own extracts/graph (pinned to an evidence snapshot). Never calls
external systems during traversal, never writes the graph, never invents consumers.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
import dataclasses
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import CONFIG_VERSION, EXTRACTOR_VERSION, EXTRACTOR_VERSIONS
from . import gitutil
from .contracts import NOT_EVALUATED, diff_contracts
from .data import data_summary, database_declared, database_of, diff_data
from .extraction import extract_commit
from .graph import Graph
from .graphstore import get_or_build
from .models import (CONFIRMED, COMPLETE, COMPLETE_WITH_GAPS, CONTRACT_CONFIRMED, CORROBORATED, FAILED,
                     INFERRED, PARTIAL, SEVERITY_ORDER, SOURCE_CONFIRMED, STALE, RepoExtract)
from .resolve import merged_service_map
from .severity import apply_override, compute, compute_data, is_material
from . import telemetry, tracing
from .scrub import scrub_json
from .tenantconfig import DEFAULT, TenantConfig, diff as config_diff
from .store import Store


@dataclass
class AnalysisRequest:
    repos: dict                      # repo name -> local path
    target: str
    base: str
    head: str
    revisions: dict = field(default_factory=dict)   # other repo -> rev (default HEAD)
    pull_request: Optional[int] = None
    change_record: Optional[str] = None
    max_depth: int = 8
    max_results: int = 200
    time_budget_s: float = 60.0
    test_naming: bool = True
    strip_prefixes: tuple = ()
    freshness: dict = field(default_factory=dict)             # repo -> scan state at analysis time (FR-005)
    config: Optional[TenantConfig] = None                     # the tenant's configuration the analysis runs under (None = defaults)
    stale_relationships: list = field(default_factory=list)   # unverifiable stale relationships (relationships.UNVERIFIABLE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract(repo: str, path: str, rev: str, store: Optional[Store], strip: tuple) -> RepoExtract:
    if store and len(rev) == 40 and all(c in "0123456789abcdef" for c in rev):
        cached = store.get_extract(repo, rev, EXTRACTOR_VERSION, strip)         # an extract exists only for a commit git already confirmed
        if cached:
            return cached
    commit = gitutil.resolve_rev(path, rev)
    if store:
        cached = store.get_extract(repo, commit, EXTRACTOR_VERSION, strip)
        if cached:
            return cached
    ex, _ = extract_commit(repo, path, commit, store, strip)
    if store:
        store.put_extract(ex)
    return ex


def _path_status(edges, is_stale=lambda e: False) -> str:
    if any(is_stale(e) for e in edges):
        return STALE
    sts = [e.status for e in edges]
    if INFERRED in sts:
        return INFERRED
    if CORROBORATED in sts:
        return CORROBORATED
    if CONTRACT_CONFIRMED in sts:
        return CONTRACT_CONFIRMED
    return SOURCE_CONFIRMED


def _failed(req: AnalysisRequest, reason: str, store) -> dict:
    rep = {"analysis_id": f"tia-failed-{hashlib.sha1((req.target + req.base + req.head + reason).encode()).hexdigest()[:10]}",
           "status": FAILED, "failure_reason": reason,
           "change": {"repository": req.target, "pull_request": req.pull_request,
                      "base_revision": req.base, "proposed_revision": req.head, "changed_components": []},
           "findings": [], "unknowns": [{"area": "analysis", "reason": reason, "affects_confidence": True}],
           "policy_results": [], "generated_at": _now(), "configuration_version": CONFIG_VERSION,
           "extractor_versions": dict(EXTRACTOR_VERSIONS)}
    if store:
        store.put_analysis(rep)
        store.audit("analysis_failed", target=req.target, reason=reason)
    return rep


def _without_files(ex: RepoExtract, files: set) -> RepoExtract:
    """The extract with every declaration that lives in one of `files` left out (used where the file could not be read at the other revision)."""
    if not files:
        return ex
    keep = lambda xs: [x for x in xs if getattr(x, "file", None) not in files]
    # a schema is "used" through the endpoints that return or accept it; when those endpoints are unreadable, whether the schema is still
    # used (and so whether a change to it is breaking) cannot be told either
    gone = [e for e in ex.endpoints if e.file in files]
    unreadable_use = {e.response_schema for e in gone if e.response_schema} | {n for e in gone for n in e.request_schemas}
    return dataclasses.replace(ex, endpoints=keep(ex.endpoints), schemas=[x for x in keep(ex.schemas) if x.name not in unreadable_use],
                               functions=keep(ex.functions), tests=keep(ex.tests), entities=keep(ex.entities), migrations=keep(ex.migrations))


def analyze(req: AnalysisRequest, store: Optional[Store] = None, persist: bool = True) -> dict:
    """Run an analysis inside a trace span and record what it produced (status, findings, unknowns, resolution) as metrics."""
    t0 = time.monotonic()
    with tracing.span("analysis.run", target=req.target, repos=len(req.repos)) as sp:
        report = _analyze(req, store, persist)
        sp.set(status=report["status"], findings=len(report.get("findings", [])), analysis_id=report["analysis_id"])
    telemetry.record_analysis(report, time.monotonic() - t0)
    return report


def _analyze(req: AnalysisRequest, store: Optional[Store] = None, persist: bool = True) -> dict:
    """persist=False runs the analysis (still using the store's caches) without recording it: used to reproduce a result."""
    t0 = time.monotonic()
    rec = store if persist else None
    cfg = req.config or DEFAULT
    if rec:
        rec.audit("analysis_requested", target=req.target, base=req.base, head=req.head,
                    repos=sorted(req.repos), pull_request=req.pull_request)

    # ---- 1. resolve revisions, extract (reusing cached evidence) ------------------------------------
    if req.target not in req.repos:
        return _failed(req, f"target repository '{req.target}' is not registered", rec)
    tpath = req.repos[req.target]
    try:
        base_sha = gitutil.resolve_rev(tpath, req.base)
        head_sha = gitutil.resolve_rev(tpath, req.head)
        base_ex = _extract(req.target, tpath, base_sha, store, req.strip_prefixes)
        head_ex = _extract(req.target, tpath, head_sha, store, req.strip_prefixes)
        changed_files = gitutil.changed_files(tpath, base_sha, head_sha)
    except gitutil.GitError as e:
        return _failed(req, f"cannot read target revisions: {e}", rec)

    extracts: dict[str, RepoExtract] = {req.target: base_ex}
    repo_failures = []
    others = [(n, p) for n, p in sorted(req.repos.items()) if n != req.target]

    def one(item):
        name, path = item
        try:
            return name, _extract(name, path, req.revisions.get(name, "HEAD"), store, req.strip_prefixes), None
        except gitutil.GitError as e:
            return name, None, str(e)
    # every repository costs a git process to resolve its revision (tens of milliseconds each, mostly process start-up): do them side by side.
    # Results are consumed in the sorted order, so the report is exactly what the sequential loop produced.
    if len(others) > 4:
        with ThreadPoolExecutor(max_workers=min(16, len(others))) as pool:
            results = list(pool.map(one, others))
    else:
        results = [one(i) for i in others]
    for name, ex, err in results:
        if err is None:
            extracts[name] = ex
        else:
            repo_failures.append({"repo": name, "error": err})

    # ---- 2. change detection --------------------------------------------------------------------------
    # A file that parses at base but not at the proposed revision has not had its endpoints, schemas or functions REMOVED: they are
    # unreadable. Comparing them would report every one as deleted (and every consumer as broken), so they are left out of the
    # comparison and reported as an unknown instead.
    unparsed = {ff["file"] for ff in head_ex.files_failed}
    cmp_base = _without_files(base_ex, unparsed)
    unevaluated = {k: len(getattr(base_ex, k)) - len(getattr(cmp_base, k)) for k in ("endpoints", "schemas", "functions", "entities", "migrations")}
    unevaluated = {k: v for k, v in unevaluated.items() if v}
    contract_changes = diff_contracts(cmp_base, head_ex)
    data_changes, changed_migrations = diff_data(cmp_base, head_ex)
    base_fn = {f.id: f for f in cmp_base.functions}
    head_fn = {f.id: f for f in head_ex.functions}
    fn_changes = [(fid, "removed" if fid not in head_fn else "modified")
                  for fid, f in sorted(base_fn.items()) if fid not in head_fn or head_fn[fid].hash != f.hash]
    added_fns = sorted(set(head_fn) - set(base_fn))

    covered_files = {base_fn[i].file for i, _ in fn_changes} | {c["file"] for c in contract_changes}
    covered_files |= {head_fn[i].file for i in added_fns}
    covered_files |= {m.file for m in cmp_base.migrations + head_ex.migrations}      # migrations are analysed as data changes
    test_files = {t.file for t in cmp_base.tests} | {t.file for t in head_ex.tests}
    changed_components, seeds = [], []     # seeds: dict(node, kind, breaking, label)
    only_tests = True
    for status, path in changed_files:
        is_test = path in test_files or "/tests/" in "/" + path or path.rsplit("/", 1)[-1].startswith("test_")
        if not is_test:
            only_tests = False
    for c in contract_changes:
        changed_components.append({"type": "api_schema" if "schema" in c["kind"] or "field" in c["kind"] else "api_endpoint",
                                   "name": c["element"], "change": c["kind"], "breaking": c["breaking"],
                                   "detail": c["detail"], "file": c["file"], "line": c["line"]})
        for s in c["seeds"]:
            seeds.append({"node": s, "kind": "breaking" if c["breaking"] else "nonbreaking",
                          "breaking": c["breaking"], "label": c["detail"]})
    data_components = []
    for c in data_changes:
        comp = {"type": "db_column" if c["column"] else "db_table",
                "name": f"{c['table']}.{c['column']}" if c["column"] else c["table"], "change": c["kind"],
                "breaking": c["breaking"], "detail": c["detail"], "file": c["file"], "line": c["line"], "database": c["database"]}
        changed_components.append(comp)
        data_components.append((comp, c))
        for s in c["seeds"]:
            seeds.append({"node": s["node"], "kind": "breaking" if c["breaking"] else "nonbreaking", "breaking": c["breaking"],
                          "label": c["detail"], "access": s["access"],
                          "data": {"table": c["table"], "column": c["column"], "kind": c["kind"], "database": c["database"]}})
    for fid, how in fn_changes:
        changed_components.append({"type": "function", "name": fid, "change": how})
        seeds.append({"node": fid, "kind": "nonbreaking", "breaking": False, "label": f"{how}: {base_fn[fid].qualname}"})
    for fid in added_fns:
        changed_components.append({"type": "function", "name": fid, "change": "added"})
    file_level = []
    for status, path in changed_files:
        if path in covered_files:
            continue
        is_test = path in test_files or "/tests/" in "/" + path or path.rsplit("/", 1)[-1].startswith("test_")
        if is_test or path.endswith((".md", ".txt")):
            continue
        ctype = "config" if path.endswith((".yaml", ".yml", ".toml", ".cfg", ".json", ".ini")) else "file"
        changed_components.append({"type": ctype, "name": path, "change": "file_level"})
        file_level.append(path)
        for f in base_ex.functions:
            if f.file == path:
                seeds.append({"node": f.id, "kind": "unclassified", "breaking": False,
                              "label": f"file-level change in {path}"})
    if changed_files and only_tests:
        change_type = "test_only"
    elif any(c["type"].startswith("api") for c in changed_components):
        change_type = "api_change"
    elif data_changes:
        change_type = "data_change"
    elif file_level:
        change_type = "config_or_file_change"
    else:
        change_type = "implementation_change"

    # ---- 3. graph + evidence snapshot ---------------------------------------------------------------------
    g, graph_version, _ = get_or_build(store, extracts, req.strip_prefixes, req.test_naming)
    telemetry.record_graph(g)
    smap = merged_service_map(extracts)
    smap["criticality"] = {**smap["criticality"], **cfg.criticality}      # the tenant's registry wins over a service's own file
    stale_by_key = {r["key"]: r for r in req.stale_relationships}

    def edge_key(e):
        src = g.nodes.get(e.src)
        return f"{src['repo']}|{src.get('file')}|{e.dst}" if src else None

    def is_stale(e):
        return e.rel == "CONSUMES_API" and edge_key(e) in stale_by_key

    snap_src = sorted((r, e.commit, e.extractor_version) for r, e in extracts.items()) + [("head", req.target, head_sha)]
    snap_src += [("graph", graph_version),
                 ("stale", sorted((r["key"], r["stale_reason"], r.get("stale_since")) for r in req.stale_relationships)),
                 ("freshness", sorted((r, sorted(f.items())) for r, f in req.freshness.items() if r in extracts))]
    snapshot_id = "snap-" + hashlib.sha256(repr((snap_src, cfg.ident(CONFIG_VERSION))).encode()).hexdigest()[:12]
    seeds = [s for s in seeds if s["node"] in g.nodes]

    # ---- 4. bounded traversal (confirmed edges first, then inferred) -----------------------------------------
    reached: dict[str, dict] = {}
    truncated, trunc_reason = False, None

    def bfs(allow_inferred: bool):
        nonlocal truncated, trunc_reason
        for si, s in enumerate(seeds):
            visited = {s["node"]}
            dq = deque([(s["node"], [s["node"]], [])])
            while dq:
                node, path, edges = dq.popleft()
                if len(edges) >= req.max_depth:
                    if g.impacted_by(node):
                        truncated, trunc_reason = True, f"traversal depth limit ({req.max_depth}) reached"
                    continue
                if g.nodes[node]["type"] == "test":
                    continue
                acc = s.get("access")           # a seed that only breaks writers (new required column) follows WRITES, not READS
                for nxt, e in g.impacted_by(node):
                    if nxt in visited or (e.status == INFERRED and not allow_inferred):
                        continue
                    if acc and e.rel in ("READS", "WRITES") and e.rel != ("WRITES" if acc == "write" else "READS"):
                        continue
                    if time.monotonic() - t0 > req.time_budget_s:
                        truncated, trunc_reason = True, "analysis time budget exceeded"
                        return
                    visited.add(nxt)
                    p, ed = path + [nxt], edges + [e]
                    if nxt not in reached:
                        if len(reached) >= req.max_results:
                            truncated, trunc_reason = True, f"result limit ({req.max_results}) reached"
                            return
                        reached[nxt] = {"path": p, "edges": ed, "seeds": [si]}
                    elif si not in reached[nxt]["seeds"]:
                        reached[nxt]["seeds"].append(si)
                    dq.append((nxt, p, ed))

    bfs(False)
    if not truncated:
        bfs(True)

    # ---- 5. findings -----------------------------------------------------------------------------------------
    target_service = base_ex.service
    tests_reached = {n: r for n, r in reached.items() if g.nodes[n]["type"] == "test"}
    findings = []
    head_entities = {e.id: e for e in head_ex.entities}

    def tests_for(node):
        out = []
        for tn, tr in tests_reached.items():
            if node in tr["path"][:-1] and tr["edges"][-1].rel == "TESTS":
                te = tr["edges"][-1]
                out.append({"test": tn, "name": g.nodes[tn]["name"], "file": g.nodes[tn]["file"],
                            "repo": g.nodes[tn]["repo"], "association": te.strategy,
                            "evidence_status": te.status,
                            **({"inference_rule": te.rule} if te.rule else {})})
        return sorted(out, key=lambda t: t["test"])

    def mapped_columns(ent, table):
        cols = set(ent.columns) if ent.table == table else set()
        for jt in ent.join_tables:
            if jt["table"] == table:
                cols |= set(jt["columns"])
        return cols

    def fixed_in_head(seed, ent_id):
        """True when the head revision no longer maps what the migration removes (the entity is updated in the same change)."""
        d, he = seed.get("data") or {}, head_entities.get(ent_id)
        if he is None:
            return True
        t, c, k = d.get("table"), d.get("column"), d.get("kind")
        tables = {he.table} | {jt["table"] for jt in he.join_tables}
        if k in ("table_dropped", "table_renamed"):
            return t not in tables
        if k in ("column_dropped", "column_renamed"):
            return c not in mapped_columns(he, t)
        if k == "column_added_required":
            return c in mapped_columns(he, t)
        return False

    def entity_finding(node, r):
        nd = g.nodes[node]
        sds = [seeds[i] for i in r["seeds"]]
        kinds = {s["kind"] for s in sds}
        kind = "breaking" if "breaking" in kinds else "nonbreaking" if "nonbreaking" in kinds else "unclassified"
        status = _path_status(r["edges"], is_stale)
        same_repo = nd["repo"] == req.target
        writers_only = all(s.get("access") == "write" for s in sds)
        accessors = []
        for fn, e in g.impacted_by(node):
            if e.rel in ("READS", "WRITES") and g.nodes[fn]["type"] == "function" and not (writers_only and e.rel == "READS"):
                accessors.append({"function": g.nodes[fn]["name"], "op": "read" if e.rel == "READS" else "write",
                                  "repo": nd["repo"], "file": g.nodes[fn].get("file"), "line": e.evidence.get("line")})
        accessors.sort(key=lambda a: (a["function"], a["op"]))
        if writers_only and not accessors:
            return None                  # a new required column only breaks services that insert; this one never writes
        fixed = same_repo and all(fixed_in_head(s, node) for s in sds)
        structural = any((s.get("data") or {}).get("kind") in ("table_dropped", "table_renamed", "column_dropped", "column_renamed")
                         for s in sds)
        crit = smap["criticality"].get(nd["service"])
        sev, rule = compute_data(kind, status, same_repo, fixed, structural, crit, cfg)
        ex = extracts[nd["repo"]]
        db, dsrc = nd["database"], (ex.service_map or {}).get("database_source")
        what = sorted({(f"column {d['table']}.{d['column']}" if d.get("column") and d["kind"] != "column_added_required"
                        else f"table {d['table']}") for d in (s.get("data") or {} for s in sds) if d})
        n_read = sum(1 for a in accessors if a["op"] == "read")
        n_write = sum(1 for a in accessors if a["op"] == "write")
        if accessors:
            uniq = sorted({a["function"] for a in accessors})
            names = ", ".join(uniq[:3]) + (", ..." if len(uniq) > 3 else "")
            acc_text = f"{n_read} analysed function(s) read it and {n_write} write it ({names})"
        else:
            acc_text = "no analysed function reads or writes it"
        label = "; ".join(sorted({s["label"] for s in sds}))
        where = f"{nd['repo']}:{nd.get('file')}:{nd.get('line')}"
        reason = (f"{nd['service']} maps {nd['name']} ({where}) to {', '.join(what)} in database {db}; {acc_text}. "
                  f"Change: {label}." + (" The entity mapping is updated in the same change." if fixed else ""))
        tests = tests_for(node)
        limitations = []
        if not same_repo:
            limitations.append(f"Linked to the changed table through database '{db}' "
                               f"({'declared in tia-services.yaml' if dsrc == 'declared' else 'named by spring.datasource.url'})")
        if any((s.get("data") or {}).get("kind") in ("column_type_changed", "column_made_required") for s in sds):
            limitations.append("Type and nullability compatibility of the mapping is not evaluated")
        if ex.data_unknowns:
            limitations.append(f"{nd['service']} also has data access that is not analysed (see unknowns)")
        if not same_repo and not tests:
            limitations.append("No relevant test evidence found in the analysed sources")
        if not smap["criticality"]:
            limitations.append("Service criticality not configured")
        verification = [f"Review the mapping of {nd['name']} ({where}) against the migration"]
        if tests:
            verification.append("Run: " + ", ".join(sorted({f"{t['name']} ({t['file']})" for t in tests})))
        elif not same_repo:
            verification.append(f"Identify and run data-access tests for {nd['service']} (none found in analysed sources)")
        if not same_repo:
            verification.append("Verify the migration against the deployed version of this service")
        return {
            "component": nd["service"], "repository": nd["repo"], "node": node, "node_type": "entity",
            "impact_type": "data", "severity": sev, "severity_rule": rule,
            "material": is_material(sev, kind == "breaking" and not fixed), "evidence_status": status,
            "resolution_strategy": r["edges"][-1].strategy, "reason": reason, "change_nature": kind, "depth": len(r["edges"]),
            "path": [{"node": n, "type": g.nodes[n]["type"], "repo": g.nodes[n]["repo"], "file": g.nodes[n].get("file"),
                      "line": g.nodes[n].get("line")} for n in r["path"]],
            "path_edges": [{"rel": e.rel, "status": e.status, "strategy": e.strategy} for e in r["edges"]],
            "evidence": r["edges"][-1].evidence,
            "data": {"entity": nd["name"], "table": nd["table"], "database": db, "database_source": dsrc,
                     "accessors": accessors[:20], "accessor_count": len(accessors),
                     "changes": sorted({s["label"] for s in sds})},
            "tests": tests, "limitations": limitations, "required_verification": verification}
    for node, r in reached.items():
        nd = g.nodes[node]
        typ = nd["type"]
        cross = nd["repo"] != req.target
        if typ == "entity":
            ef = entity_finding(node, r)
            if ef:
                findings.append(ef)
            continue
        if typ in ("callsite", "contract_ref"):
            seed_types = {g.nodes[seeds[i]["node"]]["type"] for i in r["seeds"] if seeds[i]["node"] in g.nodes}
            impact = ("contract" if seed_types & {"schema", "endpoint"} else
                      "transitive" if seed_types & {"table", "column"} else "direct")
        elif typ == "endpoint" and cross:
            impact = "transitive"
        elif typ == "function" and not cross:
            impact = "direct"
        else:
            continue
        sds = [seeds[i] for i in r["seeds"]]
        kinds = {s["kind"] for s in sds}
        kind = "breaking" if "breaking" in kinds else "nonbreaking" if "nonbreaking" in kinds else "unclassified"
        status = _path_status(r["edges"], is_stale)
        comp_service = nd["service"]
        crit = smap["criticality"].get(comp_service)
        sev, rule = compute(kind, status, cross, crit, cfg)
        cons = next((e for e in r["edges"] if e.rel == "CONSUMES_API"), None)
        tests = tests_for(node)
        limitations = []
        if status == INFERRED:
            limitations.append("Relationship is inferred (" + ", ".join(sorted({e.rule for e in r["edges"] if e.rule}))
                               + "); confirm the provider mapping before relying on it")
        if status == STALE:
            srec = next(stale_by_key[edge_key(e)] for e in r["edges"] if is_stale(e))
            limitations.append(f"Stale relationship ({srec['stale_reason']}): {srec['stale_detail']}. It cannot be verified "
                               "against the consumer's latest source; confirm before relying on it")
        if kind == "unclassified":
            limitations.append("File-level analysis: the change could not be classified at symbol level")
        if cross and not tests:
            limitations.append("No relevant test evidence found in the analysed sources")
        if not smap["criticality"]:
            limitations.append("Service criticality not configured")
        label = "; ".join(sorted({s["label"] for s in sds}))
        ev0 = cons.evidence if cons else {"repo": nd["repo"], "file": nd.get("file"), "line": nd.get("line")}
        where = f"{nd['repo']}:{nd.get('file')}:{nd.get('line')}"
        if typ in ("callsite", "contract_ref"):
            how = {"R1": "through a generated client", "R2": "through a fully resolved HTTP call",
                   "R3": "through a declared contract", "R5": "through a unique path match"}
            strat = (cons.strategy or "").split("+")[0] if cons else ""
            reason = (f"{comp_service} depends on the changed provider {how.get(strat, 'via a resolved call')} at {where}. "
                      f"Change: {label}." + (" The relationship is stale: it could not be verified against the latest source."
                                              if status == STALE else ""))
        elif typ == "endpoint":
            reason = f"{comp_service} endpoint {nd['method']} {nd['path']} depends on the changed component transitively ({where}). Change: {label}."
        else:
            reason = f"{nd.get('name')} in {where} calls or depends on the changed component. Change: {label}."
        verification = []
        if tests:
            verification.append("Run: " + ", ".join(sorted({f"{t['name']} ({t['file']})" for t in tests})))
        elif cross:
            verification.append(f"Identify and run consumer tests for {comp_service} (none found in analysed sources)")
        if cross:
            verification.append("Verify compatibility with the deployed consumer version")
        findings.append({
            "component": comp_service, "repository": nd["repo"], "node": node, "node_type": typ,
            "impact_type": impact, "severity": sev, "severity_rule": rule,
            "material": is_material(sev, kind == "breaking"),
            "evidence_status": status,
            **({"resolution_strategy": cons.strategy} if cons else {}),
            **({"inference_rule": sorted({e.rule for e in r["edges"] if e.rule})[0]} if status == INFERRED else {}),
            "reason": reason, "change_nature": kind, "depth": len(r["edges"]),
            "path": [{"node": n, "type": g.nodes[n]["type"], "repo": g.nodes[n]["repo"],
                      "file": g.nodes[n].get("file"), "line": g.nodes[n].get("line")} for n in r["path"]],
            "path_edges": [{"rel": e.rel, "status": e.status, "strategy": e.strategy} for e in r["edges"]],
            "evidence": ev0, "tests": tests, "limitations": limitations, "required_verification": verification})
    # stale relationships whose evidence can no longer be read (e.g. the consumer file no longer parses) are not in the
    # graph at all, but the consumer may still depend on the changed provider: report them as stale, not as absent
    affected_eps = {n for n, r in reached.items() if g.nodes[n]["type"] == "endpoint" and g.nodes[n]["repo"] == req.target}
    affected_eps |= {s["node"] for s in seeds if g.nodes[s["node"]]["type"] == "endpoint"}
    covered_keys = {edge_key(e) for r in reached.values() for e in r["edges"] if e.rel == "CONSUMES_API"}
    stale_on_change = []
    for srec in sorted(req.stale_relationships, key=lambda x: x["key"]):
        ep = srec["provider_endpoint"]
        if srec["provider_repo"] != req.target or srec["consumer_repo"] == req.target or ep not in affected_eps:
            continue
        stale_on_change.append(srec)
        if srec["key"] in covered_keys:
            continue
        sds = [seeds[i] for i in reached[ep]["seeds"]] if ep in reached else [s for s in seeds if s["node"] == ep]
        kinds = {s["kind"] for s in sds}
        kind = "breaking" if "breaking" in kinds else "nonbreaking" if "nonbreaking" in kinds else "unclassified"
        sev, rule = compute(kind, STALE, True, smap["criticality"].get(srec["consumer_service"]), cfg)
        label = "; ".join(sorted({s["label"] for s in sds}))
        where = f"{srec['consumer_repo']}:{srec['consumer_file']}:{srec['consumer_line']}"
        findings.append({
            "component": srec["consumer_service"], "repository": srec["consumer_repo"], "node": srec["key"],
            "node_type": "callsite", "impact_type": "contract", "severity": sev, "severity_rule": rule,
            "material": is_material(sev, kind == "breaking"), "evidence_status": STALE,
            "resolution_strategy": srec["strategy"],
            "reason": (f"{srec['consumer_service']} previously depended on {srec['method']} {srec['path']} ({where}), but that "
                       f"relationship is stale and could not be re-verified. Change: {label}."),
            "change_nature": kind, "depth": 1,
            "path": [{"node": ep, "type": "endpoint", "repo": req.target, "file": g.nodes[ep].get("file"), "line": g.nodes[ep].get("line")},
                     {"node": srec["key"], "type": "callsite", "repo": srec["consumer_repo"], "file": srec["consumer_file"],
                      "line": srec["consumer_line"]}],
            "path_edges": [{"rel": "CONSUMES_API", "status": STALE, "strategy": srec["strategy"]}],
            "evidence": {"repo": srec["consumer_repo"], "file": srec["consumer_file"], "line": srec["consumer_line"],
                         "commit": srec.get("last_confirmed_consumer_commit")},
            "tests": [],
            "limitations": [f"Stale relationship ({srec['stale_reason']}): {srec['stale_detail']}. It cannot be verified "
                            "against the consumer's latest source; confirm before relying on it"],
            "required_verification": [f"Re-scan {srec['consumer_repo']} and confirm whether it still calls "
                                      f"{srec['method']} {srec['path']}",
                                      "Verify compatibility with the deployed consumer version"]})
    # the tenant's policy, applied once to the finished findings: severity overrides (each records the default it replaced) and the
    # materiality thresholds. Under the default policy this changes nothing.
    for f in findings:
        sev, ov = apply_override(f["severity"], f["severity_rule"], cfg)
        if ov:
            f["severity"], f["severity_override"] = sev, ov
            f["limitations"] = list(f.get("limitations", [])) + [f"Severity set by tenant configuration {cfg.id} ({ov['default']} under the default rules)"]
        f["material"] = is_material(f["severity"], f["change_nature"] == "breaking" and f["severity_rule"] != "SEV-DATA-UPDATED", cfg)
    findings.sort(key=lambda f: (-SEVERITY_ORDER.index(f["severity"]), f["depth"], f["component"], f["node"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"

    # ---- 6. unknowns, coverage, status ----------------------------------------------------------------------------
    unknowns = []
    if base_sha == head_sha:
        unknowns.append({"area": "empty_change", "affects_confidence": True,
                         "reason": "base and proposed revisions are the same commit, so there is no change to analyse; "
                                   "check the base branch (for a pull request, the default branch may not be its target)"})
    if g.unresolved:
        unknowns.append({"area": "unresolved_outbound_calls", "count": len(g.unresolved), "affects_confidence": True,
                         "reason": f"{len(g.unresolved)} outbound call site(s)/contract interaction(s) could not be resolved to a provider.",
                         "items": g.unresolved})
    contract_or_endpoint_involved = bool(contract_changes) or any(
        g.nodes[n]["type"] == "endpoint" and g.nodes[n]["repo"] == req.target for n in reached) or any(
        g.nodes[s["node"]]["type"] == "function" and any(
            e.rel == "HANDLES" for e in [x for _, x in g.impacted_by(s["node"])]) for s in seeds)
    if contract_or_endpoint_involved:
        unknowns.append({"area": "event_consumers", "affects_confidence": True,
                         "reason": "Event consumers are not analysed in this release."})
        unknowns.append({"area": "external_consumers", "affects_confidence": True,
                         "reason": f"Consumers outside the {len(extracts)} analysed repositories cannot be seen; "
                                   "no external consumer inventory is connected."})
        unknowns.append({"area": "compatibility_not_evaluated", "affects_confidence": False,
                         "reason": "Compatibility was not evaluated for: " + ", ".join(NOT_EVALUATED) + "."})
    if not smap["criticality"]:
        unknowns.append({"area": "service_criticality", "affects_confidence": False,
                         "reason": "Service criticality is not configured; all services are treated as non-critical."})
    if g.ambiguous_calls:
        unknowns.append({"area": "ambiguous_intra_repo_calls", "count": g.ambiguous_calls, "affects_confidence": True,
                         "reason": f"{g.ambiguous_calls} intra-repository call(s) matched more than one function and were not linked."})
    if data_changes:
        mapped_by: dict = {}
        for s in seeds:
            if g.nodes[s["node"]]["type"] in ("table", "column"):
                mapped_by.setdefault(s["node"], sorted({n for n, e in g.impacted_by(s["node"]) if e.rel in ("MAPS_TO", "MAPS_COLUMN")}))
        unmapped = []
        for comp, c in data_components:
            ents = sorted({n for s in c["seeds"] for n in mapped_by.get(s["node"], [])})
            if c["seeds"]:
                comp["mapped_by"] = [{"entity": n, "repo": g.nodes[n]["repo"], "service": g.nodes[n]["service"]} for n in ents]
                if not ents:
                    unmapped.append({"repo": req.target, "file": c["file"], "line": c["line"], "detail": c["detail"]})
        if unmapped:
            unknowns.append({"area": "data_change_not_mapped", "affects_confidence": False, "count": len(unmapped),
                             "reason": "No analysed ORM mapping uses the changed table/column, so no consumer was found; services "
                                       "that reach it through raw SQL or an unanalysed repository would not appear.",
                             "items": unmapped})
        data_unk = [{"repo": r, **u} for r, e in sorted(extracts.items()) for u in e.data_unknowns]
        if data_unk:
            unknowns.append({"area": "data_access_not_analysed", "affects_confidence": True, "count": len(data_unk),
                             "reason": f"{len(data_unk)} data access declaration(s) could not be analysed (raw SQL, EntityManager/"
                                       "JDBC, repository methods or table names that cannot be resolved), so data impact may be "
                                       "understated.", "items": data_unk[:100]})
        no_db = [{"repo": r, "service": e.service} for r, e in sorted(extracts.items()) if e.entities and not database_declared(e)]
        if no_db:
            unknowns.append({"area": "database_identity", "affects_confidence": True, "count": len(no_db),
                             "reason": f"{len(no_db)} service(s) map tables without a declared database (database: in "
                                       "tia-services.yaml or a literal spring.datasource.url); each is assumed to use its own "
                                       "database, so none is linked to the changed table unless it is the changed repository itself.",
                             "items": no_db})
    bad_mig = [{"repo": req.target, "file": m.file, **u} for m in changed_migrations for u in m.unparsed]
    if bad_mig:
        unknowns.append({"area": "unparsed_migration_statements", "affects_confidence": True, "count": len(bad_mig),
                         "reason": f"{len(bad_mig)} statement(s) in the changed migrations could not be analysed, so their "
                                   "effect on the schema is unknown.", "items": bad_mig})
    overridden = [f for f in findings if f.get("severity_override")]
    if overridden:
        by_rule: dict = {}
        for f in overridden:
            by_rule[f["severity_override"]["rule"]] = by_rule.get(f["severity_override"]["rule"], 0) + 1
        unknowns.append({"area": "severity_overridden", "affects_confidence": False, "count": len(overridden),
                         "reason": f"{len(overridden)} finding(s) have a severity set by this tenant's configuration ({cfg.id}) instead of the "
                                   "default rules; each records the default it replaced.",
                         "items": [{"rule": r, "count": n} for r, n in sorted(by_rule.items())]})
    if file_level:
        unknowns.append({"area": "file_level_analysis", "affects_confidence": True,
                         "reason": "Symbol-level comparison unavailable for: " + ", ".join(file_level) + " (file-level analysis)."})
    unsupported = {r: e.non_python_files for r, e in extracts.items() if e.non_python_files}
    if unsupported:
        unknowns.append({"area": "unsupported_language_files", "affects_confidence": True,
                         "reason": "Files in unsupported languages were not analysed.", "files_by_repo": unsupported})
    http_libs = [{"repo": r, **x} for r, e in sorted(extracts.items()) for x in e.unsupported_clients]
    if http_libs:
        libs = sorted({x["library"] for x in http_libs})
        unknowns.append({"area": "unsupported_http_clients", "affects_confidence": True, "count": len(http_libs),
                         "reason": "Outbound calls made through " + ", ".join(libs) + " are not analysed, so consumers that "
                                   "use them cannot be seen.", "items": http_libs})
    if repo_failures:
        unknowns.append({"area": "inaccessible_repositories", "affects_confidence": True,
                         "reason": "Some repositories could not be read.", "items": repo_failures})
    parse_failures = [{"repo": r, **ff} for r, e in extracts.items() for ff in e.files_failed]
    # the proposed revision is only diffed (the graph uses base), so its parse failures must be reported too
    parse_failures += [{"repo": req.target, "revision": "proposed", **ff} for ff in head_ex.files_failed]
    if parse_failures:
        unknowns.append({"area": "parse_failures", "affects_confidence": True,
                         "reason": f"{len(parse_failures)} file(s) failed to parse.", "items": parse_failures})
    # context paths that a configuration repository sets for services (Spring Cloud Config shared/<service>.yml) are not applied automatically:
    # an analysis must not depend on which other repositories happened to be scanned. Say so, with the setting that would apply them.
    ext = {}
    for e in extracts.values():
        ext.update(e.external_context_paths or {})
    services = {e.service: e for e in extracts.values()}
    missing_cp = sorted({cp for svc, cp in ext.items() if svc in services and not services[svc].context_path
                         and cp not in tuple(req.strip_prefixes)})
    if missing_cp:
        unknowns.append({"area": "context_paths_not_applied", "affects_confidence": True,
                         "reason": "A configuration repository sets servlet context paths for some services (" + ", ".join(missing_cp) + "). Calls that go through "
                                   "them are matched only if the tenant configuration lists them in paths.strip_prefixes.",
                         "items": [{"service": s_, "context_path": cp} for s_, cp in sorted(ext.items()) if cp in missing_cp]})
    if unevaluated:
        unknowns.append({"area": "changes_not_evaluated", "affects_confidence": True,
                         "reason": "Declarations in file(s) that do not parse at the proposed revision could not be compared with the base ("
                                   + ", ".join(f"{n} {k}" for k, n in sorted(unevaluated.items())) + "): they are NOT reported as removed, "
                                   "and whether they changed is unknown.",
                         "items": [{"repo": req.target, "file": f} for f in sorted(unparsed)]})
    if truncated:
        unknowns.append({"area": "truncated_traversal", "affects_confidence": True, "reason": trunc_reason})
    stale_scans = [{"repo": r, **req.freshness[r]} for r in sorted(extracts)
                   if req.freshness.get(r, {}).get("scan_status") == "failed"]
    if stale_scans:
        parts = [f"'{x['repo']}': the latest scan failed ({(x.get('scan_error') or 'unknown error')[:120]}); analysed its last "
                 f"successful scan ({x.get('last_successful_scan') or 'unknown time'}) at revision {extracts[x['repo']].commit[:7]}"
                 for x in stale_scans]
        unknowns.append({"area": "repository_freshness", "affects_confidence": True,
                         "reason": "Analysis used stale data: " + "; ".join(parts) + ".", "items": stale_scans})
    if stale_on_change:
        unknowns.append({"area": "stale_relationships", "affects_confidence": True, "count": len(stale_on_change),
                         "reason": f"{len(stale_on_change)} previously confirmed consumer relationship(s) on the changed "
                                   "provider could not be re-verified and are reported as stale.",
                         "items": [{"repo": x["consumer_repo"], "file": x["consumer_file"], "stale_reason": x["stale_reason"],
                                    "detail": x["stale_detail"]} for x in stale_on_change]})

    if truncated or repo_failures or parse_failures or stale_scans:
        status = PARTIAL
    elif any(u["affects_confidence"] for u in unknowns):
        status = COMPLETE_WITH_GAPS
    else:
        status = COMPLETE

    material = [f for f in findings if f["material"]]
    summary = {"findings": len(findings), "material_findings": len(material),
               "by_severity": {s: sum(1 for f in findings if f["severity"] == s) for s in reversed(SEVERITY_ORDER)},
               "by_evidence_status": {}}
    for f in findings:
        summary["by_evidence_status"][f["evidence_status"]] = summary["by_evidence_status"].get(f["evidence_status"], 0) + 1
    if not findings:
        summary["message"] = ("No downstream impact was identified within the analysed scope (see coverage and unknowns); "
                              "this is not proof that no impact exists.")
    if change_type == "test_only":
        summary["message"] = "Test-only change; no production components affected within the analysed scope."

    aid = (f"tia-pr-{req.pull_request}-{head_sha[:7]}" if req.pull_request is not None
           else "tia-" + hashlib.sha256(f"{req.target}|{base_sha}|{head_sha}|{snapshot_id}".encode()).hexdigest()[:12])
    report = {
        "analysis_id": aid, "status": status, "summary": summary,
        "change": {"repository": req.target, "service": target_service, "pull_request": req.pull_request,
                   "change_record": req.change_record, "base_revision": base_sha, "proposed_revision": head_sha,
                   "change_type": change_type, "changed_components": changed_components,
                   "tests_in_changed_repository": [
                       {"test": n, "name": g.nodes[n]["name"], "association": r["edges"][-1].strategy,
                        "evidence_status": r["edges"][-1].status}
                       for n, r in sorted(tests_reached.items()) if g.nodes[n]["repo"] == req.target]},
        "evidence_snapshot_id": snapshot_id,
        "coverage": {
            "repositories_analysed": [{"repo": r, "service": e.service, "commit": e.commit,
                                       "python_files_analysed": e.files_analysed, "files_failed": len(e.files_failed),
                                       "freshness": {**req.freshness.get(r, {"scan_status": "unknown"}),
                                                     "analysed_revision": e.commit}}
                                      for r, e in sorted(extracts.items())],
            "repositories_inaccessible": repo_failures,
            **g.resolution_stats,
            **({"data": ds} if (ds := data_summary(extracts)) else {}),
            "traversal": {"max_depth": req.max_depth, "max_results": req.max_results, "truncated": truncated, "reached": len(reached)},
            **({"configuration": {"id": cfg.id, "version": cfg.version,
                                  "settings_changed": [d["path"] for d in config_diff(DEFAULT.to_dict(), cfg.to_dict())][:50]}}
               if not cfg.is_default else {}),
            "criticality_configured": bool(smap["criticality"])},
        "findings": findings, "unknowns": unknowns, "policy_results": [],
        "generated_at": _now(), "configuration_version": cfg.ident(CONFIG_VERSION),
        "extractor_versions": dict(EXTRACTOR_VERSIONS)}
    # PL-011: nothing that looks like a credential may leave in a report, whatever the source code contained
    report, in_report = scrub_json(report)
    in_source = {r: e.secrets_withheld for r, e in sorted(extracts.items()) if e.secrets_withheld}
    if head_ex.secrets_withheld and head_ex.secrets_withheld > in_source.get(req.target, 0):
        in_source[req.target] = head_ex.secrets_withheld
    if in_report or in_source:
        report["unknowns"].append({
            "area": "secrets_withheld", "affects_confidence": False, "count": in_report + sum(in_source.values()),
            "reason": f"{in_report + sum(in_source.values())} secret-like value(s) found in the analysed source or evidence were withheld "
                      "from TIA's records and this report. Treat them as exposed and rotate them.",
            "items": [{"repo": r, "count": c} for r, c in in_source.items()]})
    if rec:
        rec.put_snapshot(snapshot_id, {
            "id": snapshot_id, "target": req.target, "target_base": base_sha, "target_head": head_sha,
            "revisions": {r: e.commit for r, e in sorted(extracts.items())},
            "extractor_versions": dict(EXTRACTOR_VERSIONS), "config_version": CONFIG_VERSION, "graph_id": graph_version,
            "tenant_config": {"version": cfg.version, "digest": cfg.digest, "settings": cfg.to_dict()},
            "strip_prefixes": list(req.strip_prefixes), "test_naming": req.test_naming,
            "max_depth": req.max_depth, "max_results": req.max_results,
            "freshness": {r: f for r, f in sorted(req.freshness.items()) if r in extracts},
            "stale_relationships": req.stale_relationships})
        rec.put_analysis(report)
        rec.audit("analysis_completed", analysis_id=aid, status=status, snapshot=snapshot_id, findings=len(findings))
    return report
