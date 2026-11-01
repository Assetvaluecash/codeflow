"""Severity rules (PRD 7.3). Deterministic, versioned, and recorded per finding.

Inputs: nature of the change on the path (breaking / non-breaking / unclassified) and the
criticality of the affected component. Criticality alone never produces Critical.
A finding reached only through inferred or stale relationships is capped at Medium - unless the tenant's policy sets another cap
(PRD: "unless the configured policy explicitly overrides the cap").

The rules are the defaults; a tenant's configuration (tenantconfig.TenantConfig, passed as ``policy``) may change the inferred/stale cap,
override the severity a rule produces, and choose which severities are material. Anything the policy changes is recorded on the finding,
never silent. ``policy=None`` means the defaults.
"""
from __future__ import annotations

from typing import Optional

from .models import CONFIRMED, SEVERITY_ORDER, STALE

RULE_IDS = ("SEV-BREAKING-CRITICAL", "SEV-BREAKING-CONFIRMED", "SEV-NONBREAKING-CRITICAL", "SEV-NONBREAKING-CONFIRMED", "SEV-LOCAL",
            "SEV-UNCLASSIFIED", "SEV-INFERRED-CAP", "SEV-STALE-CAP", "SEV-DATA-MAPPING-BROKEN", "SEV-DATA-UPDATED")


def _rank(sev: str) -> int:
    return SEVERITY_ORDER.index(sev)


def compute(change_kind: str, status: str, cross_repo: bool, criticality: Optional[str], policy=None) -> tuple[str, str]:
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
        cap = getattr(policy, "stale_cap" if status == STALE else "inferred_cap", "medium")
        if cap == "none":
            return sev, rule                                    # the tenant removed the cap: the finding is rated by the rules alone
        cap_rule = "SEV-STALE-CAP" if status == STALE else "SEV-INFERRED-CAP"
        if _rank(sev) > _rank(cap):
            return cap, cap_rule
        return sev, cap_rule
    return sev, rule


def compute_data(change_kind: str, status: str, same_repo: bool, fixed_in_change: bool, structural: bool,
                 criticality: Optional[str], policy=None) -> tuple[str, str]:
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
    return compute(change_kind, status, True, criticality, policy)


def apply_override(severity: str, rule: str, policy=None) -> tuple[str, Optional[dict]]:
    """The tenant's configured severity for a rule, if any. -> (severity, None | {rule, default, configured}); recorded on the finding."""
    configured = (getattr(policy, "severity_overrides", None) or {}).get(rule)
    if configured and configured != severity:
        return configured, {"rule": rule, "default": severity, "configured": configured}
    return severity, None


def is_material(severity: str, breaking_on_path: bool, policy=None) -> bool:
    """PRD 7.4: Critical, High or Medium, or any finding on a path with a breaking change - unless the tenant tunes the thresholds."""
    material = getattr(policy, "material_severities", ("critical", "high", "medium"))
    return severity in material or (breaking_on_path and getattr(policy, "breaking_is_material", True))
