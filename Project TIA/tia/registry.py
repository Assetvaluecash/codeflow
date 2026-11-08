"""Ownership and criticality registry connector (PRD 7.3 criticality, FR-033 owner approval, FR-045 owning teams).

Imports the service catalog an organisation already keeps, into the tenant configuration (as a new, audited, versioned configuration):
  * Backstage catalog descriptors (catalog-info.yaml, one or many YAML documents): kind Component; metadata.name; spec.owner; criticality
    from the annotation tia/criticality, or from spec.tier / metadata.labels.tier (tier-1 critical, tier-2 high, tier-3 medium, tier-4 low);
  * CSV with the columns service, owner, criticality;
  * JSON: [{"service", "owner", "criticality"}].
A repository's own catalog-info.yaml is also read during extraction as a fallback for its tia-services.yaml (which wins when both say).
"""
from __future__ import annotations

import csv
import io
import json
import re

import yaml

TIERS = {"tier-1": "critical", "tier1": "critical", "1": "critical", "tier-2": "high", "tier2": "high", "2": "high",
         "tier-3": "medium", "tier3": "medium", "3": "medium", "tier-4": "low", "tier4": "low", "4": "low"}
LEVELS = ("critical", "high", "medium", "low", "none")
MAX_ENTRIES = 5000


class RegistryError(ValueError):
    pass


def _owner(v) -> str:
    v = str(v or "").strip()
    return re.sub(r"^(group|user):(default/)?", "", v)                # Backstage entity references: group:default/team-payments


def _crit(meta: dict, spec: dict):
    ann = (meta.get("annotations") or {}).get("tia/criticality")
    if ann in LEVELS:
        return ann
    tier = str(spec.get("tier") or (meta.get("labels") or {}).get("tier") or "").lower()
    return TIERS.get(tier)


def from_backstage(text: str) -> list:
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as e:
        raise RegistryError(f"not valid YAML: {str(e)[:80]}")
    out = []
    for d in docs:
        if d.get("kind") not in ("Component", "API", "System"):
            continue
        meta, spec = d.get("metadata") or {}, d.get("spec") or {}
        if not meta.get("name"):
            continue
        out.append({"service": str(meta["name"]), "owner": _owner(spec.get("owner")) or None, "criticality": _crit(meta, spec)})
    return out


def parse_registry(text: str, fmt: str) -> list:
    """-> [{service, owner, criticality}] (None where the source does not say)."""
    if fmt == "backstage":
        rows = from_backstage(text)
    elif fmt == "csv":
        rows = [{"service": (r.get("service") or "").strip(), "owner": (r.get("owner") or "").strip() or None,
                 "criticality": (r.get("criticality") or "").strip().lower() or None} for r in csv.DictReader(io.StringIO(text))]
    elif fmt == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RegistryError(f"not valid JSON: {e}")
        if not isinstance(data, list):
            raise RegistryError("expected a list of {service, owner, criticality}")
        rows = [{"service": str(r.get("service", "")), "owner": r.get("owner"), "criticality": r.get("criticality")} for r in data if isinstance(r, dict)]
    else:
        raise RegistryError("format must be backstage, csv or json")
    rows = [r for r in rows if r["service"]]
    if not rows:
        raise RegistryError("no service found")
    if len(rows) > MAX_ENTRIES:
        raise RegistryError(f"more than {MAX_ENTRIES} services")
    bad = [r["service"] for r in rows if r["criticality"] and r["criticality"] not in LEVELS]
    if bad:
        raise RegistryError(f"unknown criticality for {', '.join(bad[:5])}; use one of {', '.join(LEVELS)}")
    return rows


def merged_config(current: dict, rows: list) -> dict:
    """The current configuration with the registry's owners and criticalities applied (the registry wins for the services it names)."""
    cfg = json.loads(json.dumps(current))
    cfg.setdefault("criticality", {})
    cfg.setdefault("ownership", {})
    for r in rows:
        if r.get("criticality"):
            cfg["criticality"][r["service"]] = r["criticality"]
        if r.get("owner"):
            cfg["ownership"][r["service"]] = r["owner"]
    return cfg


def catalog_service_map(files: dict) -> dict:
    """A repository's own catalog-info.yaml as a service map fallback: {service, owner, criticality}."""
    text = files.get("catalog-info.yaml") or files.get("catalog-info.yml")
    if not text:
        return {}
    try:
        rows = from_backstage(text)
    except RegistryError:
        return {}
    comp = next((r for r in rows), None)
    return {k: v for k, v in (comp or {}).items() if v} if comp else {}
