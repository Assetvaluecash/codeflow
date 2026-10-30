"""Persisted, versioned dependency graph (UC1-11). A graph version is a pure function of its inputs (repository
commits, extractor and configuration versions), so it is content-addressed: the same inputs always give the same
version id, a changed commit gives a new version, and old versions stay readable and comparable."""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from . import CONFIG_VERSION, EXTRACTOR_VERSION
from .graph import Graph, build_graph
from .models import RepoExtract
from .store import Store

LANDSCAPE = "landscape"


def graph_inputs(extracts: dict, strip: tuple = (), test_naming: bool = True) -> dict:
    return {"repos": {r: e.commit for r, e in sorted(extracts.items())}, "extractor": EXTRACTOR_VERSION,
            "config": CONFIG_VERSION, "strip": list(strip), "test_naming": test_naming}


def graph_id(inputs: dict) -> str:
    return "g-" + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]


def _stats(g: Graph) -> dict:
    by_type: dict = {}
    for n in g.nodes.values():
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    return {"nodes": len(g.nodes), "edges": len(g.edges), "nodes_by_type": by_type,
            "cross_repo_relationships": sum(1 for e in g.edges if e.rel == "CONSUMES_API"),
            "unresolved": len(g.unresolved)}


def get_or_build(store: Optional[Store], extracts: dict, strip: tuple = (), test_naming: bool = True,
                 rebuild: bool = False):
    """Returns (graph, graph_id, built). Loads the persisted version for these inputs, else builds and persists it."""
    inputs = graph_inputs(extracts, strip, test_naming)
    gid = graph_id(inputs)
    if store and not rebuild:
        data = store.get_graph(gid)
        if data:
            return Graph.from_dict(data), gid, False
    g = build_graph(extracts, strip, test_naming)
    if store:
        store.put_graph(gid, inputs, _stats(g), g.to_dict(), overwrite=rebuild)
    return g, gid, True
