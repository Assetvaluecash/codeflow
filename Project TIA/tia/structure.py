"""Structural risk amplifiers on the evidence graph (Pulse cycles / coupling / layers port).

These are computed overlays used as triage context. They are not confirmed impact edges and
must not invent consumers. Layer names are folder conventions, labelled as such.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from .findings_catalog import lookup

# Lower number = higher layer. A dependency from a higher-number layer into a lower-number layer is upward.
LAYER_ORDER = {
    "ui": 0, "controllers": 1, "controller": 1, "api": 1, "web": 1,
    "services": 2, "service": 2, "application": 2, "app": 2,
    "domain": 3, "model": 3, "models": 3, "entities": 3,
    "data": 4, "repository": 4, "repositories": 4, "dao": 4, "persistence": 4,
    "utils": 5, "util": 5, "lib": 5, "libs": 5, "common": 5, "shared": 5, "helpers": 5,
    "tests": 9, "test": 9, "__tests__": 9,
}
_LAYER_RE = re.compile(r"(?:^|/)(" + "|".join(sorted(LAYER_ORDER, key=len, reverse=True)) + r")(?:/|$)", re.I)
COUPLING_TOP = 10
COUPLING_MIN_DEGREE = 4


def detect_layer(path: str) -> str:
    m = _LAYER_RE.search((path or "").replace("\\", "/").lower())
    if not m:
        return "utils"
    return m.group(1).lower()


def _file_of(g, nid: str) -> Optional[str]:
    n = g.nodes.get(nid) or {}
    return n.get("file")


def _call_file_edges(g, repo: Optional[str] = None) -> list[tuple[str, str, object]]:
    """(def_file, caller_file, edge) for CALLS / INHERITS with files. Convention matches Pulse:
    source of structural edge = definition / superclass file; target = caller / subclass file.
    repo: only edges inside that repository. File paths are relative to their repository, so without it two repositories'
    'app.py' would be one node (and a report would name files of repositories its reader cannot see)."""
    out = []
    for e in g.edges:
        if e.rel not in ("CALLS", "INHERITS"):
            continue
        if repo is not None and ((g.nodes.get(e.src) or {}).get("repo") != repo or (g.nodes.get(e.dst) or {}).get("repo") != repo):
            continue
        src_f, dst_f = _file_of(g, e.src), _file_of(g, e.dst)
        if not src_f or not dst_f or src_f == dst_f:
            continue
        # Edge(caller, CALLS, callee) and Edge(child, INHERITS, parent): the definition is dst, the user is src
        out.append((dst_f, src_f, e))
    return out


CYCLE_SEARCH_STEPS = 50_000      # path enumeration is exponential on dense graphs: a fixed budget keeps an analysis bounded and deterministic


def find_cycles(g, limit: int = 25, repo: Optional[str] = None, budget: int = CYCLE_SEARCH_STEPS, state: Optional[dict] = None) -> list[dict]:
    """Simple directed cycles among files linked by CALLS/INHERITS, bounded by `limit` cycles and `budget` search steps.
    state (optional dict): receives "truncated": True when the budget ran out before the search finished."""
    dadj: dict[str, set] = defaultdict(set)
    for def_f, caller_f, _ in _call_file_edges(g, repo):
        dadj[caller_f].add(def_f)
    cycles, seen, steps = [], set(), 0
    for start in sorted(dadj):
        if steps > budget or len(cycles) >= limit:
            break
        stack = [(start, [start])]
        while stack and len(cycles) < limit:
            steps += 1
            if steps > budget:
                if state is not None:
                    state["truncated"] = True
                break
            node, path = stack.pop()
            for nxt in sorted(dadj.get(node, ())):
                if nxt == start and len(path) >= 2:
                    key = tuple(sorted(path))
                    if key not in seen:
                        seen.add(key)
                        cycles.append({"files": path, "length": len(path)})
                elif nxt not in path and len(path) < 8:
                    stack.append((nxt, path + [nxt]))
    cycles.sort(key=lambda c: (-c["length"], c["files"]))
    return cycles[:limit]


def high_coupling(g, top: int = COUPLING_TOP, min_degree: int = COUPLING_MIN_DEGREE, repo: Optional[str] = None) -> list[dict]:
    deg: dict[str, int] = defaultdict(int)
    for def_f, caller_f, _ in _call_file_edges(g, repo):
        deg[def_f] += 1
        deg[caller_f] += 1
    ranked = sorted(((f, d) for f, d in deg.items() if d >= min_degree), key=lambda t: (-t[1], t[0]))
    return [{"file": f, "degree": d} for f, d in ranked[:top]]


def detect_layer_violations(g, limit: int = 50, repo: Optional[str] = None) -> list[dict]:
    viol = []
    for def_f, caller_f, e in _call_file_edges(g, repo):
        if e.rel == "INHERITS":
            continue  # inheritance across layers is common and not flagged (Pulse skips inherits)
        from_layer, to_layer = detect_layer(caller_f), detect_layer(def_f)
        if from_layer in ("tests", "test", "__tests__") or to_layer in ("tests", "test", "__tests__"):
            continue
        fo, to = LAYER_ORDER.get(from_layer, 5), LAYER_ORDER.get(to_layer, 5)
        if fo > to:  # lower layer importing a higher layer
            viol.append({
                "from": caller_f, "to": def_f, "from_layer": from_layer, "to_layer": to_layer,
                "suggestion": f"{from_layer} should not import from {to_layer}",
                "rel": e.rel, "status": e.status,
            })
            if len(viol) >= limit:
                break
    viol.sort(key=lambda v: (v["from"], v["to"]))
    return viol


def analyse(g, changed_files: Optional[set] = None, repo: Optional[str] = None) -> dict:
    """Full structural overlay. Marks items that touch changed_files when provided. repo: the repository the changed files
    belong to (an impact report passes the changed repository); None = every repository (files are then repository-relative)."""
    changed_files = {f.replace("\\", "/") for f in (changed_files or set())}
    search: dict = {}
    cycles = find_cycles(g, repo=repo, state=search)
    coupling = high_coupling(g, repo=repo)
    layers = detect_layer_violations(g, repo=repo)

    def touches(files):
        return bool(changed_files and changed_files.intersection(files))

    for c in cycles:
        c["on_changed_path"] = touches(c["files"])
        c.update(lookup("STRUCT-CYCLE") or {})
    for h in coupling:
        h["on_changed_path"] = h["file"] in changed_files
        h.update(lookup("STRUCT-HIGH-COUPLING") or {})
    for v in layers:
        v["on_changed_path"] = touches({v["from"], v["to"]})
        v.update(lookup("STRUCT-LAYER-VIOLATION") or {})

    return {
        "repository": repo,
        "cycles": cycles,
        "high_coupling": coupling,
        "layer_violations": layers,
        "summary": {
            "cycles": len(cycles),
            "cycles_on_change": sum(1 for c in cycles if c.get("on_changed_path")),
            "high_coupling": len(coupling),
            "high_coupling_on_change": sum(1 for h in coupling if h.get("on_changed_path")),
            "layer_violations": len(layers),
            "layer_violations_on_change": sum(1 for v in layers if v.get("on_changed_path")),
        },
        "cycle_search_truncated": bool(search.get("truncated")),
        "note": "Structural risk is a computed overlay on CALLS/INHERITS edges, not confirmed cross-service impact."
                + (" The cycle search stopped at its step budget: more cycles may exist." if search.get("truncated") else ""),
    }


def limitations_for_file(path: str, structural: dict) -> list[str]:
    """Short limitations to attach when a finding's evidence file sits in structural risk."""
    if not path or not structural:
        return []
    path = path.replace("\\", "/")
    out = []
    if any(path in c.get("files", []) for c in structural.get("cycles", [])):
        out.append("Changed code sits on a circular dependency among analysed call edges (structural overlay).")
    if any(h.get("file") == path for h in structural.get("high_coupling", [])):
        out.append("Evidence file is a high-coupling hub on the call graph (structural overlay).")
    if any(path in (v.get("from"), v.get("to")) for v in structural.get("layer_violations", [])):
        out.append("Evidence file participates in a layer-direction violation (folder convention overlay).")
    return out
