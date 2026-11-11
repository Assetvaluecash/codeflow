"""Mermaid architecture block for impact reports (Pulse architecture-diagram port).

Scoped to evidence edges and changed seeds — folder/layer names are labels, not facts.
"""
from __future__ import annotations

from typing import Optional


def _safe(label: str) -> str:
    return (label or "node").replace('"', "'").replace("\n", " ")[:80]


def _svc(g, nid: str) -> str:
    n = g.nodes.get(nid) or {}
    return n.get("service") or n.get("repo") or nid.split(":")[0]


def build_architecture_diagram(g, report: dict, max_nodes: int = 24) -> dict:
    """Compact service-level Mermaid flowchart from findings + CONSUMES_API / event edges."""
    findings = report.get("findings") or []
    change = report.get("change") or {}
    target = change.get("service") or change.get("repository")
    services = set()
    if target:
        services.add(target)
    for f in findings:
        if f.get("component"):
            services.add(f["component"])
        for p in f.get("path") or []:
            if p.get("repo"):
                services.add((g.nodes.get(p["node"]) or {}).get("service") or p["repo"])

    # evidence edges between services
    links = []  # (src_svc, dst_svc, rel, status)
    seen = set()
    for e in g.edges:
        if e.rel not in ("CONSUMES_API", "CONSUMES_EVENT", "PUBLISHES", "CALLS", "INHERITS", "READS", "WRITES"):
            continue
        a, b = _svc(g, e.src), _svc(g, e.dst)
        if not a or not b or a == b:
            continue
        if services and a not in services and b not in services:
            continue
        key = (a, b, e.rel)
        if key in seen:
            continue
        seen.add(key)
        links.append((a, b, e.rel, e.status))

    # keep services that appear on a link or are the target / a finding
    for a, b, _, _ in links:
        services.add(a)
        services.add(b)
    ordered = sorted(services)[:max_nodes]
    affected = sorted({f["component"] for f in findings if f.get("component") and f["component"] != target})
    return _render(ordered, links, target, affected)


def _render(services: list, links: list, target: Optional[str], affected: list) -> dict:
    keep = set(services)
    links = [list(L) for L in links if L[0] in keep and L[1] in keep][:40]
    lines = ["flowchart LR"]
    for s in services:
        mark = ":::changed" if s == target else ""
        lines.append(f'  S{services.index(s)}["{_safe(s)}"]{mark}')
    for a, b, rel, status in links:
        dash = "-.->" if status == "inferred" else "-->"
        lines.append(f"  S{services.index(a)} {dash}|{rel}| S{services.index(b)}")
    if target in keep:
        lines += ["  classDef changed stroke-width:2px"]
    return {
        "format": "mermaid",
        "diagram": "\n".join(lines),
        "services": services,
        "edges": links,                     # [from service, to service, relationship, evidence status]: lets a redacted copy be re-drawn
        "links": len(links),
        "changed_service": target,
        "affected_services": [a for a in affected if a in keep],
        "note": "Diagram summarises evidence edges among services in this analysis; not a full landscape map.",
    }


def redact_diagram(diagram: Optional[dict], visible_services: set) -> Optional[dict]:
    """The diagram re-drawn with only the services the audience may see (AC-016): hidden services are not named, nor are their links."""
    if not diagram or not diagram.get("diagram"):
        return diagram
    services = [s for s in diagram.get("services", []) if s in visible_services]
    out = _render(services, diagram.get("edges") or [], diagram.get("changed_service"),
                  [a for a in diagram.get("affected_services", []) if a in visible_services])
    out["redacted_services"] = len(diagram.get("services", [])) - len(services)
    return out


def mermaid_markdown(diagram: Optional[dict]) -> list[str]:
    if not diagram or not diagram.get("diagram"):
        return []
    return ["", "**Architecture (evidence scope)**", "", "```mermaid", diagram["diagram"], "```",
            f"_{diagram.get('note', '')}_", ""]
