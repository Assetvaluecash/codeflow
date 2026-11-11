"""Explainable finding catalog packaging (Pulse RULE_CATALOG shape, TIA content).

Findings keep their severity_rule / inference_rule as the system of record. This catalog
adds a stable rule_id, short explanation, and remediation for report and PR surfaces.
It never invents consumers or changes evidence status.
"""
from __future__ import annotations

from typing import Optional

CATALOG = {
    "SEV-BREAKING-CRITICAL": {
        "explanation": "A breaking change reaches a critical service through confirmed evidence.",
        "remediation": "Confirm consumers, run contract and critical-path tests, and require owner review before merge.",
    },
    "SEV-BREAKING-CONFIRMED": {
        "explanation": "A breaking change reaches a downstream component through confirmed evidence.",
        "remediation": "Run the listed verification and confirm the consumer still compiles and passes contract checks.",
    },
    "SEV-NONBREAKING-CRITICAL": {
        "explanation": "A non-breaking change still reaches a critical service and needs coordinated verification.",
        "remediation": "Notify the owning team and run the recommended tests for the critical path.",
    },
    "SEV-NONBREAKING-CONFIRMED": {
        "explanation": "A confirmed downstream dependency is affected by a non-breaking change.",
        "remediation": "Review the evidence path and run associated tests where listed.",
    },
    "SEV-LOCAL": {
        "explanation": "Impact is confined to the changed repository under the analysed scope.",
        "remediation": "Run local unit and integration tests for the changed symbols.",
    },
    "SEV-UNCLASSIFIED": {
        "explanation": "The change could not be classified at symbol level, so severity stays unknown.",
        "remediation": "Inspect the changed files and re-run analysis after clarifying the symbol-level change.",
    },
    "SEV-INFERRED-CAP": {
        "explanation": "At least one hop on the path is inferred, so severity is capped.",
        "remediation": "Confirm the inferred relationship (service map, client, or contract) before treating it as fact.",
    },
    "SEV-STALE-CAP": {
        "explanation": "A previously confirmed relationship could not be re-verified and is treated as stale.",
        "remediation": "Re-scan the consumer repository or confirm the dependency still exists before relying on it.",
    },
    "SEV-DATA-MAPPING-BROKEN": {
        "explanation": "A migration removes or renames mapped data that this repository still maps.",
        "remediation": "Update the ORM mapping in the same change, or verify the migration against this service.",
    },
    "SEV-DATA-UPDATED": {
        "explanation": "The data mapping was updated in the same change as the migration.",
        "remediation": "Confirm the migration and mapping still agree at deploy time.",
    },
    "INF-CALL-AMBIGUOUS": {
        "explanation": "An intra-repository call matched more than one function by simple name.",
        "remediation": "Disambiguate with imports or unique names; treat these edges as candidates, not confirmed calls.",
    },
    "INF-INHERITS-AMBIGUOUS": {
        "explanation": "A superclass name matched more than one type in the repository.",
        "remediation": "Qualify the base type or ensure a unique class name in the analysed sources.",
    },
    "STRUCT-CYCLE": {
        "explanation": "Changed code sits on a circular dependency among analysed call edges.",
        "remediation": "Treat the whole cycle as review scope; prefer breaking the cycle before a risky merge.",
    },
    "STRUCT-HIGH-COUPLING": {
        "explanation": "A changed file is a high-degree hub on the call graph.",
        "remediation": "Expect wide fan-out; expand review and test scope beyond the immediate callers.",
    },
    "STRUCT-LAYER-VIOLATION": {
        "explanation": "A dependency crosses architectural layers upward (for example utils importing services).",
        "remediation": "Move the shared logic down, or invert the dependency, before widening the change.",
    },
}


def lookup(rule_id: Optional[str]) -> Optional[dict]:
    if not rule_id:
        return None
    entry = CATALOG.get(rule_id)
    if not entry:
        return None
    return {"rule_id": rule_id, "explanation": entry["explanation"], "remediation": entry["remediation"]}


def enrich(finding: dict) -> dict:
    """Attach catalog fields from severity_rule or inference_rule when known. Idempotent."""
    rule = finding.get("severity_rule") or finding.get("inference_rule") or finding.get("rule_id")
    meta = lookup(rule)
    if meta:
        finding.setdefault("rule_id", meta["rule_id"])
        finding.setdefault("explanation", meta["explanation"])
        finding.setdefault("remediation", meta["remediation"])
    return finding


def enrich_all(findings: list) -> list:
    for f in findings:
        enrich(f)
    return findings
