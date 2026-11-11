"""Report rendering and audience filtering (PRD section 8, 9.1, 11.1)."""
from __future__ import annotations

import copy
import json

from .diagram import mermaid_markdown, redact_diagram
from .testscope import redact_scope

MARKER = "<!-- tia-analysis -->"
SEV_ICON = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW", "unknown": "UNKNOWN"}


def redact(report: dict, readable_repos) -> dict:
    """Hide findings/evidence from repositories the audience cannot read (AC-016).

    Findings in unreadable repositories are replaced by a count; path hops through them are masked.
    """
    if readable_repos is None or report.get("status") == "failed":
        return report
    readable = set(readable_repos)
    r = copy.deepcopy(report)
    kept, hidden = [], 0
    for f in r["findings"]:
        if f["repository"] not in readable:
            hidden += 1
            continue
        f["path"] = [p if p["repo"] in readable else {"node": "[redacted]", "type": "redacted", "repo": None,
                                                      "file": None, "line": None} for p in f["path"]]
        f["tests"] = [t for t in f["tests"] if t["repo"] in readable]
        kept.append(f)
    r["findings"] = kept
    if r.get("test_scope"):
        redact_scope(r["test_scope"], readable)
    if r.get("policy_results"):
        r["policy_results"] = redact_policy({"results": r["policy_results"]}, report, readable)["results"]
    visible_services = {f["component"] for f in kept} | {r["change"].get("service")}
    if r.get("owners"):
        r["owners"] = {k: v for k, v in r["owners"].items() if k in visible_services}
    # derived overlays name services and files too: the diagram keeps only visible services; the file-level overlays cover the
    # changed repository only, so they are kept when the audience can read it and dropped when it cannot
    if r.get("architecture_diagram"):
        r["architecture_diagram"] = redact_diagram(r["architecture_diagram"], visible_services)
    if r["change"]["repository"] not in readable:
        for k in ("structural_risk", "communities"):
            r.pop(k, None)
        for f in kept:
            f["limitations"] = [x for x in f.get("limitations") or [] if "(structural overlay)" not in x and "(folder convention overlay)" not in x]
    # evidence sections (FR-022 to FR-029) name repositories too: only readable ones are kept
    if r.get("test_recommendations"):
        r["test_recommendations"]["tests"] = [t for t in r["test_recommendations"]["tests"] if t["repo"] in readable]
    if "test_gaps" in r:
        r["test_gaps"] = [g for g in r["test_gaps"] if g["repo"] in readable]
    if r.get("deployments"):
        d = r["deployments"]
        d["by_repo"] = {k: v for k, v in d["by_repo"].items() if k in readable}
        d["incomplete"] = [x for x in d["incomplete"] if x["repo"] in readable]
    for c in r["change"]["changed_components"]:
        if c.get("coverage"):
            c["coverage"]["tests_executing"] = c["coverage"]["tests_executing"] if r["change"]["repository"] in readable else []
    for u in r["unknowns"]:
        if "items" in u:
            n = len(u["items"])
            u["items"] = [i for i in u["items"] if i.get("repo") in readable]
            if len(u["items"]) != n:
                u["redacted_items"] = n - len(u["items"])
                if u.get("area") in ("repository_freshness", "stale_relationships"):      # free text names repositories
                    u["reason"] = f"{u['redacted_items']} item(s) concerning repositories you cannot access were omitted."
                    u.pop("count", None)
        if "files_by_repo" in u:
            u["files_by_repo"] = {k: v for k, v in u["files_by_repo"].items() if k in readable}
    for c in r["change"]["changed_components"]:
        if "mapped_by" in c:                                    # entities of repositories the audience cannot read are not named
            n = len(c["mapped_by"])
            c["mapped_by"] = [m for m in c["mapped_by"] if m["repo"] in readable]
            if len(c["mapped_by"]) != n:
                c["mapped_by_redacted"] = n - len(c["mapped_by"])
    cov = r["coverage"]
    if "data" in cov:
        cov["data"].pop("databases", None)                      # private databases are named after their services
    cov["repositories_analysed"] = [x for x in cov["repositories_analysed"] if x["repo"] in readable]
    cov["repositories_inaccessible"] = [x for x in cov["repositories_inaccessible"] if x["repo"] in readable]
    r["change"]["tests_in_changed_repository"] = (
        r["change"]["tests_in_changed_repository"] if r["change"]["repository"] in readable else [])
    s = r["summary"]
    r["redacted"] = {"findings": hidden,
                     "note": f"{hidden} finding(s) in repositories you cannot access were omitted." if hidden else ""}
    s["findings"] = len(kept)
    s["material_findings"] = sum(1 for f in kept if f["material"])
    s["by_severity"] = {k: sum(1 for f in kept if f["severity"] == k) for k in s["by_severity"]}
    s["by_evidence_status"] = {}
    for f in kept:
        s["by_evidence_status"][f["evidence_status"]] = s["by_evidence_status"].get(f["evidence_status"], 0) + 1
    return r


def redact_policy(pol: dict, report: dict, readable) -> dict:
    """Policy outcomes are computed on the whole report (a reader cannot make a rule pass by not seeing it); the evidence behind them is
    shown only for repositories the reader can see."""
    if readable is None:
        return pol
    ok = set(readable)
    repo_of = {f["id"]: f["repository"] for f in report.get("findings", [])}
    out = copy.deepcopy(pol)
    for r in out["results"]:
        keep = [e for e in r["evidence"] if (e.get("repo") or repo_of.get(e.get("finding"), "")) in ok
                or not (e.get("repo") or e.get("finding"))]
        if len(keep) != len(r["evidence"]):
            r["withheld_evidence"] = len(r["evidence"]) - len(keep)
            r["reason"] = r["reason"].split(":")[0] + f" ({r['withheld_evidence']} item(s) in repositories you cannot access withheld)."
        r["evidence"] = keep
    return out


def redact_snapshot(snap: dict, readable_repos) -> dict:
    """An audience sees only the repositories it can read in a snapshot."""
    if readable_repos is None:
        return snap
    ok = set(readable_repos)
    out = copy.deepcopy(snap)
    out["revisions"] = {r: c for r, c in out["revisions"].items() if r in ok}
    out["freshness"] = {r: f for r, f in out["freshness"].items() if r in ok}
    out["stale_relationships"] = [x for x in out["stale_relationships"] if x["consumer_repo"] in ok and x["provider_repo"] in ok]
    return out


def evidence_view(report: dict, snapshot: dict) -> dict:
    """What an authorised reviewer needs to audit a result (AC-015): the snapshot (source revisions, versions, freshness)
    and, for every material finding, the evidence path with the exact revision of each repository on it."""
    revs = snapshot["revisions"]
    mats = []
    for f in report["findings"]:
        if not f["material"]:
            continue
        mats.append({"id": f["id"], "component": f["component"], "severity": f["severity"], "severity_rule": f["severity_rule"],
                     "evidence_status": f["evidence_status"], "resolution_strategy": f.get("resolution_strategy"),
                     "hops": [{"node": p["node"], "type": p["type"], "repo": p["repo"], "revision": revs.get(p["repo"]),
                               "file": p["file"], "line": p["line"]} for p in f["path"]],
                     "edges": f["path_edges"], "evidence": f["evidence"]})
    return {"analysis_id": report["analysis_id"], "evidence_snapshot_id": snapshot["id"], "snapshot": snapshot,
            "material_findings": mats}


def differences(original: dict, repeated: dict) -> list:
    """Top-level report sections that differ, ignoring the generation time."""
    keys = (set(original) | set(repeated)) - {"generated_at"}
    return sorted(k for k in keys if original.get(k) != repeated.get(k))


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False)


def _short(sha: str) -> str:
    return (sha or "")[:7]


def permalink(public_url: str | None, analysis_id: str) -> str | None:
    """Stable address of an analysis's report page (UC1-16), or None when TIA has no public URL configured."""
    return f"{public_url.rstrip('/')}/analyses/{analysis_id}" if public_url else None


def render_comment(report: dict, max_rows: int = 12, link: str | None = None) -> str:
    ch, st = report["change"], report["status"]
    L = [MARKER, f"### TIA impact analysis - `{st}`", ""]
    if st != "failed":
        s = report["summary"]
    if st == "failed":
        L += [f"**The analysis failed**: {report.get('failure_reason', 'unknown error')}.",
              "No impact conclusions can be drawn from this result.", ""]
        return "\n".join(L + [_footer(report, link)])
    L.append(f"`{ch['repository']}` {_short(ch['base_revision'])} -> {_short(ch['proposed_revision'])}"
             f" - change type: **{ch['change_type']}**")
    if st != "complete":
        why = {"complete_with_gaps": "the analysis ran to completion but has unknowns that limit confidence",
               "partial": "the analysis is INCOMPLETE (limit reached, stale data, or a source failed)",
               "running": "the analysis is still running"}[st]
        L += ["", f"> **Warning:** {why}. This is not a clean impact assessment."]
    sev = s["by_severity"]
    L += ["", f"**{s['findings']} finding(s), {s['material_findings']} material** - "
          + " / ".join(f"{sev[k]} {k}" for k in ("critical", "high", "medium", "low", "unknown") if sev.get(k)) if s["findings"]
          else "", ""]
    withheld = report.get("redacted", {}).get("findings", 0)
    if not s["findings"] and not withheld:
        L += [s.get("message", "No findings."), ""]
    elif not s["findings"]:
        L += [f"**{withheld} finding(s) in other repositories are withheld from this comment** (visibility policy). "
              "This is not a clean result: open the TIA report for the full impact.", ""]
    L += _test_scope_md(report.get("test_scope"), max_rows, report)
    L += mermaid_markdown(report.get("architecture_diagram"))
    mat = [f for f in report["findings"] if f["material"]]
    if mat:
        L += ["| Severity | Component | Evidence | Why |", "|---|---|---|---|"]
        for f in mat[:max_rows]:
            ev = f["evidence_status"] + (f" ({f['resolution_strategy']})" if f.get("resolution_strategy") else "")
            L.append(f"| {SEV_ICON[f['severity']]} | {f['component']} | {ev} | {f['reason'].split('. Change:')[0]} |")
        if len(mat) > max_rows:
            L.append(f"| | _+{len(mat) - max_rows} more material findings in the full report_ | | |")
        L.append("")
        L.append("<details><summary>Required verification</summary>\n")
        seen = set()
        for f in mat[:max_rows]:
            if f.get("remediation"):
                k = (f["component"], f["remediation"])
                if k not in seen:
                    seen.add(k)
                    # the rule id stays in the report: printed here it would show e.g. "CRITICAL" beside a severity the tenant overrode
                    L.append(f"- **{f['component']}**: {f['remediation']}")
            for v in f["required_verification"]:
                k = (f["component"], v)
                if k not in seen:
                    seen.add(k)
                    L.append(f"- **{f['component']}**: {v}")
        L.append("\n</details>")
    summ = (report.get("structural_risk") or {}).get("summary") or {}
    if summ.get("cycles_on_change") or summ.get("high_coupling_on_change") or summ.get("layer_violations_on_change"):
        L += ["", "**Structural risk on changed files** (overlay, not confirmed impact)"]
        if summ.get("cycles_on_change"):
            L.append(f"- {summ['cycles_on_change']} circular-dependency ring(s) touch changed files")
        if summ.get("high_coupling_on_change"):
            L.append(f"- {summ['high_coupling_on_change']} high-coupling hub(s) among changed files")
        if summ.get("layer_violations_on_change"):
            L.append(f"- {summ['layer_violations_on_change']} layer-direction violation(s) on changed paths")
    low = [f for f in report["findings"] if not f["material"]]
    if low:
        L += ["", f"_{len(low)} non-material finding(s) collapsed; see the full report._"]
    cc = [c for c in ch["changed_components"] if c["type"].startswith("api")]
    if cc:
        L += ["", "**Contract changes**"]
        for c in cc:
            L.append(f"- {'BREAKING' if c.get('breaking') else 'non-breaking'}: {c['detail']}")
    dc = [c for c in ch["changed_components"] if c["type"] in ("db_table", "db_column") and c["change"] not in
          ("table_added", "column_added", "column_made_nullable", "column_type_widened")]
    if dc:
        L += ["", "**Data changes**"]
        for c in dc:
            L.append(f"- {'BREAKING' if c.get('breaking') else 'non-breaking'}: {c['detail']} (`{c['file']}`)")
    if report.get("policy_results"):
        pol = report["policy"]
        L += ["", f"**Release policy ({pol['mode']}): {pol['outcome']}**" + (" - a mandatory policy failed" if pol.get("blocking") else "")]
        for r in report["policy_results"]:
            L.append(f"- {r['outcome'].upper()} `{_md(r['id'])}`{'' if r['mandatory'] else ' (optional)'}: {_md(r['reason'])}")
    cov = report["coverage"]
    if cov.get("outbound_call_sites"):
        L += ["", f"**Coverage:** {len(cov['repositories_analysed'])} repo(s) analysed; "
              f"{cov['outbound_call_sites_resolved']}/{cov['outbound_call_sites']} outbound calls resolved"
              f" ({round(100 * (cov['resolution_rate'] or 0))}%)."]
    unk = [u for u in report["unknowns"] if u["affects_confidence"]]
    if unk:
        L += ["", "**Unknowns that affect confidence**"]
        for u in unk:
            L.append(f"- {u['reason']}")
    if withheld and s["findings"]:
        L += ["", f"_{withheld} more finding(s) in other repositories are withheld from this comment (visibility policy)._"]
    return "\n".join(L + ["", _footer(report, link)])


EFFECT_TEXT = {"breaking": "BREAKING", "behaviour_change": "behaviour change", "compatible_change": "compatible change",
               "no_functional_change": "no functional change", "unknown": "unknown (file-level)"}


def _md(text) -> str:
    """A value placed in a markdown table cell: no pipes or line breaks from source code."""
    return str(text).replace("|", "\\|").replace("\n", " ")


RESULT_TEXT = {"passed": " (passed)", "failed": " (**FAILED**)", "error": " (**ERROR**)", "skipped": " (skipped)", "not_in_run": " (not in the CI run)"}


def _result(t: dict) -> str:
    r = RESULT_TEXT.get(t.get("result") or "", "")
    if r and t.get("run") and not t["run"].get("of_analysed_revision"):
        r = r[:-1] + ", other revision)"
    return r


def _evidence_md(report: dict) -> list:
    """CI results, recommended tests and runtime/deployment facts (FR-022 to FR-029), only when the evidence exists."""
    L = []
    ts = report.get("test_scope") or {}
    tests = {(t["repo"], t["file"], t["name"]): t for e in ts.get("entry_points", []) + ts.get("code_without_entry_point", []) for t in e["tests"]}
    if any(t.get("result") not in (None, "no_ci_results") for t in tests.values()):
        n = {k: sum(1 for t in tests.values() if t.get("result") == k) for k in ("passed", "failed", "error", "skipped", "not_in_run", "no_ci_results")}
        L.append("**Test evidence (CI):** " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in n.items() if v) + ".")
    recs = (report.get("test_recommendations") or {}).get("tests") or []
    if recs:
        L += ["", "<details><summary>Recommended tests</summary>", ""]
        for r in recs[:15]:
            L.append(f"- `{_md(r['test'])}` ({_md(r['repo'])}) - {_md(r['why'][0])}{_result({'result': r.get('last_result')})}")
        L += ["", f"_{report['test_recommendations']['note']}_", "", "</details>", ""]
    rt = [f for f in report.get("findings", []) if f.get("runtime")]
    if rt:
        L.append(f"**Runtime:** {len(rt)} finding(s) confirmed by calls observed in " +
                 ", ".join(sorted({e for f in rt for e in f["runtime"]["environments"]}) or ["telemetry"]) + ".")
    dep = report.get("deployments")
    if dep:
        drift = [f"{r} in {env} ({d['revision'][:7]})" for r, envs in dep["by_repo"].items() for env, d in envs.items() if not d["is_analysed_revision"]
                 and r != report["change"]["repository"]]
        if drift:
            L.append("**Deployed consumers differ from the analysed revision:** " + "; ".join(drift[:6]) + ("; ..." if len(drift) > 6 else "") + ".")
    return L + ([""] if L else [])


def _test_scope_md(ts: dict | None, max_rows: int, report: dict) -> list:
    """The tester's view first: which entry points to test because of this change, and the rule gaps and consistency checks it touches."""
    if not ts or not (ts["entry_points"] or ts["code_without_entry_point"] or ts["rule_gaps"]):
        return []
    L = []
    rows = ts["entry_points"] + ts["code_without_entry_point"]
    if rows:
        L += ["**What to test** (entry points this change reaches)", "", "| Entry point | Service | Change effect | Tests found |", "|---|---|---|---|"]
        for e in rows[:max_rows]:
            name = f"{e['label']} (`{_md(e['entry_point'])}`)" if e.get("label") else f"`{_md(e['entry_point'])}`"
            if e["type"] != "endpoint":
                name += " - no analysed API endpoint"
            tests = ", ".join(f"`{_md(t['name'])}`{_result(t)}" for t in e["tests"][:3]) + (" ..." if len(e["tests"]) > 3 else "") or "none found"
            ev = "" if e["evidence_status"] in ("source_confirmed", "contract_confirmed", "corroborated") else f" ({e['evidence_status']})"
            L.append(f"| {name} | {_md(e['service'])} | {EFFECT_TEXT[e['change_effect']]}{ev} | {tests} |")
        if len(rows) > max_rows:
            L.append(f"| _+{len(rows) - max_rows} more in the full report_ | | | |")
        L.append("")
    for gp in ts["rule_gaps"]:
        L.append(("- **Rule moved into the shared service** - " if gp.get("resolved_by_this_change") else "- **Possible rule gap** - ") + _md(gp["reason"]))
    for c in ts["consistency_checks"]:
        L.append(f"- **Consistency check** - {_md(c['check'])}")
    if ts["rule_gaps"] or ts["consistency_checks"]:
        L.append("")
    L += _evidence_md(report)
    if (ts.get("redacted") or {}).get("items"):
        L += [f"_{ts['redacted']['note']}_", ""]
    return L


def _footer(report: dict, link: str | None = None) -> str:
    return ((f"[Full report]({link})\n\n" if link else "") +
            f"<sub>Advisory only - does not block merge. analysis `{report['analysis_id']}` - "
            f"snapshot `{report.get('evidence_snapshot_id', 'n/a')}` - config `{report['configuration_version']}`</sub>")
