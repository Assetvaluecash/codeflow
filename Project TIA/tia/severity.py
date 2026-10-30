"""Severity rules (PRD 7.3). Deterministic, versioned, and recorded per finding.

Inputs: nature of the change on the path (breaking / non-breaking / unclassified) and the
criticality of the affected component. Criticality alone never produces Critical.
A finding reached only through inferred or stale relationships is capped at Medium.
"""
from __future__ import annotations

from .models import CONFIRMED, SEVERITY_ORDER, STALE


def compute(change_kind: str, status: str, cross_repo: bool, criticality: str | None) -> tuple[str, str]:
    """change_kind: 'breaking' | 'nonbreaking' | 'unclassified'.  -> (severity, rule_id)"""
    if change_kind == "unclassified":
        return "unknown", "SEV-UNCLASSIFIED"
    confirmed = status in CONFIRMED
    critical = criticality == "critical"
    if not cross_repo:
        return "low", "SEV-LOCAL"
    if change_kind == "breaking":
        sev, rule = (("critical", "SEV-BREAKING-CRITICAL") if critical else ("high", "SEV-BREAKING-CONFIRMED"))
    else:
        sev, rule = (("high", "SEV-NONBREAKING-CRITICAL") if critical else ("medium", "SEV-NONBREAKING-CONFIRMED"))
    if not confirmed:
        cap, cap_rule = "medium", ("SEV-STALE-CAP" if status == STALE else "SEV-INFERRED-CAP")
        if SEVERITY_ORDER.index(sev) > SEVERITY_ORDER.index(cap):
            return cap, cap_rule
        return sev, cap_rule
    return sev, rule


def compute_data(change_kind: str, status: str, same_repo: bool, fixed_in_change: bool, structural: bool,
                 criticality: str | None) -> tuple[str, str]:
    """Severity of an ORM-mapped entity affected by a schema change.

    * the entity's mapping is already updated by the same change              -> low  (SEV-DATA-UPDATED)
    * the changed repository still maps something its own migration removes   -> high (SEV-DATA-MAPPING-BROKEN)
    * the changed repository keeps a mapping across a type/nullability change  -> low  (SEV-LOCAL: the author owns both sides)
    * any other repository sharing the database                               -> the cross-repository rules in compute()
    """
    if fixed_in_change:
        return "low", "SEV-DATA-UPDATED"
    if same_repo:
        if change_kind == "breaking" and structural:
            return "high", "SEV-DATA-MAPPING-BROKEN"
        return "low", "SEV-LOCAL"
    return compute(change_kind, status, True, criticality)


def is_material(severity: str, breaking_on_path: bool) -> bool:
    return severity in ("critical", "high", "medium") or breaking_on_path
