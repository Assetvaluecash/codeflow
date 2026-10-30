"""Code / dependency graph views for the UI (PRD 9.2 supporting visualisation).

Builds the landscape graph from the same extracts and resolution used by the impact engine, at three
levels of detail, and an impact overlay from a stored analysis report. Read-only.

  service   one node per service; edges = aggregated cross-service API consumption
  endpoint  services, endpoints, schemas; consumers linked to the endpoints they call; unresolved calls shown
  function  everything extracted (functions, call sites, tests, endpoints, schemas)
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional

from .graph import build_graph
from .models import RepoExtract
from .resolve import merged_service_map

LEVELS = ("service", "endpoint", "function")
STRENGTH = ["corroborated", "contract_confirmed", "source_confirmed", "runtime_observed", "inferred", "stale"]
SEV_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _best(statuses) -> str:
    for s in STRENGTH:
        if s in statuses:
            return s
    return "unknown"


def _label(nid: str, n: dict) -> str:
    t = n["type"]
    if t == "service":
        return n["service"]
    if t == "endpoint":
        return f"{n['method']} {n['path']}"
    if t in ("function", "schema", "test", "entity", "table", "column"):
        return n.get("name") or nid
    if t == "migration":
        return f"migration {n.get('name')}"
    if t == "callsite":
        return f"call {n.get('file')}:{n.get('line')}"
    if t == "contract_ref":
        return f"contract {n.get('file')}"
    return nid


def build_view(extracts: dict, level: str = "service", repos: Optional[list] = None, q: Optional[str] = None,
               focus: Optional[str] = None, depth: int = 1, limit: int = 600, graph=None,
               stale: Optional[dict] = None) -> dict:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    g = graph or build_graph(extracts)
    smap = merged_service_map(extracts)
    svc_of_repo = {r: e.service for r, e in extracts.items()}
    unresolved = {u["id"]: u for u in g.unresolved}
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(nid, **a):
        nodes.setdefault(nid, {"id": nid, **a})

    def add_edge(s, t, rel, status=None, **a):
        edges.append({"id": f"{s}>{rel}>{t}#{len(edges)}", "source": s, "target": t, "rel": rel, "status": status, **a})

    # ---- service nodes (all levels) ----------------------------------------------------------------------
    for repo, ex in sorted(extracts.items()):
        unres = sum(1 for u in g.unresolved if u["repo"] == repo)
        add_node(f"svc:{ex.service}", type="service", label=ex.service, repo=repo, service=ex.service,
                 endpoints=len(ex.endpoints), functions=len(ex.functions), schemas=len(ex.schemas),
                 callsites=len(ex.callsites), tests=len(ex.tests), unresolved_calls=unres,
                 files_failed=len(ex.files_failed), unsupported_files=ex.non_python_files, commit=ex.commit,
                 criticality=smap["criticality"].get(ex.service), owner=smap["owners"].get(ex.service))

    consumes = [e for e in g.edges if e.rel == "CONSUMES_API"]
    stale = stale or {}

    def key_of(e) -> str:
        return f"{g.nodes[e.src]['repo']}|{g.nodes[e.src].get('file')}|{e.dst}"

    def status_of(e) -> str:
        """A relationship the history marks stale is never presented as current, whatever the last graph says."""
        return "stale" if key_of(e) in stale else e.status

    in_graph = {key_of(e) for e in consumes}
    gone = [r for k, r in sorted(stale.items()) if k not in in_graph
            and r["consumer_repo"] in extracts and r["provider_repo"] in extracts]      # evidence no longer in the graph

    if level == "service":
        agg = defaultdict(lambda: {"statuses": set(), "strategies": set(), "ev": []})
        for e in consumes:
            a, b = g.nodes[e.src]["service"], g.nodes[e.dst]["service"]
            if a == b:
                continue
            x = agg[(a, b)]
            x["statuses"].add(status_of(e))
            x["strategies"].update((e.strategy or "").split("+"))
            x["ev"].append(f"{e.evidence.get('repo')}:{e.evidence.get('file')}:{e.evidence.get('line')} {e.strategy}")
            x["stale"] = x.get("stale", 0) + (status_of(e) == "stale")
        for r in gone:
            x = agg[(r["consumer_service"], r["provider_service"])]
            x["statuses"].add("stale")
            x["ev"].append(f"{r['consumer_repo']}:{r['consumer_file']}:{r['consumer_line']} {r['strategy']} (stale: {r['stale_reason']})")
            x["stale"] = x.get("stale", 0) + 1
        for (a, b), x in sorted(agg.items()):
            add_edge(f"svc:{a}", f"svc:{b}", "CONSUMES_API", _best(x["statuses"]), count=len(x["ev"]),
                     strategies=sorted(s for s in x["strategies"] if s), inferred_only=x["statuses"] == {"inferred"},
                     stale_count=x.get("stale", 0), evidence=sorted(x["ev"])[:20])
    else:
        for nid, n in g.nodes.items():
            t = n["type"]
            if t == "service":
                continue
            if level == "endpoint" and t not in ("endpoint", "schema"):
                continue
            if t == "column" and not (q or focus):
                continue                    # dozens per repository: shown when the viewer searches or focuses, not in the overview
            add_node(nid, type=t, label=_label(nid, n), repo=n["repo"], service=n["service"], file=n.get("file"),
                     line=n.get("line"), unresolved=nid in unresolved)
        for e in g.edges:
            if e.rel == "CONSUMES_API":
                continue
            if e.src in nodes and e.dst in nodes or (e.rel == "EXPOSES" and e.dst in nodes):
                if level == "endpoint" and e.rel not in ("EXPOSES", "USES_SCHEMA"):
                    continue
                add_edge(e.src, e.dst, e.rel, e.status, strategy=e.strategy)
        if level == "function":
            for e in consumes:
                add_edge(e.src, e.dst, "CONSUMES_API", status_of(e), strategy=e.strategy, rule=e.rule,
                         evidence=[f"{e.evidence.get('repo')}:{e.evidence.get('file')}:{e.evidence.get('line')}"])
        else:   # endpoint level: consumer service -> endpoint, aggregated per (service, endpoint)
            agg = defaultdict(lambda: {"statuses": set(), "strategies": set(), "ev": []})
            for e in consumes:
                a = g.nodes[e.src]["service"]
                if a == g.nodes[e.dst]["service"]:
                    continue
                x = agg[(a, e.dst)]
                x["statuses"].add(status_of(e))
                x["strategies"].update((e.strategy or "").split("+"))
                x["ev"].append(f"{e.evidence.get('repo')}:{e.evidence.get('file')}:{e.evidence.get('line')} {e.strategy}")
                x["stale"] = x.get("stale", 0) + (status_of(e) == "stale")
            for r in gone:
                if r["provider_endpoint"] in g.nodes:
                    x = agg[(r["consumer_service"], r["provider_endpoint"])]
                    x["statuses"].add("stale")
                    x["ev"].append(f"{r['consumer_repo']}:{r['consumer_file']}:{r['consumer_line']} {r['strategy']} (stale: {r['stale_reason']})")
                    x["stale"] = x.get("stale", 0) + 1
            for (a, ep), x in sorted(agg.items()):
                add_edge(f"svc:{a}", ep, "CONSUMES_API", _best(x["statuses"]), count=len(x["ev"]),
                         strategies=sorted(s for s in x["strategies"] if s), inferred_only=x["statuses"] == {"inferred"},
                         stale_count=x.get("stale", 0), evidence=sorted(x["ev"])[:20])
            for uid, u in sorted(unresolved.items()):
                svc = svc_of_repo.get(u["repo"])
                if svc:
                    add_node(f"unres:{uid}", type="unresolved", label=f"? {u['file']}:{u['line']}", repo=u["repo"],
                             service=svc, file=u["file"], line=u["line"], reason=u["reason"])
                    add_edge(f"svc:{svc}", f"unres:{uid}", "UNRESOLVED_CALL", "unknown", reason=u["reason"])

    total_nodes, total_edges = len(nodes), len(edges)

    # ---- filters -----------------------------------------------------------------------------------------------
    if repos:
        keep = {n for n, d in nodes.items() if d.get("repo") in set(repos)}
        nodes = {n: nodes[n] for n in keep}
    edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]

    def neighbours(seed: set, hops: int) -> set:
        adj = defaultdict(set)
        for e in edges:
            adj[e["source"]].add(e["target"]); adj[e["target"]].add(e["source"])
        seen, dq = set(seed), deque((s, 0) for s in seed)
        while dq:
            n, d = dq.popleft()
            if d >= hops:
                continue
            for m in adj[n]:
                if m not in seen:
                    seen.add(m); dq.append((m, d + 1))
        return seen

    if focus:
        if focus not in nodes:
            raise KeyError(f"node '{focus}' not in this view")
        nodes = {n: nodes[n] for n in neighbours({focus}, max(1, depth))}
    elif q:
        ql = q.lower()
        hit = {n for n, d in nodes.items() if ql in d["label"].lower() or ql in n.lower()}
        nodes = {n: nodes[n] for n in neighbours(hit, 1)} if hit else {}
    edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]

    truncated = False
    if len(nodes) > limit:
        deg = defaultdict(int)
        for e in edges:
            deg[e["source"]] += 1; deg[e["target"]] += 1
        top = sorted(nodes, key=lambda n: (-deg[n], n))[:limit]
        nodes = {n: nodes[n] for n in top}
        edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]
        truncated = True

    return {"level": level, "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(edges, key=lambda e: e["id"]),
            "stats": {"nodes": len(nodes), "edges": len(edges), "total_nodes": total_nodes,
                      "total_edges": total_edges, "truncated": truncated, "limit": limit,
                      "unresolved_call_sites": len(g.unresolved)}}


def landscape_stats(extracts: dict, graph=None) -> list:
    g = graph or build_graph(extracts)
    out = []
    for repo, ex in sorted(extracts.items()):
        out.append({"repo": repo, "service": ex.service, "commit": ex.commit, "python_files": ex.files_analysed,
                    "files_failed": len(ex.files_failed), "unsupported_files": ex.non_python_files,
                    "functions": len(ex.functions), "endpoints": len(ex.endpoints), "schemas": len(ex.schemas),
                    "callsites": len(ex.callsites), "tests": len(ex.tests),
                    "unresolved_calls": sum(1 for u in g.unresolved if u["repo"] == repo)})
    return out


def restrict(view: dict, readable) -> dict:
    """Audience filter: drop nodes of repositories the viewer cannot read, and the edges touching them."""
    if readable is None:
        return view
    ok = set(readable)
    nodes = [n for n in view["nodes"] if n.get("repo") in ok]
    ids = {n["id"] for n in nodes}
    edges = [e for e in view["edges"] if e["source"] in ids and e["target"] in ids]
    hidden = len(view["nodes"]) - len(nodes)
    return {**view, "nodes": nodes, "edges": edges,
            "stats": {**view["stats"], "nodes": len(nodes), "edges": len(edges), "hidden_by_permissions": hidden}}


def impact_overlay(report: dict) -> dict:
    """Graph of exactly what an analysis found: changed components -> affected components, per evidence path."""
    if report.get("status") == "failed":
        return {"nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0}, "analysis_id": report["analysis_id"]}
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    for f in report["findings"]:
        path, pe = f["path"], f["path_edges"]
        for i, p in enumerate(path):
            n = nodes.setdefault(p["node"], {"id": p["node"], "type": p["type"], "repo": p["repo"], "file": p["file"],
                                             "line": p["line"], "label": _short(p), "role": "via", "severity": None})
            if i == 0:
                n["role"] = "changed"
            if i == len(path) - 1 and n["role"] != "changed":
                n["role"] = "affected"
                if n["severity"] is None or SEV_RANK[f["severity"]] > SEV_RANK[n["severity"]]:
                    n["severity"] = f["severity"]
                n["finding"] = f["id"]
        for i, e in enumerate(pe):
            k = (path[i]["node"], path[i + 1]["node"], e["rel"])
            edges.setdefault(k, {"id": f"{k[0]}>{k[2]}>{k[1]}", "source": k[0], "target": k[1], "rel": e["rel"],
                                 "status": e["status"], "strategy": e["strategy"]})
    return {"analysis_id": report["analysis_id"], "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(edges.values(), key=lambda e: e["id"]),
            "stats": {"nodes": len(nodes), "edges": len(edges)}}


def _short(p: dict) -> str:
    tail = p["node"].split("::")[-1] if "::" in p["node"] else p["node"].split(":", 1)[-1]
    return tail if p["type"] != "callsite" else f"call {p.get('file')}:{p.get('line')}"
