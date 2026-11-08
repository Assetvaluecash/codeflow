"""Support script (not production code): graph-store decision support (docs/GRAPH-STORE.md) - build, traversal and (de)serialisation cost of
TIA's in-memory graph at increasing sizes.

    python tools/graphbench.py out.json
"""
import json
import random
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tia.graph import Graph  # noqa: E402
from tia.models import SOURCE_CONFIRMED, Edge  # noqa: E402


def build(services: int, fns_per_service: int, calls_per_fn: int, consumers_per_ep: int, seed=7):
    rnd = random.Random(seed)
    g = Graph()
    eps = []
    for s in range(services):
        svc = f"svc{s}"
        fns = [f"{svc}:f{i}" for i in range(fns_per_service)]
        for f in fns:
            g.add_node(f, type="function", repo=svc, service=svc, file="a.py", line=1, name=f)
        for i, f in enumerate(fns):
            for _ in range(calls_per_fn):
                g.add_edge(Edge(f, "CALLS", rnd.choice(fns), SOURCE_CONFIRMED, "extract", None, {}))
        for e in range(max(1, fns_per_service // 10)):
            ep = f"{svc}:GET /r{e}"
            g.add_node(ep, type="endpoint", repo=svc, service=svc, file="a.py", line=1, method="GET", path=f"/r{e}")
            g.add_edge(Edge(ep, "HANDLES", rnd.choice(fns), SOURCE_CONFIRMED, "extract", None, {}))
            eps.append((svc, ep))
    for svc_i in range(services):
        svc = f"svc{svc_i}"
        for c in range(consumers_per_ep * max(1, fns_per_service // 10)):
            target_svc, ep = rnd.choice(eps)
            if target_svc == svc:
                continue
            cs = f"{svc}:cs{c}"
            g.add_node(cs, type="callsite", repo=svc, service=svc, file="b.py", line=c)
            g.add_edge(Edge(cs, "CONSUMES_API", ep, SOURCE_CONFIRMED, "R2", None, {}))
            g.add_edge(Edge(f"{svc}:f{rnd.randrange(fns_per_service)}", "ISSUES", cs, SOURCE_CONFIRMED, "extract", None, {}))
    return g


def bfs(g, start, depth=8, limit=200000):
    seen, frontier = {start}, [start]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for m, _e in g.impacted_by(n):
                if m not in seen:
                    seen.add(m)
                    nxt.append(m)
        frontier = nxt
        if not frontier or len(seen) > limit:
            break
    return len(seen)


rows = []
for services, fns in ((100, 200), (500, 200), (500, 800), (2000, 400)):
    tracemalloc.start()
    t0 = time.perf_counter()
    g = build(services, fns, 3, 4)
    build_s = time.perf_counter() - t0
    mem = tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()
    starts = [n for n in g.nodes if n.endswith(":f0")][:50]
    t0 = time.perf_counter()
    reached = [bfs(g, s) for s in starts]
    trav = (time.perf_counter() - t0) / len(starts)
    t0 = time.perf_counter()
    blob = json.dumps(g.to_dict())
    ser = time.perf_counter() - t0
    t0 = time.perf_counter()
    Graph.from_dict(json.loads(blob))
    load = time.perf_counter() - t0
    rows.append({"services": services, "nodes": len(g.nodes), "edges": len(g.edges), "build_s": round(build_s, 2), "peak_mb": round(mem),
                 "traversal_ms_avg": round(trav * 1000, 1), "reached_median": sorted(reached)[len(reached) // 2],
                 "json_mb": round(len(blob) / 1e6, 1), "serialise_s": round(ser, 2), "load_s": round(load, 2)})
    print(rows[-1], flush=True)
json.dump(rows, open(sys.argv[1], "w"), indent=2)
