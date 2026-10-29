"""Report rendering and audience filtering (PRD section 8, 9.1, 11.1)."""
from __future__ import annotations

import copy
import json

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
    for u in r["unknowns"]:
        if "items" in u:
            n = len(u["items"])
            u["items"] = [i for i in u["items"] if i.get("repo") in readable]
            if len(u["items"]) != n:
                u["redacted_items"] = n - len(u["items"])
        if "files_by_repo" in u:
            u["files_by_repo"] = {k: v for k, v in u["files_by_repo"].items() if k in readable}
    cov = r["coverage"]
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


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False)


def _short(sha: str) -> str:
    return (sha or "")[:7]


def render_comment(report: dict, max_rows: int = 12) -> str:
    ch, st = report["change"], report["status"]
    L = [MARKER, f"### TIA impact analysis - `{st}`", ""]
    if st != "failed":
        s = report["summary"]
    if st == "failed":
        L += [f"**The analysis failed**: {report.get('failure_reason', 'unknown error')}.",
              "No impact conclusions can be drawn from this result.", ""]
        return "\n".join(L + [_footer(report)])
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
    if not s["findings"]:
        L += [s.get("message", "No findings."), ""]
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
            for v in f["required_verification"]:
                k = (f["component"], v)
                if k not in seen:
                    seen.add(k)
                    L.append(f"- **{f['component']}**: {v}")
        L.append("\n</details>")
    low = [f for f in report["findings"] if not f["material"]]
    if low:
        L += ["", f"_{len(low)} non-material finding(s) collapsed; see the full report._"]
    cc = [c for c in ch["changed_components"] if c["type"].startswith("api")]
    if cc:
        L += ["", "**Contract changes**"]
        for c in cc:
            L.append(f"- {'BREAKING' if c.get('breaking') else 'non-breaking'}: {c['detail']}")
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
    if report.get("redacted", {}).get("findings"):
        L += ["", f"_{report['redacted']['note']}_"]
    return "\n".join(L + ["", _footer(report)])


def _footer(report: dict) -> str:
    return (f"<sub>Advisory only - does not block merge. analysis `{report['analysis_id']}` - "
            f"snapshot `{report.get('evidence_snapshot_id', 'n/a')}` - config `{report['configuration_version']}`</sub>")
