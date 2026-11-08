"""Report filters (PRD 9.3): severity; service or repository; direct versus transitive impact; contract, data, test, runtime and operational
impact; evidence status; environment; policy result; unresolved or stale evidence. Every filter is optional; values within one filter are
alternatives (OR) and different filters must all hold (AND). Used by GET /api/v1/impact-analyses/{aid}/findings and by the web interface."""
from __future__ import annotations

from typing import Optional

KINDS = ("contract", "data", "test", "runtime", "operational")
REACH = ("direct", "transitive")
ISSUES = ("unresolved", "stale", "inferred")


def reach_of(f: dict) -> str:
    """direct: the component depends on the changed element itself (a call, a consumer, a mapping, a function in the changed repository);
    transitive: reached through another component first."""
    return "transitive" if f.get("impact_type") == "transitive" or (f.get("depth") or 0) > 3 else "direct"


def kinds_of(f: dict, report: dict) -> set:
    out = set()
    if f.get("impact_type") in ("contract", "event"):
        out.add("contract")
    if f.get("impact_type") == "data" or f.get("data"):
        out.add("data")
    if f.get("tests"):
        out.add("test")
    if f.get("runtime") or f.get("evidence_status") in ("runtime_observed", "corroborated"):
        out.add("runtime")
    if ((report.get("deployments") or {}).get("by_repo") or {}).get(f.get("repository")):
        out.add("operational")                                   # the component is deployed somewhere: a change reaches a running system
    return out


def environments_of(f: dict, report: dict) -> set:
    return set((((report.get("deployments") or {}).get("by_repo") or {}).get(f.get("repository")) or {}).keys()) | \
        set((f.get("runtime") or {}).get("environments") or [])


def policy_of(f: dict, report: dict) -> set:
    """The outcomes of the policy rules that name this finding (a rule with no finding list applies to the whole change)."""
    out = set()
    for r in report.get("policy_results") or []:
        named = {e.get("finding") for e in r.get("evidence") or [] if isinstance(e, dict)}
        if f.get("id") in named or not any(named):
            out.add(r.get("outcome"))
    return out - {None}


def issues_of(f: dict) -> set:
    out = set()
    st = f.get("evidence_status")
    if st == "stale":
        out.add("stale")
    if st == "inferred":
        out |= {"inferred", "unresolved"}
    if any("not analysed" in x or "unresolved" in x.lower() or "No relevant test evidence" in x for x in f.get("limitations") or []):
        out.add("unresolved")
    return out


def _vals(v) -> Optional[set]:
    if v is None or v == "" or v == []:
        return None
    items = v if isinstance(v, (list, tuple, set)) else str(v).split(",")
    return {str(x).strip().lower() for x in items if str(x).strip()}


def apply(report: dict, severity=None, service=None, reach=None, kind=None, evidence=None, environment=None, policy=None,
          issue=None) -> dict:
    """-> {"findings": [...], "total", "matched", "filters"}."""
    flt = {"severity": _vals(severity), "service": _vals(service), "reach": _vals(reach), "kind": _vals(kind), "evidence": _vals(evidence),
           "environment": _vals(environment), "policy": _vals(policy), "issue": _vals(issue)}
    bad = [f"reach must be {'/'.join(REACH)}" for _ in [0] if flt["reach"] and flt["reach"] - set(REACH)]
    bad += [f"kind must be one of {', '.join(KINDS)}" for _ in [0] if flt["kind"] and flt["kind"] - set(KINDS)]
    bad += [f"issue must be one of {', '.join(ISSUES)}" for _ in [0] if flt["issue"] and flt["issue"] - set(ISSUES)]
    if bad:
        raise ValueError("; ".join(bad))
    out = []
    for f in report.get("findings", []):
        if flt["severity"] and f.get("severity") not in flt["severity"]:
            continue
        if flt["service"] and not ({str(f.get("component", "")).lower(), str(f.get("repository", "")).lower()} & flt["service"]):
            continue
        if flt["reach"] and reach_of(f) not in flt["reach"]:
            continue
        if flt["kind"] and not (kinds_of(f, report) & flt["kind"]):
            continue
        if flt["evidence"] and f.get("evidence_status") not in flt["evidence"]:
            continue
        if flt["environment"] and not ({e.lower() for e in environments_of(f, report)} & flt["environment"]):
            continue
        if flt["policy"] and not (policy_of(f, report) & flt["policy"]):
            continue
        if flt["issue"] and not (issues_of(f) & flt["issue"]):
            continue
        out.append(f)
    return {"findings": out, "total": len(report.get("findings", [])), "matched": len(out),
            "filters": {k: sorted(v) for k, v in flt.items() if v}}
