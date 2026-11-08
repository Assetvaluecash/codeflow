"""Severity-rule calibration from validated outcomes (PRD FR-044, AC-038) and change-risk trends (FR-043).

Calibration. Reviewers record a verdict on a finding (confirmed / false_positive / irrelevant); the rules that produced the finding (severity
rule, inference rule, resolution strategy, evidence status, impact type, configuration version) are copied into the verdict when it is
recorded, so a stored report is never read back, re-scored or changed. Incident outcomes count too: a finding about the incident's service in
an analysis of a change that the incident record names, or that a person linked to it, is confirmed by that incident (unless a reviewer
judged it). Per rule: precision = confirmed / (confirmed + false_positive); "irrelevant" is reported apart (the relationship was real but not
worth the attention). Fewer than MIN_SAMPLE judgements is flagged as too few to act on.

Trends. Descriptive figures per application (or team) and period, from stored analyses: change volume, share of analyses with a breaking
change, share with unresolved dependencies, share of material findings without any associated test, and incident associations. They are
counts and shares of the past, never probabilities or predictions.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Optional

VERDICTS = ("confirmed", "false_positive", "irrelevant")
MIN_SAMPLE = 10
DIMENSIONS = ("severity_rule", "inference_rule", "resolution_strategy", "evidence_status", "impact_type", "configuration")
UNRESOLVED_AREAS = ("unresolved_outbound_calls", "stale_relationships", "event_consumers_unknown", "inaccessible_repositories",
                    "procedures_not_found")
TREND_NOTE = "Descriptive counts and shares of past analyses. They are not probabilities and do not predict the outcome of a change."


def rules_of(report: dict, f: dict) -> dict:
    return {"severity_rule": f.get("severity_rule"), "inference_rule": f.get("inference_rule"), "resolution_strategy": f.get("resolution_strategy"),
            "evidence_status": f.get("evidence_status"), "impact_type": f.get("impact_type"), "severity": f.get("severity"),
            "component": f.get("component"), "configuration": report.get("configuration_version")}


def precision(verdicts: list, incident_confirmations: list) -> dict:
    """verdicts: [{analysis_id, finding_id, verdict, rules, at, evidence_id}]; incident_confirmations: [{analysis_id, finding_id, rules,
    incident}]. The latest verdict per finding wins; an incident confirms a finding no reviewer judged."""
    latest = {}
    for v in sorted(verdicts, key=lambda v: (v.get("at") or "", v.get("evidence_id") or 0)):
        latest[(v["analysis_id"], v["finding_id"])] = {**v, "source": "reviewer"}
    for c in incident_confirmations:
        latest.setdefault((c["analysis_id"], c["finding_id"]), {**c, "verdict": "confirmed", "source": "incident"})
    out = {d: defaultdict(lambda: {"confirmed": 0, "false_positive": 0, "irrelevant": 0, "from_incidents": 0}) for d in DIMENSIONS}
    for v in latest.values():
        for d in DIMENSIONS:
            key = (v.get("rules") or {}).get(d)
            if key is None:
                continue
            row = out[d][key]
            row[v["verdict"]] += 1
            if v["source"] == "incident":
                row["from_incidents"] += 1

    def finish(row):
        judged = row["confirmed"] + row["false_positive"] + row["irrelevant"]
        denom = row["confirmed"] + row["false_positive"]
        return {**row, "judged": judged, "precision": round(row["confirmed"] / denom, 3) if denom else None,
                "too_few_to_act_on": judged < MIN_SAMPLE}
    return {"by": {d: {k: finish(r) for k, r in sorted(out[d].items(), key=lambda kv: str(kv[0]))} for d in DIMENSIONS},
            "judged_findings": len(latest), "minimum_sample": MIN_SAMPLE,
            "note": "Precision per rule from reviewer verdicts and incident outcomes. Stored reports are never re-scored: change a rule through "
                    "the tenant configuration, which applies to new analyses only."}


def incident_confirmations(incidents: list, links: list, analyses_for: Callable[[str, str], list]) -> list:
    """Findings confirmed by incidents: for each change an incident names or a person linked, the findings about the incident's service in
    the analyses of that change."""
    out = []
    for inc in incidents:
        refs = [(c.get("repo"), c.get("revision") or c.get("commit")) for c in inc.get("changes") or []]
        refs += [(ln["repo"], ln["revision"]) for ln in links if ln.get("incident_id") == inc["id"]]
        for repo, rev in refs:
            if not repo or not rev:
                continue
            for rep in analyses_for(repo, rev):
                for f in rep.get("findings", []):
                    if f.get("component") == inc.get("service"):
                        out.append({"analysis_id": rep["analysis_id"], "finding_id": f["id"], "rules": rules_of(rep, f), "incident": inc["id"]})
    return out


# ---- trends -----------------------------------------------------------------------------------------------------------------------------
def _period(ts: str, period: str) -> str:
    from datetime import date
    d = date.fromisoformat(ts[:10])
    if period == "month":
        return ts[:7]
    if period == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def trends(reports: list, owners: dict, incident_refs: list, group: str = "application", period: str = "month") -> dict:
    """reports: stored analyses; owners: service -> team; incident_refs: [(repo, revision)] that incidents name or were linked to."""
    inc = defaultdict(int)
    for repo, rev in incident_refs:
        inc[(repo, (rev or "")[:40])] += 1
    rows = defaultdict(lambda: {"analyses": 0, "breaking": 0, "unresolved": 0, "material_findings": 0, "material_without_tests": 0,
                                "incident_associations": 0})
    for r in reports:
        if r.get("status") == "failed" or not r.get("generated_at"):
            continue
        ch = r["change"]
        svc = ch.get("service") or ch["repository"]
        key = (owners.get(svc) or "(no owner)") if group == "team" else svc
        row = rows[(key, _period(r["generated_at"], period))]
        row["analyses"] += 1
        row["breaking"] += int(any(c.get("breaking") for c in ch.get("changed_components", [])))
        row["unresolved"] += int(any(u["area"] in UNRESOLVED_AREAS for u in r.get("unknowns", [])))
        mat = [f for f in r.get("findings", []) if f.get("material")]
        row["material_findings"] += len(mat)
        row["material_without_tests"] += sum(1 for f in mat if not f.get("tests"))
        row["incident_associations"] += inc.get((ch["repository"], (ch.get("proposed_revision") or "")[:40]), 0)
    out = []
    for (key, per), row in sorted(rows.items()):
        n = row["analyses"]
        out.append({group: key, "period": per, "change_volume": n, "breaking_changes": row["breaking"],
                    "breaking_share": round(row["breaking"] / n, 3), "unresolved_dependency_share": round(row["unresolved"] / n, 3),
                    "material_findings": row["material_findings"],
                    "missing_verification_share": round(row["material_without_tests"] / row["material_findings"], 3) if row["material_findings"] else None,
                    "incident_associations": row["incident_associations"]})
    return {"group": group, "period": period, "rows": out, "note": TREND_NOTE}
