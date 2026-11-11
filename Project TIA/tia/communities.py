"""File-level communities and god nodes (Pulse Leiden port, pure-Python label propagation).

Computed overlay only: communities are not extracted evidence and must be labelled as derived.
Ambiguous/inferred edges are weighted at half of confirmed edges, matching Pulse.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional

from .models import CONFIRMED


def _file_of(g, nid: str) -> Optional[str]:
    return (g.nodes.get(nid) or {}).get("file")


def _weighted_edges(g, repo: Optional[str] = None) -> list[tuple[str, str, float]]:
    edges = []
    for e in g.edges:
        if e.rel not in ("CALLS", "INHERITS"):
            continue
        # file paths are repository-relative: one repository at a time, or two repositories' 'app.py' would be one file
        if repo is not None and ((g.nodes.get(e.src) or {}).get("repo") != repo or (g.nodes.get(e.dst) or {}).get("repo") != repo):
            continue
        a, b = _file_of(g, e.src), _file_of(g, e.dst)
        if not a or not b or a == b:
            continue
        w = 1.0 if e.status in CONFIRMED else 0.5
        edges.append((a, b, w))
    return edges


def cluster(g, seed: int = 42, rounds: int = 20, top_god: int = 8, repo: Optional[str] = None) -> dict:
    """Label-propagation communities over files. Deterministic given seed and graph order. repo: one repository's files only."""
    wedges = _weighted_edges(g, repo)
    if not wedges:
        return {"repository": repo, "communities": [], "god_nodes": [], "file_community": {},
                "note": "No CALLS/INHERITS file edges; communities not computed."}
    files = sorted({f for a, b, _ in wedges for f in (a, b)})
    idx = {f: i for i, f in enumerate(files)}
    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    degree = [0.0] * len(files)
    for a, b, w in wedges:
        i, j = idx[a], idx[b]
        adj[i].append((j, w))
        adj[j].append((i, w))
        degree[i] += w
        degree[j] += w
    labels = list(range(len(files)))
    order = list(range(len(files)))
    rng = seed
    for _ in range(rounds):
        for i in range(len(order) - 1, 0, -1):
            rng = (1103515245 * rng + 12345) & 0x7FFFFFFF
            j = rng % (i + 1)
            order[i], order[j] = order[j], order[i]
        changed = False
        for i in order:
            if not adj[i]:
                continue
            votes: dict[int, float] = defaultdict(float)
            for nb, w in adj[i]:
                votes[labels[nb]] += w
            best = max(votes.items(), key=lambda t: (t[1], -t[0]))[0]
            if best != labels[i]:
                labels[i] = best
                changed = True
        if not changed:
            break

    groups: dict[int, list[str]] = defaultdict(list)
    for f, lab in zip(files, labels):
        groups[lab].append(f)

    communities, file_community = [], {}
    for lab, members in sorted(groups.items(), key=lambda t: (-len(t[1]), min(t[1]))):
        members = sorted(members)
        folders = Counter((m.split("/")[0] if "/" in m else "root") for m in members)
        cid = len(communities)
        communities.append({
            "id": cid,
            "label": folders.most_common(1)[0][0],
            "size": len(members),
            "files": members[:50],
            "file_count": len(members),
        })
        for f in members:
            file_community[f] = cid

    god = sorted(({"file": files[i], "degree": round(degree[i], 2)} for i in range(len(files))),
                 key=lambda x: (-x["degree"], x["file"]))[:top_god]
    return {
        "repository": repo,
        "communities": communities,
        "god_nodes": god,
        "file_community": file_community,
        "note": "Communities are a derived overlay (label propagation on CALLS/INHERITS); not extracted evidence.",
    }
