"""Accuracy harness (UC1-25, PRD section 15): measures TIA against a CURATED reference landscape.

    python tools/accuracy.py reference/spring-petclinic-microservices --source C:\\tiaref\\spm
    python tools/accuracy.py reference/synthetic-landscape
    python tools/accuracy.py <reference> --report out.json --enforce

A reference (reference/<name>/reference.json) is the ground truth, written by reading the code and NOT by running TIA:
  services      repository name -> where its code lives (a sub-directory of --source, or built by a fixture)
  dependencies  the curated cross-service dependencies (consumer -> provider endpoint), each with the file:line that proves it
  endpoints     the provider endpoints that exist (to measure extraction recall)
  changes       proposed changes (a patch applied on a branch) with the components that MUST be reported as materially impacted
  unresolvable  outbound calls that cannot be resolved statically; reporting them as resolved to a provider would be a false edge

What is measured (PRD 15.3, each with its target) -
  recall of curated in-scope dependencies >= 90%          (graph level: does the CONSUMES_API edge exist?)
  precision of material findings >= 80%                    (a material finding is right only if its component is in the change's truth)
  material findings per PR: median <= 5, P90 <= 15
  cross-repository resolution rate >= 70% of outbound call sites
  material findings with traceable evidence >= 90%          (the evidence file exists at that revision and the line is inside it)
  silent pass for missing mandatory evidence = 0            (a change whose input is broken must not come back as plain "complete")
  material findings shown as source_confirmed without evidence = 0
Also reported: edge precision, endpoint extraction recall, and every metric split by language, relationship type, resolution strategy
and evidence source. The time-saving target (-30% assessment time) needs a user study and is reported as NOT MEASURED.

The source tree is third-party code and is only ever COPIED and PARSED here: nothing in it is executed, built or installed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGETS = {"recall": 0.90, "precision": 0.80, "median_material": 5, "p90_material": 15, "resolution": 0.70, "traceable": 0.90}
GIT = ["git", "-c", "user.name=tia-accuracy", "-c", "user.email=accuracy@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.longpaths=true"]
SKIP = shutil.ignore_patterns(".git", "target", "node_modules", "__pycache__", ".idea", "*.class", "*.jar")


def git(cwd, *args) -> str:
    r = subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def norm_path(p: str) -> str:
    """Compare endpoint paths as templates: '{anything}' and '*' segments are parameters, no trailing slash, always a leading slash."""
    parts = [("{}" if (s.startswith("{") and s.endswith("}")) or s == "*" else s) for s in p.split("?")[0].strip("/").split("/") if s != ""]
    return "/" + "/".join(parts)


# ------------------------------------------------------------------------------------------------------------------------------------
def materialize(ref: dict, source: Path | None, work: Path) -> dict:
    """Real git repositories for the reference: one per service, a base commit, and a branch per change."""
    out = {"repos": {}, "head": {}, "changes": {}}
    if ref["source"]["type"] == "fixture":
        from fixtures.build_fixture import build
        fx = build(str(work / "landscape"))
        out["repos"] = dict(fx["paths"])
        for n, p in out["repos"].items():
            out["head"][n] = git(p, "rev-parse", "HEAD")
        for c in ref["changes"]:
            out["changes"][c["id"]] = {"repo": c["target"], "base": fx["shas"][c["base"]], "head": fx["shas"][c["head"]]}
        return out
    if source is None or not source.is_dir():
        raise SystemExit(f"--source is required for '{ref['name']}' ({ref['source'].get('url')} at {ref['source'].get('commit')})")
    if ref["source"].get("commit"):
        have = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True).stdout.strip()
        if have != ref["source"]["commit"]:
            print(f"  note: --source is at {have[:10]}, the truth was curated at {ref['source']['commit'][:10]}; results may differ", file=sys.stderr)
    for name, sub in ref["services"].items():
        dest = work / "repos" / name
        shutil.copytree(source / sub, dest, ignore=SKIP)
        git(dest, "init", "-q", "-b", "main")
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", "base")
        out["repos"][name] = str(dest)
        out["head"][name] = git(dest, "rev-parse", "HEAD")
    for c in ref["changes"]:
        repo = out["repos"][c["target"]]
        base = out["head"][c["target"]]
        git(repo, "checkout", "-q", "-b", c["id"], base)
        for p in c["patch"]:
            f = Path(repo) / p["file"]
            text = f.read_text(encoding="utf-8")
            if p.get("append"):
                text += p["append"]
            else:
                if text.count(p["find"]) != 1:
                    raise SystemExit(f"change {c['id']}: the text to replace occurs {text.count(p['find'])} times in {p['file']} (must be exactly once); "
                                     "the reference no longer matches --source")
                text = text.replace(p["find"], p["replace"])
            f.write_text(text, encoding="utf-8", newline="")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", c["id"], "--allow-empty")
        out["changes"][c["id"]] = {"repo": c["target"], "base": base, "head": git(repo, "rev-parse", "HEAD")}
        git(repo, "checkout", "-q", "main")
    return out


# ------------------------------------------------------------------------------------------------------------------------------------
def strip_of(ref: dict, use_config: bool = True) -> tuple:
    return tuple((ref.get("config") or {}).get("strip_prefixes", [])) if use_config else ()


def observed_graph(ref: dict, mat: dict, use_config: bool = True):
    from tia.extraction import extract_commit
    from tia.graph import build_graph
    strip = strip_of(ref, use_config)
    extracts = {n: extract_commit(n, p, mat["head"][n], None, strip)[0] for n, p in mat["repos"].items()}
    return build_graph(extracts, strip), extracts


def observed_edges(g) -> list[dict]:
    out = []
    for e in g.edges:
        if e.rel != "CONSUMES_API":
            continue
        src, dst = g.nodes.get(e.src, {}), g.nodes.get(e.dst, {})
        out.append({"consumer": src.get("repo"), "provider": dst.get("repo"), "method": dst.get("method"), "path": norm_path(dst.get("path") or ""),
                    "strategy": e.strategy, "status": e.status, "file": src.get("file"), "line": src.get("line")})
    return out


def ratio(a: int, b: int):
    return None if b == 0 else a / b


def evidence_traceable(mat, ev: dict, revisions: dict) -> bool:
    """The evidence points at something real: the file exists at the analysed revision of that repository and, when it names a line, the
    line is inside the file and not blank. A whole-file artefact (a declared contract JSON) has no line: the file itself is the evidence."""
    from tia import gitutil
    repo, file, line = ev.get("repo"), ev.get("file"), ev.get("line")
    if not (repo in mat["repos"] and file):
        return False
    try:
        text = gitutil.read_file(mat["repos"][repo], revisions[repo], file)
    except Exception:
        return False
    if text is None:
        return False
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    if line in (0, None):
        return bool(text.strip())
    lines = text.split("\n")
    return isinstance(line, int) and 0 < line <= len(lines) and bool(lines[line - 1].strip())


def evaluate(ref: dict, mat: dict) -> dict:
    from tia.impact import AnalysisRequest, analyze
    g, extracts = observed_graph(ref, mat)
    if ref.get("config"):                                      # how much the result depends on the tenant configuration the reference states
        g0, _ = observed_graph(ref, mat, use_config=False)
        found0 = {(e["consumer"], e["provider"], e["method"], e["path"]) for e in observed_edges(g0)}
        without = sum(1 for d in ref["dependencies"] if d.get("in_scope", True)
                      and (d["consumer"], d["provider"], d["method"], norm_path(d["path"])) in found0)
    edges = observed_edges(g)
    res: dict = {"reference": ref["name"], "independent": ref.get("independent", False), "services": len(mat["repos"])}

    # ---- graph level: recall of curated dependencies, precision of edges, endpoint extraction ---------------------------------------
    deps = [d for d in ref["dependencies"] if d.get("in_scope", True)]
    found, missed, seg = [], [], defaultdict(lambda: [0, 0])
    for d in deps:
        hit = next((e for e in edges if e["consumer"] == d["consumer"] and e["provider"] == d["provider"] and e["method"] == d["method"]
                    and e["path"] == norm_path(d["path"])), None)
        (found if hit else missed).append({**d, **({"strategy": hit["strategy"], "status": hit["status"]} if hit else {})})
        for key in (("language", d.get("language", "?")), ("relationship", d.get("relationship", "rest")), ("difficulty", d.get("difficulty", "plain")),
                    ("strategy", hit["strategy"] if hit else "(missed)"), ("evidence", hit["status"] if hit else "(missed)")):
            seg[key][1] += 1
            seg[key][0] += bool(hit)
    truth_edges = {(d["consumer"], d["provider"], d["method"], norm_path(d["path"])) for d in ref["dependencies"] if d.get("in_scope", True)}
    truth_edges |= {(d["consumer"], d["provider"], d["method"], norm_path(d["path"])) for d in ref.get("acceptable_extra_edges", [])}
    seen = {(e["consumer"], e["provider"], e["method"], e["path"]) for e in edges}
    false_edges = sorted(seen - truth_edges)
    if ref.get("config"):
        res["configuration"] = {"settings": ref["config"], "dependencies_found_without_it": without, "of": len(deps)}
    res["recall"] = {"value": ratio(len(found), len(deps)), "found": len(found), "of": len(deps), "missed": missed,
                     "by": {f"{k}={v}": {"found": a, "of": b} for (k, v), (a, b) in sorted(seg.items())}}
    res["edge_precision"] = {"value": ratio(len(seen) - len(false_edges), len(seen)), "edges": len(seen), "false_edges": [list(x) for x in false_edges]}
    eps = {(n, ep.method, norm_path(ep.path)) for n, ex in extracts.items() for ep in ex.endpoints}
    truth_eps = {(e["service"], e["method"], norm_path(e["path"])) for e in ref.get("endpoints", [])}
    res["endpoint_recall"] = {"value": ratio(len(truth_eps & eps), len(truth_eps)), "found": len(truth_eps & eps), "of": len(truth_eps),
                              "missed": sorted(truth_eps - eps), "extra": len(eps - truth_eps)}
    unres = g.unresolved
    wrongly_resolved = [u for u in ref.get("unresolvable", []) if any(e["consumer"] == u["consumer"] and e["file"] and e["file"].endswith(u["file"]) and e["line"] == u["line"]
                                                                       for e in edges)]
    res["resolution"] = {"value": g.resolution_stats.get("resolution_rate"), "call_sites": g.resolution_stats.get("outbound_call_sites"),
                         "resolved": g.resolution_stats.get("outbound_call_sites_resolved"), "by_strategy": g.resolution_stats.get("resolution_by_strategy"),
                         "unresolved_reported": len(unres), "unresolvable_resolved_wrongly": wrongly_resolved}

    # ---- change level ----------------------------------------------------------------------------------------------------------------
    per_change, material_counts, tp = [], [], 0
    material_total = traceable = no_evidence_confirmed = 0
    silent = []
    exp_found = exp_total = 0
    fp_detail, by_status = [], defaultdict(lambda: [0, 0])
    for c in ref["changes"]:
        m = mat["changes"][c["id"]]
        t0 = time.perf_counter()
        rep = analyze(AnalysisRequest(repos=mat["repos"], target=m["repo"], base=m["base"], head=m["head"], strip_prefixes=strip_of(ref)), None)
        dt = time.perf_counter() - t0
        material = [f for f in rep["findings"] if f.get("material")]
        expected = {e["component"] for e in c.get("expected_material", [])}
        got = {f["component"] for f in material}
        good = [f for f in material if f["component"] in expected]
        tp += len(good)
        material_total += len(material)
        material_counts.append(len(material))
        exp_total += len(expected)
        exp_found += len(expected & got)
        for f in material:
            ok = evidence_traceable(mat, f.get("evidence") or {}, {**mat["head"], m["repo"]: m["head"]})
            traceable += ok
            by_status[f["evidence_status"]][0] += ok
            by_status[f["evidence_status"]][1] += 1
            if f["evidence_status"] in ("source_confirmed", "contract_confirmed", "corroborated") and not f.get("evidence"):
                no_evidence_confirmed += 1
            if f["component"] not in expected:
                fp_detail.append({"change": c["id"], "component": f["component"], "node_type": f["node_type"], "severity": f["severity"]})
        broken = c.get("expect_gaps")
        if broken and rep["status"] == "complete":
            silent.append({"change": c["id"], "status": rep["status"], "why": broken})
        per_change.append({"id": c["id"], "description": c["description"], "status": rep["status"], "material": len(material), "expected": sorted(expected),
                           "found_expected": sorted(expected & got), "missing": sorted(expected - got), "unexpected": sorted(got - expected),
                           "seconds": round(dt, 3), "unknowns": sorted({u["area"] for u in rep["unknowns"]})})
    res["changes"] = per_change
    res["precision"] = {"value": ratio(tp, material_total), "material": material_total, "correct": tp, "false_positives": fp_detail}
    res["change_recall"] = {"value": ratio(exp_found, exp_total), "found": exp_found, "of": exp_total}
    res["material_per_pr"] = {"median": statistics.median(material_counts) if material_counts else None,
                              "p90": sorted(material_counts)[min(len(material_counts) - 1, int(0.9 * len(material_counts)))] if material_counts else None,
                              "counts": material_counts}
    res["traceable_evidence"] = {"value": ratio(traceable, material_total), "of": material_total, "by_status": {k: {"traceable": a, "of": b} for k, (a, b) in by_status.items()}}
    res["silent_pass"] = {"value": len(silent), "cases": silent}
    res["confirmed_without_evidence"] = {"value": no_evidence_confirmed}
    res["analysis_seconds"] = {"median": round(statistics.median(x["seconds"] for x in per_change), 3), "max": max(x["seconds"] for x in per_change)} if per_change else None
    res["verdict"] = verdict(res)
    return res


def verdict(r: dict) -> dict:
    def ge(v, t):
        return None if v is None else v >= t
    v = {"recall": ge(r["recall"]["value"], TARGETS["recall"]), "precision": ge(r["precision"]["value"], TARGETS["precision"]),
         "material_per_pr": None if r["material_per_pr"]["median"] is None else r["material_per_pr"]["median"] <= TARGETS["median_material"] and r["material_per_pr"]["p90"] <= TARGETS["p90_material"],
         "resolution": ge(r["resolution"]["value"], TARGETS["resolution"]), "traceable_evidence": ge(r["traceable_evidence"]["value"], TARGETS["traceable"]),
         "silent_pass": r["silent_pass"]["value"] == 0, "confirmed_without_evidence": r["confirmed_without_evidence"]["value"] == 0}
    v["all_measured_targets_met"] = all(x for x in v.values() if x is not None)
    return v


def pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def markdown(r: dict) -> str:
    L = [f"# Accuracy: {r['reference']}" + ("" if r["independent"] else "  (curated by the product's author: NOT independent)"), ""]
    L += ["| Metric (PRD 15.3) | Target | Result | Met |", "|---|---|---|---|"]
    v = r["verdict"]
    mark = lambda x: "-" if x is None else "yes" if x else "**NO**"
    L += [f"| Recall of curated in-scope dependencies | >= 90% | {pct(r['recall']['value'])} ({r['recall']['found']}/{r['recall']['of']}) | {mark(v['recall'])} |",
          f"| Precision of material findings | >= 80% | {pct(r['precision']['value'])} ({r['precision']['correct']}/{r['precision']['material']}) | {mark(v['precision'])} |",
          f"| Material findings per PR | median <= 5, P90 <= 15 | median {r['material_per_pr']['median']}, P90 {r['material_per_pr']['p90']} | {mark(v['material_per_pr'])} |",
          f"| Cross-repository resolution rate | >= 70% | {pct(r['resolution']['value'])} ({r['resolution']['resolved']}/{r['resolution']['call_sites']}) | {mark(v['resolution'])} |",
          f"| Material findings with traceable evidence | >= 90% | {pct(r['traceable_evidence']['value'])} | {mark(v['traceable_evidence'])} |",
          f"| Silent pass for missing mandatory evidence | 0 | {r['silent_pass']['value']} | {mark(v['silent_pass'])} |",
          f"| source_confirmed without evidence | 0 | {r['confirmed_without_evidence']['value']} | {mark(v['confirmed_without_evidence'])} |",
          "| Reduction in median assessment time | >= 30% | NOT MEASURED (needs a user study) | - |", ""]
    L += [f"Also: edge precision {pct(r['edge_precision']['value'])} ({r['edge_precision']['edges']} edges), endpoint extraction recall {pct(r['endpoint_recall']['value'])} "
          f"({r['endpoint_recall']['found']}/{r['endpoint_recall']['of']}), change-level recall {pct(r['change_recall']['value'])}.", ""]
    if r.get("configuration"):
        c = r["configuration"]
        L += [f"Configuration used: {json.dumps(c['settings']['strip_prefixes'])} (the context paths set in the services' external configuration). "
              f"Without it {c['dependencies_found_without_it']}/{c['of']} dependencies are found.", ""]
    if r["recall"]["missed"]:
        L += ["**Missed dependencies**: " + "; ".join(f"{d['id']} {d['consumer']} -> {d['provider']} {d['method']} {d['path']} ({d.get('difficulty', 'plain')})" for d in r["recall"]["missed"]), ""]
    if r["edge_precision"]["false_edges"]:
        L += ["**False edges**: " + "; ".join(" ".join(map(str, e)) for e in r["edge_precision"]["false_edges"]), ""]
    if r["precision"]["false_positives"]:
        L += ["**False-positive material findings**: " + "; ".join(f"{x['change']}: {x['component']} ({x['node_type']})" for x in r["precision"]["false_positives"]), ""]
    L += ["| Change | Status | Material | Expected | Missing | Unexpected |", "|---|---|---|---|---|---|"]
    L += [f"| {c['id']} {c['description']} | {c['status']} | {c['material']} | {', '.join(c['expected']) or '-'} | {', '.join(c['missing']) or '-'} | {', '.join(c['unexpected']) or '-'} |"
          for c in r["changes"]]
    L += ["", "By segment (found/of): " + "; ".join(f"{k} {x['found']}/{x['of']}" for k, x in r["recall"]["by"].items())]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reference", help="a directory under reference/ (containing reference.json)")
    ap.add_argument("--source", help="where the reference's code is checked out (not needed for fixture references)")
    ap.add_argument("--report", help="write the full JSON result here")
    ap.add_argument("--enforce", action="store_true", help="exit 1 unless every measured PRD 15.3 target is met")
    ap.add_argument("--workdir", help="keep the generated repositories here (default: a temporary directory)")
    a = ap.parse_args(argv)
    ref_dir = Path(a.reference)
    if not ref_dir.is_absolute() and not ref_dir.exists():
        ref_dir = ROOT / a.reference
    ref = json.loads((ref_dir / "reference.json").read_text(encoding="utf-8"))
    work = Path(a.workdir) if a.workdir else Path(tempfile.mkdtemp(prefix="tia-accuracy-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        mat = materialize(ref, Path(a.source) if a.source else None, work)
        result = evaluate(ref, mat)
    finally:
        if not a.workdir:
            shutil.rmtree(work, ignore_errors=True)
    if a.report:
        Path(a.report).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    sys.stdout.buffer.write((markdown(result) + "\n").encode("utf-8"))
    return 0 if (not a.enforce or result["verdict"]["all_measured_targets_met"]) else 1


if __name__ == "__main__":
    sys.exit(main())
