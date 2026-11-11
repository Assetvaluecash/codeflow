"""Ports from Pulse/Codeflow: ambiguous CALLS, structure, excludes, inheritance, communities, diagram, catalog."""
from __future__ import annotations

from tia.communities import cluster
from tia.diagram import build_architecture_diagram, mermaid_markdown
from tia.exclude import matches, compile_patterns, should_exclude
from tia.extract_python import extract_repo
from tia.findings_catalog import enrich, lookup
from tia.graph import build_graph
from tia.report import render_comment
from tia.structure import analyse, detect_layer, detect_layer_violations
from tia.tenantconfig import parse, DEFAULT


def test_exclude_globs_match_nested_and_literal_regex_chars():
    assert should_exclude("vendor/lib/a.py", ("vendor/**",))
    assert should_exclude("dist/app.min.js", ("*.min.js",))
    assert not should_exclude("src/app.js", ("*.min.js", "vendor/**"))
    # regex metacharacters in user patterns are literal
    assert matches(compile_patterns(["file(1).py"]), "dir/file(1).py")
    assert not matches(compile_patterns(["file(1).py"]), "dir/fileX.py")


def test_tenant_exclude_patterns_round_trip():
    c = parse({"paths": {"exclude_patterns": ["vendor/**", "*.generated.py"]}})
    assert c.exclude_patterns == ("vendor/**", "*.generated.py")
    assert parse(c.to_dict()).exclude_patterns == c.exclude_patterns
    assert DEFAULT.exclude_patterns == ()
    assert "exclude_patterns" in DEFAULT.to_dict()["paths"]


def test_ambiguous_calls_become_inferred_edges():
    files = {
        "a.py": "def helper():\n    return 1\n",
        "b.py": "def helper():\n    return 2\n",
        "c.py": "def run():\n    return helper()\n",
        "tia-services.yaml": "service: demo\n",
    }
    ex = extract_repo("demo", "c0", files)
    g = build_graph({"demo": ex})
    amb = [e for e in g.edges if e.rel == "CALLS" and e.rule == "INF-CALL-AMBIGUOUS"]
    assert len(amb) == 2
    assert all(e.status == "inferred" for e in amb)
    assert g.ambiguous_calls == 2
    callees = sorted(e.dst for e in amb)
    assert any("a.py::helper" in c for c in callees) and any("b.py::helper" in c for c in callees)


def test_unique_call_stays_source_confirmed():
    files = {
        "math.py": "def add(a,b):\n    return a+b\n",
        "app.py": "def boot():\n    return add(1,2)\n",
        "tia-services.yaml": "service: demo\n",
    }
    g = build_graph({"demo": extract_repo("demo", "c0", files)})
    calls = [e for e in g.edges if e.rel == "CALLS"]
    assert len(calls) == 1 and calls[0].status == "source_confirmed" and calls[0].rule is None


def test_inheritance_edges_from_python_classes():
    files = {
        "base.py": "class Animal:\n    def speak(self):\n        pass\n",
        "dog.py": "class Dog(Animal):\n    def speak(self):\n        return 'woof'\n",
        "tia-services.yaml": "service: demo\n",
    }
    ex = extract_repo("demo", "c0", files)
    assert {t.name for t in ex.types} >= {"Animal", "Dog"}
    g = build_graph({"demo": ex})
    inherits = [e for e in g.edges if e.rel == "INHERITS"]
    assert len(inherits) == 1
    assert inherits[0].status == "source_confirmed"
    child = next(n for n, a in g.nodes.items() if a.get("name") == "Dog")
    parent = next(n for n, a in g.nodes.items() if a.get("name") == "Animal")
    assert inherits[0].src == child and inherits[0].dst == parent
    # changing parent impacts child
    impacted = {n for n, _ in g.impacted_by(parent)}
    assert child in impacted


def test_structural_layer_violation_direction():
    assert detect_layer("src/services/userService.py") == "services"
    assert detect_layer("src/lib/helper.py") == "lib"
    # Build a tiny graph: utils file CALLS into services file (upward)
    files = {
        "src/services/user.py": "def fetch_user():\n    return 1\n",
        "src/lib/helper.py": "def run():\n    return fetch_user()\n",
        "tia-services.yaml": "service: demo\n",
    }
    g = build_graph({"demo": extract_repo("demo", "c0", files)})
    viol = detect_layer_violations(g)
    assert any(v["from"].endswith("helper.py") and v["to"].endswith("user.py") for v in viol)


def test_structural_analyse_marks_changed_files():
    files = {
        "a.py": "def fa():\n    return fb()\n",
        "b.py": "def fb():\n    return fa()\n",
        "tia-services.yaml": "service: demo\n",
    }
    g = build_graph({"demo": extract_repo("demo", "c0", files)})
    s = analyse(g, {"a.py"})
    assert s["summary"]["cycles"] >= 1
    assert any(c.get("on_changed_path") for c in s["cycles"])
    assert s["note"]


def test_cycle_search_is_bounded_on_a_dense_acyclic_graph():
    """A layered graph where every file calls every file of the next layer has no cycle but exponentially many paths: the search
    must stop at its budget, say so, and stay quick."""
    import time
    from tia.graph import Graph
    from tia.models import Edge
    from tia.structure import find_cycles
    g = Graph()
    layers = [[f"l{i}/f{j}.py" for j in range(6)] for i in range(9)]
    for layer in layers:
        for f in layer:
            g.add_node(f, type="function", repo="r", file=f)
    for a, b in zip(layers, layers[1:]):
        for x in a:
            for y in b:
                g.add_edge(Edge(x, "CALLS", y, "source_confirmed"))
    t0, state = time.monotonic(), {}
    assert find_cycles(g, repo="r", budget=20_000, state=state) == []
    assert state.get("truncated") and time.monotonic() - t0 < 5
    assert analyse(g, repo="r")["summary"]["cycles"] == 0


def test_communities_split_two_clusters():
    files = {
        "alpha/a1.py": "def a1():\n    return a2()\n",
        "alpha/a2.py": "def a2():\n    return a3()\n",
        "alpha/a3.py": "def a3():\n    return a1()\n",
        "beta/b1.py": "def b1():\n    return b2()\n",
        "beta/b2.py": "def b2():\n    return b1()\n",
        "tia-services.yaml": "service: demo\n",
    }
    g = build_graph({"demo": extract_repo("demo", "c0", files)})
    result = cluster(g, seed=42)
    assert len(result["communities"]) >= 2
    assert result["god_nodes"]
    assert result["file_community"]


def test_finding_catalog_enrichment():
    meta = lookup("SEV-INFERRED-CAP")
    assert meta and meta["remediation"]
    f = enrich({"severity_rule": "SEV-BREAKING-CONFIRMED", "component": "x"})
    assert f["rule_id"] == "SEV-BREAKING-CONFIRMED" and f["explanation"] and f["remediation"]


def test_full_analysis_carries_the_overlays_scoped_to_the_changed_repository(tmp_path):
    """End to end on the lanes demo: catalog fields on findings, a diagram of the services involved, overlays keyed to the changed
    repository (three repositories each have an app.py), changed paths matched (git gives (status, path) pairs), and exclusions."""
    from fixtures.build_lanes_fixture import build
    from tia.impact import AnalysisRequest, analyze
    land = build(str(tmp_path / "land"))
    rep = analyze(AnalysisRequest(repos=land["paths"], target="payments-service", base="main", head="signature_unfixed"), None)
    assert rep["findings"] and all(f.get("explanation") and f.get("remediation") for f in rep["findings"] if f.get("severity_rule"))
    d = rep["architecture_diagram"]
    assert {"payments-service", "statement-service", "customer-portal"} <= set(d["services"]) and d["edges"]
    assert rep["structural_risk"]["repository"] == rep["communities"]["repository"] == "payments-service"
    files = set(rep["communities"]["file_community"])
    assert files <= {"app.py", "tests/test_payments.py"}          # never another repository's client.py
    # the analysis's snapshot records the exclusions it ran with, and excluded files are not analysed
    gone = analyze(AnalysisRequest(repos=land["paths"], target="payments-service", base="main", head="signature_unfixed",
                                   exclude_patterns=("app.py",)), None)
    assert not [f for f in gone["findings"] if f["repository"] != "payments-service"]


def test_structural_overlay_matches_changed_paths_and_redaction_hides_other_services():
    from tia.diagram import redact_diagram
    files = {"a.py": "def fa():\n    return fb()\n", "b.py": "def fb():\n    return fa()\n", "tia-services.yaml": "service: demo\n"}
    g = build_graph({"demo": extract_repo("demo", "c0", files)})
    s = analyse(g, {p for _, p in [("M", "a.py")]}, repo="demo")
    assert s["summary"]["cycles_on_change"] == 1 and analyse(g, {"a.py"}, repo="other")["summary"]["cycles"] == 0
    d = {"diagram": "x", "services": ["a", "b", "secret"], "edges": [["a", "b", "CONSUMES_API", "source_confirmed"],
                                                                     ["secret", "b", "CONSUMES_API", "source_confirmed"]],
         "changed_service": "b", "affected_services": ["a", "secret"]}
    r = redact_diagram(d, {"a", "b"})
    assert "secret" not in r["diagram"] and r["services"] == ["a", "b"] and r["links"] == 1 and r["redacted_services"] == 1


def test_graph_view_shows_classes_inheritance_and_ambiguous_calls():
    """The UI's Functions level: classes with readable labels, INHERITS edges, and inferred candidate calls carrying their rule."""
    from tia.graphview import build_view
    files = {"base.py": "class Animal:\n    pass\n", "dog.py": "class Dog(Animal):\n    pass\n",
             "a.py": "def helper():\n    return 1\n", "b.py": "def helper():\n    return 2\n", "c.py": "def run():\n    return helper()\n",
             "tia-services.yaml": "service: demo\n"}
    v = build_view({"demo": extract_repo("demo", "c0", files)}, level="function")
    labels = {n["label"] for n in v["nodes"] if n["type"] == "type"}
    assert labels == {"class Animal", "class Dog"}
    assert [e["status"] for e in v["edges"] if e["rel"] == "INHERITS"] == ["source_confirmed"]
    amb = [e for e in v["edges"] if e["rel"] == "CALLS" and e.get("rule") == "INF-CALL-AMBIGUOUS"]
    assert len(amb) == 2 and {e["status"] for e in amb} == {"inferred"}


def test_the_page_presents_the_new_report_sections():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "tia" / "web" / "index.html").read_text(encoding="utf-8")
    for needle in ("<th>What to do</th>", 'id="archcy"', 'id="structure"', "function renderStructure", 'node[type="type"]',
                   'edge[rel="INHERITS"]', 'edge[status="inferred"].struct', "ambiguous call", 'id="cfghelp"', "exclude_patterns",
                   'id="cplain"', 'id="cmd"', "function restoreLastAnalysis", "function warmAllSections"):
        assert needle in html, needle


def test_architecture_diagram_and_comment_include_mermaid():
    files = {
        "api.py": "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/x')\ndef h():\n    return 1\n",
        "tia-services.yaml": "service: payments-service\n",
    }
    ex = extract_repo("payments-service", "c0", files)
    g = build_graph({"payments-service": ex})
    report = {
        "analysis_id": "tia-x", "status": "complete",
        "summary": {"findings": 0, "material_findings": 0, "by_severity": {}, "message": "No findings."},
        "change": {"repository": "payments-service", "service": "payments-service",
                   "base_revision": "aaaaaaa", "proposed_revision": "bbbbbbb",
                   "change_type": "nonbreaking", "changed_components": [], "tests_in_changed_repository": []},
        "findings": [], "unknowns": [], "coverage": {"repositories_analysed": [], "outbound_call_sites": 0},
        "configuration_version": "cfg", "evidence_snapshot_id": "snap",
        "architecture_diagram": build_architecture_diagram(g, {
            "change": {"service": "payments-service", "repository": "payments-service"}, "findings": []}),
        "structural_risk": {"summary": {}},
    }
    d = report["architecture_diagram"]
    assert "flowchart" in d["diagram"] and "payments-service" in d["diagram"]
    md = render_comment(report)
    assert "```mermaid" in md
    assert mermaid_markdown(d)[0] == ""
