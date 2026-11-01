"""Per-tenant configuration, versioned (UC1-24; PRD 7.3 configurable severity, FR-016 configurable limits, PL-003 per-tenant configuration).

    {"limits":      {"max_depth": 8, "max_results": 200, "time_budget_seconds": 60, "max_concurrent_jobs": 4},
     "paths":       {"strip_prefixes": ["/gateway", "/api/v1"]},            # gateway prefixes removed before paths are matched (TFD 5.5)
     "tests":       {"naming_convention": true},                           # associate tests by file-naming convention (labelled inferred)
     "severity":    {"overrides": {"SEV-LOCAL": "medium"}, "inferred_cap": "medium", "stale_cap": "medium"},
     "materiality": {"severities": ["critical", "high", "medium"], "breaking_on_path": true},
     "criticality": {"payments-service": "critical"},                      # wins over the service's own tia-services.yaml
     "comments":    {"audience": "target"}}                               # "target" | "all"; unset = the deployment default

Rules this module keeps:
  * strict: an unknown key, a wrong type or an out-of-range value is rejected with EVERY problem listed (and a "did you mean"), never
    silently ignored - a typo in a severity setting must not quietly leave the default in force;
  * versioned and immutable: every change is a new version (insert-only table); saving an identical configuration is a no-op;
  * deterministic: a report records the exact configuration it was produced under, and reproduction uses THAT, not the current one;
  * the default configuration (nothing stored) leaves every report and snapshot identity exactly as it was before this feature existed;
  * transparent: whatever a configuration changes about a finding is recorded on it (see impact.py), never hidden.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from .models import SEVERITY_ORDER
from .severity import RULE_IDS

SEVERITIES = tuple(SEVERITY_ORDER)                                  # unknown, low, medium, high, critical
CAPS = ("none",) + SEVERITIES[1:]                                   # none removes the cap
CRITICALITIES = ("critical", "high", "medium", "low", "none")
AUDIENCES = ("target", "readers", "all")
BOUNDS = {"max_depth": (1, 50), "max_results": (1, 5000), "time_budget_seconds": (1, 3600), "max_concurrent_jobs": (1, 16)}
SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PREFIXES, MAX_SERVICES, MAX_COMMENT = 50, 2000, 500
SECTIONS = {
    "limits": ("max_depth", "max_results", "time_budget_seconds", "max_concurrent_jobs"),
    "paths": ("strip_prefixes",), "tests": ("naming_convention",), "severity": ("overrides", "inferred_cap", "stale_cap"),
    "materiality": ("severities", "breaking_on_path"), "comments": ("audience",)}
TOP_LEVEL = tuple(SECTIONS) + ("criticality",)


class ConfigError(Exception):
    """Every problem with a submitted configuration, each naming the setting it is about."""

    def __init__(self, problems: list):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class TenantConfig:
    max_depth: int = 8
    max_results: int = 200
    time_budget_seconds: float = 60
    max_concurrent_jobs: int = 4
    strip_prefixes: tuple = ()
    test_naming: bool = True
    severity_overrides: dict = field(default_factory=dict)
    inferred_cap: str = "medium"
    stale_cap: str = "medium"
    material_severities: tuple = ("critical", "high", "medium")
    breaking_is_material: bool = True
    criticality: dict = field(default_factory=dict)
    comment_audience: Optional[str] = None
    version: int = 0                                      # 0 = nothing stored: the built-in defaults
    digest: str = "default"

    # ---- identity ----------------------------------------------------------------------------------------------------
    @property
    def is_default(self) -> bool:
        return self.version == 0

    @property
    def id(self) -> str:
        return "default" if self.is_default else f"tenant-v{self.version}:{self.digest[:8]}"

    def ident(self, rules_version: str) -> str:
        """The configuration identity a report carries: the rules version alone for the defaults (as before), else with the tenant's version."""
        return rules_version if self.is_default else f"{rules_version};{self.id}"

    # ---- (de)serialisation ------------------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        """The full, normalised settings in their user-facing shape (defaults filled in)."""
        return {"limits": {"max_depth": self.max_depth, "max_results": self.max_results,
                           "time_budget_seconds": self.time_budget_seconds, "max_concurrent_jobs": self.max_concurrent_jobs},
                "paths": {"strip_prefixes": list(self.strip_prefixes)}, "tests": {"naming_convention": self.test_naming},
                "severity": {"overrides": dict(sorted(self.severity_overrides.items())), "inferred_cap": self.inferred_cap,
                             "stale_cap": self.stale_cap},
                "materiality": {"severities": list(self.material_severities), "breaking_on_path": self.breaking_is_material},
                "criticality": dict(sorted(self.criticality.items())),
                "comments": {"audience": self.comment_audience}}

    def settings_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def stamped(self, version: int) -> "TenantConfig":
        return replace(self, version=version, digest=self.settings_digest())

    @staticmethod
    def from_stored(settings: dict, version: int, digest: str) -> "TenantConfig":
        return replace(parse(settings), version=version, digest=digest)


DEFAULT = TenantConfig()


def _suggest(key: str, known) -> str:
    near = difflib.get_close_matches(str(key), list(known), n=1, cutoff=0.6)
    return f" (did you mean '{near[0]}'?)" if near else ""


def _int(problems, path, v, lo, hi, allow_float=False):
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and (allow_float or isinstance(v, int) or float(v).is_integer())
    if not ok:
        problems.append(f"{path} must be a number" if allow_float else f"{path} must be a whole number")
        return None
    if not lo <= v <= hi:
        problems.append(f"{path} must be between {lo} and {hi} (got {v})")
        return None
    return v if allow_float else int(v)


def parse(raw) -> TenantConfig:
    """Validate a submitted configuration (missing settings keep their defaults) -> TenantConfig. Raises ConfigError listing every problem."""
    problems: list = []
    if not isinstance(raw, dict):
        raise ConfigError(["the configuration must be a JSON object"])
    for k in raw:
        if k not in TOP_LEVEL:
            problems.append(f"unknown setting '{k}'{_suggest(k, TOP_LEVEL)}")
    kw: dict = {}

    def section(name):
        v = raw.get(name)
        if v is None:
            return {}
        if not isinstance(v, dict):
            problems.append(f"{name} must be an object")
            return {}
        for k in v:
            if k not in SECTIONS.get(name, ()):
                problems.append(f"unknown setting '{name}.{k}'{_suggest(k, SECTIONS.get(name, ()))}")
        return v
    lim = section("limits")
    for key, (lo, hi) in BOUNDS.items():
        if key in lim:
            got = _int(problems, f"limits.{key}", lim[key], lo, hi, allow_float=key == "time_budget_seconds")
            if got is not None:
                kw[key] = got
    paths = section("paths")
    if "strip_prefixes" in paths:
        pv = paths["strip_prefixes"]
        out = []
        if not isinstance(pv, list) or not all(isinstance(x, str) for x in pv):
            problems.append("paths.strip_prefixes must be a list of strings")
        elif len(pv) > MAX_PREFIXES:
            problems.append(f"paths.strip_prefixes may hold at most {MAX_PREFIXES} prefixes")
        else:
            for x in pv:
                p = x.strip().rstrip("/").lower()
                if not p.startswith("/") or len(p) < 2 or len(p) > 200 or re.search(r"[\s{}?#]", p):
                    problems.append(f"paths.strip_prefixes: '{x}' is not a path prefix (it must start with '/', e.g. '/api/v1', and contain "
                                    "no spaces, braces, '?' or '#')")
                elif p in out:
                    problems.append(f"paths.strip_prefixes: '{x}' is listed twice")
                else:
                    out.append(p)
            kw["strip_prefixes"] = tuple(out)
    tests = section("tests")
    if "naming_convention" in tests:
        if isinstance(tests["naming_convention"], bool):
            kw["test_naming"] = tests["naming_convention"]
        else:
            problems.append("tests.naming_convention must be true or false")
    sev = section("severity")
    if "overrides" in sev:
        ov, out = sev["overrides"], {}
        if not isinstance(ov, dict):
            problems.append("severity.overrides must be an object of rule id -> severity")
        else:
            for rule, level in ov.items():
                if rule not in RULE_IDS:
                    problems.append(f"severity.overrides: unknown rule '{rule}'{_suggest(rule, RULE_IDS)}; rules are {', '.join(RULE_IDS)}")
                elif level not in SEVERITIES:
                    problems.append(f"severity.overrides.{rule} must be one of {', '.join(SEVERITIES)} (got {level!r})")
                else:
                    out[rule] = level
            kw["severity_overrides"] = out
    for key, attr in (("inferred_cap", "inferred_cap"), ("stale_cap", "stale_cap")):
        if key in sev:
            if sev[key] in CAPS:
                kw[attr] = sev[key]
            else:
                problems.append(f"severity.{key} must be one of {', '.join(CAPS)} (got {sev[key]!r}); 'none' removes the cap")
    mat = section("materiality")
    if "severities" in mat:
        sv = mat["severities"]
        if not isinstance(sv, list) or not all(x in SEVERITIES for x in sv) or len(set(sv)) != len(sv):
            problems.append(f"materiality.severities must be a list of distinct severities from {', '.join(SEVERITIES)}")
        else:
            kw["material_severities"] = tuple(sorted(sv, key=SEVERITIES.index, reverse=True))
    if "breaking_on_path" in mat:
        if isinstance(mat["breaking_on_path"], bool):
            kw["breaking_is_material"] = mat["breaking_on_path"]
        else:
            problems.append("materiality.breaking_on_path must be true or false")
    crit = raw.get("criticality")
    if crit is not None:
        if not isinstance(crit, dict):
            problems.append("criticality must be an object of service name -> level")
        elif len(crit) > MAX_SERVICES:
            problems.append(f"criticality may name at most {MAX_SERVICES} services")
        else:
            out = {}
            for svc, level in crit.items():
                if not SERVICE_NAME.match(str(svc)):
                    problems.append(f"criticality: '{svc}' is not a valid service name")
                elif level not in CRITICALITIES:
                    problems.append(f"criticality.{svc} must be one of {', '.join(CRITICALITIES)} (got {level!r})")
                else:
                    out[svc] = level            # 'none' is kept: it removes a criticality the service declared for itself
            kw["criticality"] = out
    com = section("comments")
    if "audience" in com:
        if com["audience"] is None or com["audience"] in AUDIENCES:
            kw["comment_audience"] = com["audience"]
        else:
            problems.append(f"comments.audience must be one of {', '.join(AUDIENCES)} or null (got {com['audience']!r})")
    if problems:
        raise ConfigError(problems)
    return TenantConfig(**kw)


def diff(a: dict, b: dict) -> list:
    """Setting-by-setting differences between two normalised configurations: [{path, from, to}] (None = not set)."""
    out = []

    def walk(x, y, path):
        if isinstance(x, dict) or isinstance(y, dict):
            x, y = x if isinstance(x, dict) else {}, y if isinstance(y, dict) else {}
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{path}.{k}" if path else k)
        elif x != y:
            out.append({"path": path, "from": x, "to": y})
    walk(a, b, "")
    return out


# ---- persistence (shared by the runtime Store and the admin CLI; the caller supplies a tenant-bound or admin connection) --------------------
def latest(conn, tenant_id: str) -> TenantConfig:
    row = conn.execute("select version, config, config_hash from tenant_config_versions where tenant_id = %s order by version desc limit 1",
                       (tenant_id,)).fetchone()
    return TenantConfig.from_stored(row[1], row[0], row[2]) if row else DEFAULT


def get_version(conn, tenant_id: str, version: int) -> Optional[dict]:
    r = conn.execute("select version, config, config_hash, comment, actor, created_at from tenant_config_versions "
                     "where tenant_id = %s and version = %s", (tenant_id, version)).fetchone()
    return _meta(r) if r else None


def list_versions(conn, tenant_id: str, limit: int = 50) -> list:
    return [_meta(r, with_config=False) for r in conn.execute(
        "select version, config, config_hash, comment, actor, created_at from tenant_config_versions where tenant_id = %s "
        "order by version desc limit %s", (tenant_id, limit))]


def _meta(r, with_config: bool = True) -> dict:
    d = {"version": r[0], "id": f"tenant-v{r[0]}:{r[2][:8]}", "hash": r[2], "comment": r[3], "actor": r[4], "created_at": r[5].isoformat(timespec="seconds")}
    return {**d, "config": r[1]} if with_config else d


def put_version(conn, tenant_id: str, raw: dict, comment: Optional[str] = None, actor: Optional[str] = None) -> dict:
    """Validate and store a new version. Identical to the current version -> no new version (created False). Raises ConfigError."""
    cfg = parse(raw)
    from .scrub import scrub_text
    from psycopg.types.json import Jsonb
    settings, digest = cfg.to_dict(), cfg.settings_digest()
    comment = scrub_text((comment or "").strip()[:MAX_COMMENT], True)[0] or None
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 11))", (str(tenant_id),))
    last = conn.execute("select version, config, config_hash from tenant_config_versions where tenant_id = %s order by version desc limit 1",
                        (tenant_id,)).fetchone()
    if last and last[2] == digest:
        return {"version": last[0], "created": False, "id": f"tenant-v{last[0]}:{digest[:8]}", "changes": []}
    changes = diff(last[1] if last else DEFAULT.to_dict(), settings)
    version = (last[0] if last else 0) + 1
    conn.execute("insert into tenant_config_versions (tenant_id, version, config, config_hash, comment, actor) values (%s, %s, %s, %s, %s, %s)",
                 (tenant_id, version, Jsonb(settings), digest, comment, (actor or "")[:64] or None))
    return {"version": version, "created": True, "id": f"tenant-v{version}:{digest[:8]}", "changes": changes}
