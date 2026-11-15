"""Regression test selection (docs/MEETING-PAWAN.md, use case 1): "out of your 10 test cases, these 4 must be regression-tested".

The suite is the written (functional) test cases kept in the analysed repositories (testcases/*.csv|yaml|json, tia.testcases) as they
were BEFORE the change: the regression suite the tester already has. Each case is put in exactly one bucket:

  selected       it names an entry point (API endpoint) the change reaches, or a page that calls one. Every reason is the reached entry
                 point with its change effect and why the change reaches it, so the tester can see the chain without reading code.
  not_selected   it names entry points or pages, and the change reaches none of them.
  unmapped       it names nothing TIA can match (no entry point column, no route or label in its text). It is listed for review by
                 hand, NEVER silently treated as unaffected.

Test cases added or edited by the change itself (the tester's new cases for the change) are listed separately: they are not regression.
Matching is static and textual (the same rules as the coverage view, tia.coverage): "selected" means "the change reaches what this case
tests", not that the case will fail. When Knowledge Fabric links test cases to sub-functions, those links become one more way to match;
until then the entry point / page named in the case is the link.
"""
from __future__ import annotations

import re

from .coverage import _entry_matches, _page_matches
from .testscope import EFFECT_RANK, labels_of

PRIORITY_RANK = {"p0": 0, "blocker": 0, "critical": 0, "p1": 1, "high": 1, "p2": 2, "medium": 2, "normal": 2, "p3": 3, "low": 3, "p4": 4}
BASIS = ("Static, textual matching: a written test case is selected when the entry point or page it names is reached by this change. "
         "Selected means the change reaches what the case tests, not that it will fail. Cases TIA cannot map are listed for review by "
         "hand, never dropped.")
METHOD = re.compile(r"\s*(GET|POST|PUT|PATCH|DELETE)\s", re.I)


def _priority(tc: dict) -> int:
    return PRIORITY_RANK.get(str(tc.get("priority") or "").strip().lower(), 5)


def _case(tc: dict) -> dict:
    return {k: tc.get(k, "") for k in ("id", "title", "entry_point", "priority", "type", "file", "line", "repo")}


def _endpoints(extracts: dict) -> list:
    labels = labels_of(extracts)
    return [{"node": e.id, "method": e.method, "path": e.path, "label": labels.get(e.id), "service": e.service, "repo": e.repo,
             "entry_point": f"{e.method} {e.path}"} for _, ex in sorted(extracts.items()) for e in ex.endpoints]


def _pages(extracts: dict) -> list:
    eps: dict = {}
    for ex in extracts.values():
        for e in ex.endpoints:
            eps.setdefault((e.method, e.norm_path), []).append(e.id)
    out = []
    for repo, ex in sorted(extracts.items()):
        for p in ex.ui_pages:
            calls = sorted({n for c in p.get("api_calls", []) for n in eps.get((c.get("method"), c.get("norm_path")), [])})
            out.append({**{k: p.get(k) for k in ("page", "route", "component")}, "repo": repo, "calls": calls})
    return out


def targets_of(tc: dict, endpoints: list, pages: list) -> tuple[set, list]:
    """The endpoint nodes a written case tests: those it names, and those called by the pages it names -> (nodes, [(page, node)])."""
    on_page = [p for p in pages if p["page"] and _page_matches(tc, p)]
    names_method = bool(METHOD.match(tc.get("entry_point") or ""))
    direct = {e["node"] for e in endpoints if (names_method or not on_page) and _entry_matches(tc, e)}
    via_page = [(p, n) for p in on_page for n in p["calls"]]
    return direct | {n for _, n in via_page}, via_page


def select(scope: dict, extracts: dict, target: str, head_cases: list | None = None) -> dict:
    """scope: the analysis's test scope (entry points reached). extracts: the analysed repositories, the changed one at its base revision.
    head_cases: the changed repository's written test cases at the proposed revision (to tell the change's own new cases apart)."""
    endpoints, pages = _endpoints(extracts), _pages(extracts)
    reached = {e["node"]: e for e in scope.get("entry_points", [])}
    suite = [dict(tc, repo=r) for r, ex in sorted(extracts.items()) for tc in ex.manual_tests]
    selected, not_selected, unmapped = [], [], []
    for tc in suite:
        nodes, via_page = targets_of(tc, endpoints, pages)
        if not nodes:
            unmapped.append({**_case(tc), "reason": "names no entry point, route or page TIA can match: review it by hand"})
            continue
        hits = sorted(n for n in nodes if n in reached)
        if not hits:
            names = sorted({e["entry_point"] for e in endpoints if e["node"] in nodes})
            not_selected.append({**_case(tc), "tests": names[:5],
                                 "reason": "tests " + ", ".join(names[:3]) + (" ..." if len(names) > 3 else "") + ", which this change does not reach"})
            continue
        pages_of = {}
        for p, n in via_page:
            pages_of.setdefault(n, []).append(p["page"])
        reasons = []
        for n in hits:
            e = reached[n]
            reasons.append({"entry_point": e["entry_point"], "node": n, **({"label": e["label"]} if e.get("label") else {}),
                            "service": e["service"], "repo": e["repo"], "change_effect": e["change_effect"], "why": e["why"],
                            **({"via_page": sorted(set(pages_of[n]))} if n in pages_of else {})})
        reasons.sort(key=lambda r: (-EFFECT_RANK[r["change_effect"]], r["entry_point"]))
        top = reasons[0]
        where = f" (on page {top['via_page'][0]})" if top.get("via_page") else ""
        selected.append({**_case(tc), "change_effect": top["change_effect"], "reasons": reasons,
                         "reason": f"tests {top['entry_point']}{where}, which this change reaches ({top['change_effect'].replace('_', ' ')})"
                                   + (f"; and {len(reasons) - 1} more reached entry point(s)" if len(reasons) > 1 else "")})
    selected.sort(key=lambda c: (-EFFECT_RANK[c["change_effect"]], _priority(c), c["repo"], c["id"]))

    new = []
    if head_cases is not None and target in extracts:
        before = {tc["id"]: tc for tc in extracts[target].manual_tests}
        for tc in head_cases:
            old = before.get(tc["id"])
            if old is None:
                new.append({**_case(dict(tc, repo=target)), "change": "added"})
            elif any(old.get(k) != tc.get(k) for k in ("title", "steps", "expected", "entry_point")):
                new.append({**_case(dict(tc, repo=target)), "change": "edited"})
    total = len(suite)
    out = {"selected": selected, "not_selected": not_selected, "unmapped": unmapped, "changed_by_this_change": new, "basis": BASIS}
    out["summary"] = summary_of(out, total)
    return out


def summary_of(reg: dict, total: int | None = None) -> dict:
    s, n, u = len(reg["selected"]), len(reg["not_selected"]), len(reg["unmapped"])
    total = s + n + u if total is None else total
    if not total:
        headline = "No written test cases were found in the analysed repositories (testcases/*.csv|yaml|json): nothing to select from."
    else:
        headline = f"Regression-test {s} of your {total} test case(s)"
        rest = [f"{n} not affected by this change" if n else "", f"{u} could not be mapped - review by hand" if u else ""]
        rest = [r for r in rest if r]
        headline += (" (" + "; ".join(rest) + ")" if rest else "") + "."
    return {"test_cases": total, "selected": s, "not_selected": n, "unmapped": u,
            "changed_by_this_change": len(reg.get("changed_by_this_change", [])), "headline": headline}


def redact(reg: dict, readable: set) -> int:
    """Keep what an audience may see: cases in unreadable repositories are dropped; a selected case keeps only reasons in readable
    repositories, and one with none left says the change reaches it through code the reader cannot see. -> number of cases withheld."""
    hidden = 0
    for key in ("selected", "not_selected", "unmapped", "changed_by_this_change"):
        kept = []
        for c in reg.get(key, []):
            if c.get("repo") not in readable:
                hidden += 1
                continue
            if key == "selected":
                rs = [r for r in c["reasons"] if r["repo"] in readable]
                if len(rs) != len(c["reasons"]):
                    c["reasons"] = rs
                    if not rs:
                        c["reason"] = "the change reaches what this case tests through code in repositories you cannot access"
            kept.append(c)
        reg[key] = kept
    reg["summary"] = summary_of(reg)
    return hidden
