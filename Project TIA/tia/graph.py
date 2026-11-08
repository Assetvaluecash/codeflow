"""In-memory typed dependency graph built from repository extracts."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from .data import add_data_layer, add_sql_lineage
from .extract_python import module_name
from .models import CONTRACT_CONFIRMED, INFERRED, SOURCE_CONFIRMED, Edge, RepoExtract
from .resolve import resolve_consumers


def _path_matches(template: str, concrete: str) -> bool:
    """Endpoint template ('/a/{}/b') vs a test's concrete or templated path ('/a/123/b')."""
    ta, tb = template.strip("/").split("/"), concrete.strip("/").split("/")
    return len(ta) == len(tb) and all(x == y or x == "{}" or y == "{}" for x, y in zip(ta, tb))


class Graph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[Edge] = []
        self._impact: dict[str, list] = defaultdict(list)
        self._deps: dict[str, list] = defaultdict(list)     # the reverse of _impact: what a node depends on
        self.unresolved: list[dict] = []
        self.resolution_stats: dict = {}
        self.ambiguous_calls = 0
        self.sql_unresolved: list[dict] = []        # procedure calls that match no analysed definition (FR-041)

    def add_node(self, nid: str, **attrs):
        self.nodes[nid] = attrs

    def add_edge(self, e: Edge):
        self.edges.append(e)
        # impact direction (who is affected when `key` changes)
        if e.rel in ("HANDLES", "PUBLISHES"):  # handler change -> endpoint; producer change -> the topic it publishes to
            self._impact[e.src].append((e.dst, e))
            self._deps[e.dst].append((e.src, e))
        elif e.rel in ("CALLS", "USES_SCHEMA", "CONSUMES_API", "CONSUMES_EVENT", "ISSUES", "TESTS", "MAPS_TO", "MAPS_COLUMN", "READS", "WRITES"):
            self._impact[e.dst].append((e.src, e))     # table/column -> entity -> function: who is affected by a data change
            self._deps[e.src].append((e.dst, e))
        # EXPOSES / OWNED_BY / MIGRATES are not traversed

    def impacted_by(self, nid: str):
        return sorted(self._impact.get(nid, []), key=lambda t: (t[0], t[1].rel))

    def depends_on(self, nid: str):
        """What a change to these nodes would reach `nid` through (the reverse of impacted_by)."""
        return sorted(self._deps.get(nid, []), key=lambda t: (t[0], t[1].rel))

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "edges": [asdict(e) for e in self.edges], "unresolved": self.unresolved,
                "resolution_stats": self.resolution_stats, "ambiguous_calls": self.ambiguous_calls, "sql_unresolved": self.sql_unresolved}

    @staticmethod
    def from_dict(d: dict) -> "Graph":
        g = Graph()
        g.nodes = d["nodes"]
        for e in d["edges"]:
            g.add_edge(Edge(**e))
        g.unresolved, g.resolution_stats, g.ambiguous_calls = d["unresolved"], d["resolution_stats"], d["ambiguous_calls"]
        g.sql_unresolved = d.get("sql_unresolved", [])
        return g


def add_events(g, extracts: dict):
    """Topics (global: one Kafka topic name is one node), producers, consumers (listener nodes, like outbound call sites) and the
    schema each topic carries (FR-019)."""
    def topic(t):
        nid = f"topic:{t}"
        if nid not in g.nodes:
            g.add_node(nid, type="topic", repo=None, service=None, file=None, line=None, name=t)
        return nid
    for repo, ex in extracts.items():
        ev = lambda file, line: {"repo": repo, "file": file, "line": line, "commit": ex.commit}
        for e in ex.events:
            t = topic(e["topic"])
            status, strat = (CONTRACT_CONFIRMED, "E2") if e.get("declared") else (SOURCE_CONFIRMED, "E1")
            if e["kind"] == "produces":
                src = e["function_id"] or f"svc:{ex.service}"
                g.add_edge(Edge(src, "PUBLISHES", t, status, strat, None, ev(e["file"], e["line"])))
            else:
                lid = f"{repo}:{e['file']}:{e['line']}:consumes:{e['topic']}"
                g.add_node(lid, type="listener", repo=repo, service=ex.service, file=e["file"], line=e["line"], name=f"{e['via']} {e['topic']}")
                g.add_edge(Edge(lid, "CONSUMES_EVENT", t, status, strat, None, ev(e["file"], e["line"])))
                if e["function_id"]:
                    g.add_edge(Edge(e["function_id"], "ISSUES", lid, SOURCE_CONFIRMED, "extract", None, ev(e["file"], e["line"])))
        for s in ex.event_schemas:
            g.add_node(s["id"], type="event_schema", repo=repo, service=ex.service, file=s["file"], line=s["line"], name=s["name"])
            if s.get("topic"):
                inferred = s.get("topic_source") == "file_name"
                g.add_edge(Edge(topic(s["topic"]), "USES_SCHEMA", s["id"], INFERRED if inferred else CONTRACT_CONFIRMED,
                                f"schema-{s.get('topic_source')}", "INF-SCHEMA-FILE-NAME" if inferred else None, ev(s["file"], s["line"])))


def build_graph(extracts: dict[str, RepoExtract], strip_prefixes: tuple = (),
                test_naming: bool = True) -> Graph:
    g = Graph()
    fn_by_repo_name: dict[str, dict[str, list]] = {}
    for repo, ex in extracts.items():
        g.add_node(f"svc:{ex.service}", type="service", repo=repo, service=ex.service)
        by_name = defaultdict(list)
        for f in ex.functions:
            g.add_node(f.id, type="function", repo=repo, service=ex.service, file=f.file,
                       line=f.line, name=f.qualname)
            by_name[f.qualname.split(".")[-1]].append(f)
        fn_by_repo_name[repo] = by_name
        for s in ex.schemas:
            g.add_node(s.id, type="schema", repo=repo, service=ex.service, file=s.file, line=s.line, name=s.name)
        for e in ex.endpoints:
            g.add_node(e.id, type="endpoint", repo=repo, service=ex.service, file=e.file, line=e.line,
                       method=e.method, path=e.norm_path, route=e.path)
        for c in ex.callsites:
            g.add_node(c.id, type="callsite", repo=repo, service=ex.service, file=c.file, line=c.line)
        for t in ex.tests:
            g.add_node(t.id, type="test", repo=repo, service=ex.service, file=t.file, line=t.line, name=t.name)

    for repo, ex in extracts.items():
        ev = lambda file, line: {"repo": repo, "file": file, "line": line, "commit": ex.commit}
        by_name = fn_by_repo_name[repo]
        schema_ids = {s.name: s.id for s in ex.schemas}
        for e in ex.endpoints:
            g.add_edge(Edge(f"svc:{ex.service}", "EXPOSES", e.id, SOURCE_CONFIRMED, "extract", None, ev(e.file, e.line)))
            if e.source == "code":                                  # an endpoint declared only in a specification has no handler in the code
                g.add_edge(Edge(e.handler_id, "HANDLES", e.id, SOURCE_CONFIRMED, "extract", None, ev(e.file, e.line)))
            for nm in ([e.response_schema] if e.response_schema else []) + list(e.request_schemas):
                if nm in schema_ids:
                    g.add_edge(Edge(e.id, "USES_SCHEMA", schema_ids[nm], SOURCE_CONFIRMED, "extract", None,
                                    ev(e.file, e.line)))
        for c in ex.callsites:
            if c.function_id:
                g.add_edge(Edge(c.function_id, "ISSUES", c.id, SOURCE_CONFIRMED, "extract", None, ev(c.file, c.line)))
        for f in ex.functions:
            for callee in f.calls:
                cands = by_name.get(callee, [])
                if len(cands) == 1 and cands[0].id != f.id:
                    g.add_edge(Edge(f.id, "CALLS", cands[0].id, SOURCE_CONFIRMED, "extract", None, ev(f.file, f.line)))
                elif len(cands) > 1:
                    g.ambiguous_calls += 1
        # ---- test association (TFD: TESTS is a static association, not execution) -------------------
        funcs_by_module = defaultdict(list)
        for f in ex.functions:
            funcs_by_module[module_name(f.file)].append(f)
        for t in ex.tests:
            linked = False
            for key in t.http_calls:
                for ep in ex.endpoints:
                    if ep.method == key[0] and _path_matches(ep.norm_path, key[1]):
                        g.add_edge(Edge(t.id, "TESTS", ep.id, SOURCE_CONFIRMED, "test-http-call", None,
                                        ev(t.file, t.line)))
                        linked = True
            for mod, funcs in funcs_by_module.items():
                if mod in t.imports or any(i.endswith("." + mod) for i in t.imports):
                    for f in funcs:
                        if f.qualname.split(".")[-1] in t.called and f.id != t.id:
                            g.add_edge(Edge(t.id, "TESTS", f.id, SOURCE_CONFIRMED, "test-import-call", None,
                                            ev(t.file, t.line)))
                            linked = True
            if test_naming and not linked:
                stem = t.file.rsplit("/", 1)[-1]
                if stem.endswith(".java"):
                    stem = next((stem[:-len(x) - 5] for x in ("Tests", "Test", "IT") if stem.endswith(x + ".java")), None)
                else:
                    stem = stem[5:-3] if stem.startswith("test_") else stem[:-8] if stem.endswith("_test.py") else None
                if stem:
                    for mod, funcs in funcs_by_module.items():
                        if mod.rsplit(".", 1)[-1] == stem:
                            for f in funcs:
                                g.add_edge(Edge(t.id, "TESTS", f.id, INFERRED, "test-naming", "INF-TEST-NAMING",
                                                ev(t.file, t.line)))

    add_events(g, extracts)
    edges, unresolved, stats, synthetic = resolve_consumers(extracts, strip_prefixes)
    for nid, attrs in synthetic.items():
        g.add_node(nid, **attrs)
    for e in edges:
        g.add_edge(e)
    g.unresolved, g.resolution_stats = unresolved, stats
    add_data_layer(g, extracts)
    add_sql_lineage(g, extracts)
    return g
