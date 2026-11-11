"""The tester pack of one analysis (docs/CASE-STUDY.md: one change, three answers from the same code facts).

For the entry points a change reaches, and only those:
  1. what to retest, the tests found, and a coverage gate (covered / gaps / not_evaluated) over their technical aspects;
  2. the locator pack of the pages that call them (elements, unique locators, mandatory flags, dropdown values);
  3. the API test matrix of the touched endpoints (inputs, rules, valid and invalid values, expected error statuses).

It reads a FINISHED report and never starts an analysis. The report passed in is the one the reader may see (already redacted) and the
extracts are those of repositories the reader may read, so nothing of an unreadable repository is matched, counted or named. "Covered"
means a test addresses the aspect, not that it passes; evidence status is carried as the report has it, never upgraded.
"""
from __future__ import annotations

from .automation import api_inventory, summarise_api, ui_inventory
from .coverage import coverage_report, summary_of
from .testscope import labels_of

OUTCOMES = ("covered", "gaps", "not_evaluated")
NOTE = ("Static and textual: 'covered' means a test addresses the aspect, not that it passes. 'not_evaluated' means TIA has no analysed "
        "entry point to measure, not that it is safe.")


def _slim(e: dict) -> dict:
    keep = ("entry_point", "label", "node", "type", "service", "repo", "file", "line", "cross_repository", "change_effect", "changes",
            "why", "evidence_status")
    return {**{k: e[k] for k in keep if k in e},
            "tests": [{k: t[k] for k in ("name", "file", "repo", "association", "evidence_status", "result") if k in t} for t in e.get("tests", [])]}


def build_pack(rep: dict, extracts: dict, g, revisions: dict | None = None, unavailable: list | None = None) -> dict:
    """rep: a finished, already redacted report. extracts / g: the readable repositories at the revisions the pack describes
    (the changed repository at its proposed revision). unavailable: repositories whose revision could not be read."""
    ts = rep.get("test_scope") or {}
    entries, top = ts.get("entry_points", []), ts.get("code_without_entry_point", [])
    touched_nodes = {e["node"] for e in entries}
    touched_names = {(e["repo"], e["entry_point"]) for e in entries}
    hit = lambda x: x.get("node") in touched_nodes or (x.get("repo"), x.get("entry_point")) in touched_names     # noqa: E731

    cov = coverage_report(extracts, g) if extracts and g is not None else {"entry_points": []}
    apis = [i for i in api_inventory(extracts, labels_of(extracts)) if hit(i)] if extracts else []
    pages = [p for p in ui_inventory(extracts) if any(hit(x) for c in p["api_calls"] for x in c["endpoints"])] if extracts else []
    page_names = {(p["repo"], f"page {p['page']}") for p in pages}
    cov_rows = [e for e in cov["entry_points"] if hit(e) or (e["repo"], e["entry_point"]) in page_names]
    by_node = {e["node"]: e for e in cov_rows}
    by_name = {(e["repo"], e["entry_point"]): e for e in cov_rows}

    def gate_row(kind, name, label, service, repo, c, tests=(), effect=None, reason=None):
        failing = [t["name"] for t in tests if t.get("result") in ("failed", "error")]
        if c is None:
            outcome, why = "not_evaluated", reason or "no analysed entry point to measure coverage against"
        elif c["missing"] or c["partial"]:
            outcome = "gaps"
            why = f"{c['missing'] + c['partial']} of {c['total']} technical aspect(s) not fully covered"
        else:
            outcome, why = "covered", f"all {c['total']} technical aspect(s) are addressed by a test"
        if failing:
            why += f"; {len(failing)} linked test(s) FAILED in the last CI run"
        return {"entry_point": name, **({"label": label} if label else {}), "kind": kind, "service": service, "repo": repo,
                **({"change_effect": effect} if effect else {}), "outcome": outcome, "reason": why,
                "covered": c["covered"] if c else 0, "partial": c["partial"] if c else 0, "missing": c["missing"] if c else 0,
                "total": c["total"] if c else 0, "percent": c["percent"] if c else None, "tests_found": len(tests), "failing_tests": failing,
                "missing_aspects": [a["title"] for a in c["aspects"] if a["status"] != "covered"] if c else []}

    rows = []
    for e in entries:
        c = by_node.get(e["node"]) or by_name.get((e["repo"], e["entry_point"]))
        why = (f"repository '{e['repo']}' could not be read at the analysed revision" if e["repo"] in (unavailable or [])
               else "the entry point is outside the repositories you can read or TIA analyses" if e["repo"] not in extracts else None)
        rows.append(gate_row("api", e["entry_point"], e.get("label"), e["service"], e["repo"], c, e.get("tests", []), e["change_effect"], why))
    for e in top:
        rows.append(gate_row(e["type"], e["entry_point"], e.get("label"), e["service"], e["repo"], None, e.get("tests", []), e["change_effect"]))
    for p in pages:
        c = by_name.get((p["repo"], f"page {p['page']}"))
        rows.append(gate_row("page", f"page {p['page']}", None, p["repo"], p["repo"], c))
    counts = {k: sum(1 for r in rows if r["outcome"] == k) for k in OUTCOMES}
    outcome = "gaps" if counts["gaps"] else "not_evaluated" if counts["not_evaluated"] else "covered"
    if not rows:
        headline = "This change reaches no entry point: nothing to retest."
    else:
        headline = (f"{len(rows)} entry point(s) and page(s) to retest: {counts['covered']} covered, {counts['gaps']} with gaps, "
                    f"{counts['not_evaluated']} not evaluated.")

    refs = {(x.get("repo"), x.get("file"), x.get("name") or x.get("id"), x["type"]) for e in cov_rows for a in e["aspects"] for x in a["covered_by"]}
    ch = rep.get("change") or {}
    return {
        "analysis_id": rep["analysis_id"], "report_status": rep.get("status"),
        "change": {k: ch.get(k) for k in ("repository", "service", "pull_request", "base_revision", "proposed_revision", "change_type")},
        "basis": {"revisions": revisions or {r: e.commit for r, e in sorted(extracts.items())}, "unavailable_repositories": sorted(unavailable or []),
                  "note": "Computed for the changed repository at its proposed revision and the other repositories at the revisions the "
                          "analysis used. " + NOTE},
        "gate": {"outcome": outcome, "headline": headline, "counts": counts, "entry_points": rows},
        "retest": {"entry_points": [_slim(e) for e in entries], "code_without_entry_point": [_slim(e) for e in top],
                   "rule_gaps": ts.get("rule_gaps", []), "consistency_checks": ts.get("consistency_checks", []),
                   **({"redacted": ts["redacted"]} if ts.get("redacted") else {})},
        "coverage": {"entry_points": cov_rows, "unmatched_written_tests": [],
                     "summary": summary_of(cov_rows, sum(1 for r in refs if r[3] == "automated"), sum(1 for r in refs if r[3] == "written"), 0)},
        "api": {"endpoints": apis, "summary": summarise_api(apis)},
        "ui": {"pages": pages, "summary": {"pages": len(pages), "elements": sum(p["summary"]["elements"] for p in pages),
                                           "without_unique_locator": sum(p["summary"]["without_unique_locator"] for p in pages)}},
        "unknowns": rep.get("unknowns", []),
    }
