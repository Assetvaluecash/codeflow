"""Incident association and historical investigation (PRD FR-031, FR-032, AC-034).

An incident is recorded evidence ({id, title, service, environment, started_at, ended_at, changes: [recorded change references]}). For it,
TIA lists candidate changes, each labelled with HOW it is associated - never as the cause:
  directly_recorded    the incident record itself names the change (a PR, a commit, a change record)
  time_correlated      a deployment to the incident's service or environment shortly before the incident started (window configurable)
  dependency_derived   a deployment of a service the incident's service depends on (from the dependency graph), shortly before
  manually_validated   a person linked the change to the incident (recorded with who and when)
The output states explicitly that no candidate is a root cause: association is evidence for an investigation, not a conclusion.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .testscope import dependencies

ASSOCIATIONS = ("directly_recorded", "time_correlated", "dependency_derived", "manually_validated")
NOTE = ("These are candidate changes associated with the incident by the stated evidence. TIA does not determine root cause: none of them is "
        "labelled or ranked as the cause.")


def _ts(v) -> datetime:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_incident(d: dict) -> dict:
    if not isinstance(d, dict) or not d.get("id") or not d.get("started_at") or not (d.get("service") or d.get("repo")):
        raise ValueError("an incident needs id, started_at and service (or repo)")
    out = {"id": str(d["id"])[:80], "title": str(d.get("title") or "")[:300], "service": d.get("service"), "repo": d.get("repo"),
           "environment": (d.get("environment") or "production").lower(), "started_at": _ts(d["started_at"]).isoformat(timespec="seconds"),
           "ended_at": _ts(d["ended_at"]).isoformat(timespec="seconds") if d.get("ended_at") else None, "severity": d.get("severity"),
           "changes": [c for c in (d.get("changes") or []) if isinstance(c, dict)][:50]}
    return out


def investigate(incident: dict, deployments: list, links: list, g, extracts: dict, window_hours: float = 72.0,
                analyses_for: Optional[callable] = None) -> dict:
    """Candidate changes for an incident with their association type(s). deployments: evidence records of kind deployment; links:
    manual links [{incident_id, repo, revision, by, at, note}]; analyses_for(repo, revision) -> [analysis ids] when available."""
    start = _ts(incident["started_at"])
    since = start - timedelta(hours=window_hours)
    svc_repo = {ex.service: r for r, ex in extracts.items()}
    target_repo = incident.get("repo") or svc_repo.get(incident.get("service") or "")
    target_svc = incident.get("service") or (extracts[target_repo].service if target_repo in extracts else None)
    # services the incident's service depends on (providers it calls, topics it consumes from), from the dependency graph
    upstream = set()
    if g is not None and target_svc:
        for n, a in g.nodes.items():
            if a.get("type") == "endpoint" and a.get("service") == target_svc:
                upstream |= {g.nodes[x].get("service") for x in dependencies(g, n) if g.nodes.get(x, {}).get("service")}
        upstream.discard(target_svc)
    cands: dict = {}

    def add(repo, revision, assoc, evidence):
        k = (repo, (revision or "")[:40])
        c = cands.setdefault(k, {"repo": repo, "revision": revision, "associations": [], "evidence": []})
        if assoc not in c["associations"]:
            c["associations"].append(assoc)
        c["evidence"].append(evidence)
    for ch in incident.get("changes") or []:
        add(ch.get("repo") or target_repo, ch.get("revision") or ch.get("commit"), "directly_recorded",
            {"recorded": {k: ch[k] for k in ("pull_request", "change_record", "revision", "commit", "repo") if k in ch}})
    for r in deployments:
        d = r["data"]
        repo = d.get("repo") or svc_repo.get(d.get("service") or "")
        svc = d.get("service") or (extracts[repo].service if repo in extracts else None)
        try:
            at = _ts(d["deployed_at"])
        except (KeyError, ValueError):
            continue
        if not (since <= at <= start):
            continue
        ev = {"deployment": {"environment": d["environment"], "deployed_at": d["deployed_at"], "revision": d["revision"], "evidence_id": r["id"]},
              "hours_before_incident": round((start - at).total_seconds() / 3600, 1)}
        if (repo == target_repo or svc == target_svc) and d["environment"] == incident["environment"]:
            add(repo, d["revision"], "time_correlated", ev)
        elif svc in upstream and d["environment"] == incident["environment"]:
            add(repo, d["revision"], "dependency_derived", {**ev, "because": f"{target_svc} depends on {svc}"})
    for ln in links:
        if ln.get("incident_id") == incident["id"]:
            add(ln.get("repo"), ln.get("revision"), "manually_validated", {"by": ln.get("by"), "at": ln.get("at"), "note": ln.get("note")})
    out = []
    for c in cands.values():
        if analyses_for and c["repo"] and c["revision"]:
            c["analyses"] = analyses_for(c["repo"], c["revision"])
        c["associations"] = [a for a in ASSOCIATIONS if a in c["associations"]]
        out.append(c)
    out.sort(key=lambda c: (ASSOCIATIONS.index(c["associations"][0]), c["repo"] or "", c["revision"] or ""))
    return {"incident": incident, "window_hours": window_hours, "upstream_services": sorted(x for x in upstream if x),
            "candidates": out, "root_cause": None, "note": NOTE}
