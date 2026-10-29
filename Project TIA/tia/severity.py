"""Severity rules (PRD 7.3). Deterministic, versioned, and recorded per finding.

Inputs: nature of the change on the path (breaking / non-breaking / unclassified) and the
criticality of the affected component. Criticality alone never produces Critical.
A finding reached only through inferred relationships is capped at Medium.
"""
from __future__ import annotations

from .models import CONFIRMED, SEVERITY_ORDER


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
        cap = "medium"
        if SEVERITY_ORDER.index(sev) > SEVERITY_ORDER.index(cap):
            return cap, "SEV-INFERRED-CAP"
        return sev, "SEV-INFERRED-CAP"
    return sev, rule


def is_material(severity: str, breaking_on_path: bool) -> bool:
    return severity in ("critical", "high", "medium") or breaking_on_path
