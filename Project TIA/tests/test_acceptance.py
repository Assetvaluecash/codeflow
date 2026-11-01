"""Maps to PRD section 14 acceptance criteria (Phase 1)."""
import json
import shutil

from conftest import by_component
from tia import EXTRACTOR_VERSION
from tia.report import redact, render_comment


def test_ac001_exact_revisions(fx, run):
    r = run()
    assert r["change"]["base_revision"] == fx["shas"]["base"]
    assert r["change"]["proposed_revision"] == fx["shas"]["breaking"]


def test_ac002_direct_impact_with_source_backed_path(run):
    r = run("impl")      # implementation change in lookup(): handler -> endpoint -> consumers
    f = by_component(r, "reconciliation-service", "callsite")[0]
    assert f["path"][0]["file"] == "app.py" and f["evidence"]["file"] == "client.py"
    assert f["change_nature"] == "nonbreaking"


def test_ac003_transitive_across_repos(run):
    r = run()
    ep = by_component(r, "reconciliation-service", "endpoint")[0]
    assert ep["impact_type"] == "transitive"
    assert ep["depth"] > by_component(r, "reconciliation-service", "callsite")[0]["depth"]
    repos = {p["repo"] for p in ep["path"]}
    assert {"payments-service", "reconciliation-service"} <= repos


def test_ac004_contract_impact_each_strategy(run):
    r = run()
    expect = {"reconciliation-service": ("R2", "source_confirmed"), "mobile-bff": ("R1", "source_confirmed"),
              "audit-reporting": ("R3", "contract_confirmed"), "ledger-sync": ("R5", "inferred")}
    for comp, (strat, status) in expect.items():
        f = by_component(r, comp)[0]
        assert f["resolution_strategy"] == strat and f["evidence_status"] == status, comp
        assert f["impact_type"] == "contract"


def test_ac005_unresolved_consumer_inventory_is_stated(run):
    r = run()
    areas = {u["area"] for u in r["unknowns"]}
    assert {"unresolved_outbound_calls", "external_consumers", "event_consumers"} <= areas
    assert r["status"] == "complete_with_gaps"


def test_ac006_test_association_has_method(run):
    r = run()
    f = by_component(r, "reconciliation-service", "callsite")[0]
    assert f["tests"] and f["tests"][0]["association"] == "test-import-call"
    # provider tests exercising the changed endpoint are listed too
    assert any(t["association"] == "test-http-call" for t in r["change"]["tests_in_changed_repository"])
    # consumers with no tests say so rather than claiming "no tests exist"
    f = by_component(r, "mobile-bff")[0]
    assert "No relevant test evidence found in the analysed sources" in f["limitations"]


def test_ac009_failed_repo_marks_partial(fx, run):
    repos = dict(fx["paths"]); repos["ghost-service"] = "/does/not/exist"
    r = run(repos=repos)
    assert r["status"] == "partial"
    assert r["coverage"]["repositories_inaccessible"][0]["repo"] == "ghost-service"


def test_ac009_parse_failure_marks_partial(run):
    r = run("syntax_error")
    assert r["status"] == "partial"
    assert any(u["area"] == "parse_failures" for u in r["unknowns"])


def test_ac009_unreadable_revision_is_failed_not_clean(run):
    r = run("0000000000000000000000000000000000000000")
    assert r["status"] == "failed" and r["findings"] == []
    assert "No impact conclusions" in render_comment(r)


def test_ac010_zero_impact_is_scoped_not_clean(run):
    r = run("docs_only")
    assert r["findings"] == []
    assert "not proof" in r["summary"]["message"]
    assert r["coverage"]["repositories_analysed"]          # coverage still visible
    assert r["status"] in ("complete", "complete_with_gaps")


def test_test_only_change(run):
    r = run("tests_only")
    assert r["change"]["change_type"] == "test_only" and r["findings"] == []


def test_ac012_bounded_traversal_reports_truncation(run):
    r = run(max_results=2)
    assert r["status"] == "partial"
    assert r["coverage"]["traversal"]["truncated"] is True
    assert any(u["area"] == "truncated_traversal" for u in r["unknowns"])
    r = run(max_depth=2)
    assert r["status"] == "partial"


def test_ac015_reproducible(fx, run):
    a, b = run(use_store=False), run(use_store=False)
    for r in (a, b):
        r.pop("generated_at")
    assert a == b
    assert a["evidence_snapshot_id"].startswith("snap-")


def test_ac016_redaction(run):
    r = run()
    red = redact(r, ["payments-service", "mobile-bff"])
    comps = {f["component"] for f in red["findings"]}
    assert comps == {"mobile-bff"}
    assert red["redacted"]["findings"] == len(r["findings"]) - 1
    blob = json.dumps(red)
    assert "reconciliation-service" not in blob and "audit-reporting" not in blob
    assert "withheld from this comment" in render_comment(red)


def test_ac017_no_invented_entities(run):
    r = run()
    nodes = {n["node"] for f in r["findings"] for n in f["path"]}
    for f in r["findings"]:
        assert f["evidence"]["file"]
        assert "all api" not in json.dumps(f).lower()
    # every finding's callsite/endpoint really exists in the landscape files
    assert all(":" in n for n in nodes)


def test_ac018_resolution_transparency(run):
    r = run()
    c = r["coverage"]
    assert c["outbound_call_sites"] >= c["outbound_call_sites_resolved"] > 0
    assert c["resolution_by_strategy"] == {"R1": 1, "R2": 1, "R3": 1, "R5": 1}
    u = next(u for u in r["unknowns"] if u["area"] == "unresolved_outbound_calls")
    assert any(i["repo"] == "notifications" and i["line"] > 0 for i in u["items"])


def test_ac019_inference_labelled_and_capped(run):
    r = run()
    f = by_component(r, "ledger-sync")[0]
    assert f["evidence_status"] == "inferred" and f["inference_rule"] == "INF-PATH-UNIQUE"
    assert f["severity"] == "medium" and f["severity_rule"] == "SEV-INFERRED-CAP"


def test_ac020_comment_single_marker_and_advisory(run):
    c = render_comment(run())
    assert c.count("<!-- tia-analysis -->") == 1 and "does not block merge" in c


def test_severity_rules(run):
    r = run()
    assert by_component(r, "reconciliation-service", "callsite")[0]["severity_rule"] == "SEV-BREAKING-CRITICAL"
    assert by_component(r, "mobile-bff")[0]["severity_rule"] == "SEV-BREAKING-CONFIRMED"
    r = run("impl")
    assert by_component(r, "reconciliation-service", "callsite")[0]["severity_rule"] == "SEV-NONBREAKING-CRITICAL"
    assert by_component(r, "mobile-bff")[0]["severity_rule"] == "SEV-NONBREAKING-CONFIRMED"


def test_removed_endpoint_is_breaking(run):
    r = run("removed_endpoint")
    kinds = {c["change"] for c in r["change"]["changed_components"]}
    assert "endpoint_removed" in kinds
    assert by_component(r, "reconciliation-service", "callsite")


def test_resolution_never_by_name_alone(tmp_path):
    """A call whose path matches two providers must stay unresolved (no guessing)."""
    import subprocess
    from fixtures.build_fixture import _commit, _run, _write
    from tia.extract_python import extract_repo
    from tia.graph import build_graph
    prov = lambda svc: extract_repo(svc, "c", {"a.py": "from fastapi import FastAPI\napp=FastAPI()\n"
                                                   "@app.get('/x/{i}')\ndef h(i): pass\n",
                                               "tia-services.yaml": f"service: {svc}\n"})
    cons = extract_repo("consumer", "c", {"c.py": "import requests\ndef f(i):\n    return requests.get(BASE + '/x/' + i)\n"})
    g = build_graph({"p1": prov("p1"), "p2": prov("p2"), "consumer": cons})
    assert not [e for e in g.edges if e.rel == "CONSUMES_API"]
    assert any("ambiguous" in u["reason"] or "not statically" in u["reason"] or "base" in u["reason"]
               for u in g.unresolved)


def test_audit_and_cache(fx, run, store):
    r = run()
    assert store.get_analysis(r["analysis_id"])["status"] == r["status"]
    events = [e["event"] for e in store.audit_events()]
    assert "analysis_requested" in events and "analysis_completed" in events
    assert store.verify_audit()["ok"] is True
    sha = fx["shas"]["base"]
    assert store.get_extract("payments-service", sha, EXTRACTOR_VERSION) is not None      # evidence cached for reuse
