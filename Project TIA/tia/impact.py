"""Change-impact engine.

Reads only from TIA's own extracts/graph (pinned to an evidence snapshot). Never calls
external systems during traversal, never writes the graph, never invents consumers.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import CONFIG_VERSION, EXTRACTOR_VERSION
from . import gitutil
from .contracts import NOT_EVALUATED, diff_contracts
from .extract_python import extract_repo
from .graph import Graph, build_graph
from .models import (CONFIRMED, COMPLETE, COMPLETE_WITH_GAPS, CONTRACT_CONFIRMED, CORROBORATED, FAILED,
                     INFERRED, PARTIAL, SEVERITY_ORDER, SOURCE_CONFIRMED, RepoExtract)
from .resolve import merged_service_map
from .severity import compute, is_material
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract(repo: str, path: str, rev: str, store: Optional[Store], strip: tuple) -> RepoExtract:
    commit = gitutil.resolve_rev(path, rev)
    if store:
        cached = store.get_extract(repo, commit, EXTRACTOR_VERSION)
        if cached:
            return cached
    ex = extract_repo(repo, commit, gitutil.read_tree(path, commit), strip)
    if store:
        store.put_extract(ex)
    return ex


def _path_status(edges) -> str:
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
           "extractor_versions": {"python": EXTRACTOR_VERSION}}
    if store:
        store.put_analysis(rep)
        store.audit("analysis_failed", target=req.target, reason=reason)
    return rep


def analyze(req: AnalysisRequest, store: Optional[Store] = None) -> dict:
    t0 = time.monotonic()
    if store:
        store.audit("analysis_requested", target=req.target, base=req.base, head=req.head,
                    repos=sorted(req.repos), pull_request=req.pull_request)

    # ---- 1. resolve revisions, extract (reusing cached evidence) ------------------------------------
    if req.target not in req.repos:
        return _failed(req, f"target repository '{req.target}' is not registered", store)
    tpath = req.repos[req.target]
    try:
        base_sha = gitutil.resolve_rev(tpath, req.base)
        head_sha = gitutil.resolve_rev(tpath, req.head)
        base_ex = _extract(req.target, tpath, base_sha, store, req.strip_prefixes)
        head_ex = _extract(req.target, tpath, head_sha, store, req.strip_prefixes)
        changed_files = gitutil.changed_files(tpath, base_sha, head_sha)
    except gitutil.GitError as e:
        return _failed(req, f"cannot read target revisions: {e}", store)

    extracts: dict[str, RepoExtract] = {req.target: base_ex}
    repo_failures = []
    for name, path in sorted(req.repos.items()):
        if name == req.target:
            continue
        try:
            extracts[name] = _extract(name, path, req.revisions.get(name, "HEAD"), store, req.strip_prefixes)
        except gitutil.GitError as e:
            repo_failures.append({"repo": name, "error": str(e)})

    # ---- 2. change detection --------------------------------------------------------------------------
    contract_changes = diff_contracts(base_ex, head_ex)
    base_fn = {f.id: f for f in base_ex.functions}
    head_fn = {f.id: f for f in head_ex.functions}
    fn_changes = [(fid, "removed" if fid not in head_fn else "modified")
                  for fid, f in sorted(base_fn.items()) if fid not in head_fn or head_fn[fid].hash != f.hash]
    added_fns = sorted(set(head_fn) - set(base_fn))

    covered_files = {base_fn[i].file for i, _ in fn_changes} | {c["file"] for c in contract_changes}
    covered_files |= {head_fn[i].file for i in added_fns}
    test_files = {t.file for t in base_ex.tests} | {t.file for t in head_ex.tests}
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
    elif file_level:
        change_type = "config_or_file_change"
    else:
        change_type = "implementation_change"

    # ---- 3. graph + evidence snapshot ---------------------------------------------------------------------
    g = build_graph(extracts, req.strip_prefixes, req.test_naming)
    smap = merged_service_map(extracts)
    snap_src = sorted((r, e.commit, e.extractor_version) for r, e in extracts.items()) + [("head", req.target, head_sha)]
    snapshot_id = "snap-" + hashlib.sha256(repr((snap_src, CONFIG_VERSION)).encode()).hexdigest()[:12]
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
                for nxt, e in g.impacted_by(node):
                    if nxt in visited or (e.status == INFERRED and not allow_inferred):
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
    for node, r in reached.items():
        nd = g.nodes[node]
        typ = nd["type"]
        cross = nd["repo"] != req.target
        if typ in ("callsite", "contract_ref"):
            impact = "contract" if any(seeds[i]["node"] in g.nodes and g.nodes[seeds[i]["node"]]["type"] in ("schema", "endpoint")
                                       for i in r["seeds"]) else "direct"
        elif typ == "endpoint" and cross:
            impact = "transitive"
        elif typ == "function" and not cross:
            impact = "direct"
        else:
            continue
        sds = [seeds[i] for i in r["seeds"]]
        kinds = {s["kind"] for s in sds}
        kind = "breaking" if "breaking" in kinds else "nonbreaking" if "nonbreaking" in kinds else "unclassified"
        status = _path_status(r["edges"])
        comp_service = nd["service"]
        crit = smap["criticality"].get(comp_service)
        sev, rule = compute(kind, status, cross, crit)
        cons = next((e for e in r["edges"] if e.rel == "CONSUMES_API"), None)
        tests = []
        for tn, tr in tests_reached.items():
            if node in tr["path"][:-1] and tr["edges"][-1].rel == "TESTS":
                te = tr["edges"][-1]
                tests.append({"test": tn, "name": g.nodes[tn]["name"], "file": g.nodes[tn]["file"],
                              "repo": g.nodes[tn]["repo"], "association": te.strategy,
                              "evidence_status": te.status,
                              **({"inference_rule": te.rule} if te.rule else {})})
        tests.sort(key=lambda t: t["test"])
        limitations = []
        if status == INFERRED:
            limitations.append("Relationship is inferred (" + ", ".join(sorted({e.rule for e in r["edges"] if e.rule}))
                               + "); confirm the provider mapping before relying on it")
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
                      f"Change: {label}.")
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
    findings.sort(key=lambda f: (-SEVERITY_ORDER.index(f["severity"]), f["depth"], f["component"], f["node"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"

    # ---- 6. unknowns, coverage, status ----------------------------------------------------------------------------
    unknowns = []
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
    if file_level:
        unknowns.append({"area": "file_level_analysis", "affects_confidence": True,
                         "reason": "Symbol-level comparison unavailable for: " + ", ".join(file_level) + " (file-level analysis)."})
    unsupported = {r: e.non_python_files for r, e in extracts.items() if e.non_python_files}
    if unsupported:
        unknowns.append({"area": "unsupported_language_files", "affects_confidence": True,
                         "reason": "Files in unsupported languages were not analysed.", "files_by_repo": unsupported})
    if repo_failures:
        unknowns.append({"area": "inaccessible_repositories", "affects_confidence": True,
                         "reason": "Some repositories could not be read.", "items": repo_failures})
    parse_failures = [{"repo": r, **ff} for r, e in extracts.items() for ff in e.files_failed]
    # the proposed revision is only diffed (the graph uses base), so its parse failures must be reported too
    parse_failures += [{"repo": req.target, "revision": "proposed", **ff} for ff in head_ex.files_failed]
    if parse_failures:
        unknowns.append({"area": "parse_failures", "affects_confidence": True,
                         "reason": f"{len(parse_failures)} file(s) failed to parse.", "items": parse_failures})
    if truncated:
        unknowns.append({"area": "truncated_traversal", "affects_confidence": True, "reason": trunc_reason})

    if truncated or repo_failures or parse_failures:
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
                                       "python_files_analysed": e.files_analysed, "files_failed": len(e.files_failed)}
                                      for r, e in sorted(extracts.items())],
            "repositories_inaccessible": repo_failures,
            **g.resolution_stats,
            "traversal": {"max_depth": req.max_depth, "max_results": req.max_results, "truncated": truncated},
            "criticality_configured": bool(smap["criticality"])},
        "findings": findings, "unknowns": unknowns, "policy_results": [],
        "generated_at": _now(), "configuration_version": CONFIG_VERSION,
        "extractor_versions": {"python": EXTRACTOR_VERSION}}
    if store:
        store.put_analysis(report)
        store.audit("analysis_completed", analysis_id=aid, status=status, snapshot=snapshot_id,
                    findings=len(findings))
    return report
