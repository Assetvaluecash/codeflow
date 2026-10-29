"""Canonical data structures (TECHNICAL-FRAMEWORK.md section 5)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Evidence statuses (TFD 5.3)
SOURCE_CONFIRMED = "source_confirmed"
CONTRACT_CONFIRMED = "contract_confirmed"
RUNTIME_OBSERVED = "runtime_observed"
CORROBORATED = "corroborated"
INFERRED = "inferred"
UNKNOWN = "unknown"
STALE = "stale"

# Statuses that count as confirmed (anything except inferred/unknown/stale)
CONFIRMED = {SOURCE_CONFIRMED, CONTRACT_CONFIRMED, RUNTIME_OBSERVED, CORROBORATED}

# Analysis statuses (TFD 7.3)
RUNNING, COMPLETE, COMPLETE_WITH_GAPS, PARTIAL, FAILED = (
    "running", "complete", "complete_with_gaps", "partial", "failed")

SEVERITY_ORDER = ["unknown", "low", "medium", "high", "critical"]


@dataclass
class Endpoint:
    id: str
    service: str
    repo: str
    method: str
    path: str
    norm_path: str
    handler: str            # function qualname
    handler_id: str
    file: str
    line: int
    response_schema: Optional[str] = None
    request_schemas: list = field(default_factory=list)


@dataclass
class Schema:
    id: str
    service: str
    repo: str
    name: str
    fields: dict            # name -> {"type": str, "required": bool}
    file: str
    line: int


@dataclass
class CallSite:
    id: str
    repo: str
    service: str
    file: str
    line: int
    function_id: Optional[str]
    method: Optional[str]
    norm_path: Optional[str]      # None when the path could not be determined
    base_kind: Optional[str]      # config_key | host | literal_url | None
    base_value: Optional[str]
    client_pkg: Optional[str] = None
    client_attr: Optional[str] = None
    reason: Optional[str] = None  # why path/base is unknown


@dataclass
class Function:
    id: str
    repo: str
    file: str
    qualname: str
    line: int
    hash: str
    calls: list = field(default_factory=list)


@dataclass
class TestCase:
    id: str
    repo: str
    file: str
    name: str
    line: int
    imports: list = field(default_factory=list)       # module names
    called: list = field(default_factory=list)        # simple names called
    http_calls: list = field(default_factory=list)    # [(method, norm_path)]


@dataclass
class Edge:
    src: str
    rel: str
    dst: str
    status: str
    strategy: Optional[str] = None
    rule: Optional[str] = None
    evidence: dict = field(default_factory=dict)


@dataclass
class RepoExtract:
    repo: str
    service: str
    commit: str
    extractor_version: str
    files_analysed: int = 0
    files_failed: list = field(default_factory=list)       # [{file, error}]
    non_python_files: int = 0
    endpoints: list = field(default_factory=list)
    schemas: list = field(default_factory=list)
    callsites: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    tests: list = field(default_factory=list)
    contracts: list = field(default_factory=list)          # pact-like dicts
    service_map: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RepoExtract":
        return RepoExtract(
            repo=d["repo"], service=d["service"], commit=d["commit"],
            extractor_version=d["extractor_version"],
            files_analysed=d["files_analysed"], files_failed=d["files_failed"],
            non_python_files=d.get("non_python_files", 0),
            endpoints=[Endpoint(**e) for e in d["endpoints"]],
            schemas=[Schema(**s) for s in d["schemas"]],
            callsites=[CallSite(**c) for c in d["callsites"]],
            functions=[Function(**f) for f in d["functions"]],
            tests=[TestCase(**t) for t in d["tests"]],
            contracts=d["contracts"], service_map=d["service_map"])


def normalize_path(path: str, strip_prefixes: tuple = ()) -> str:
    """TFD 5.5 path-template normalisation."""
    import re
    p = path.split("?", 1)[0]
    p = re.sub(r"\$\{[^}]*\}|\{[^}]*\}|<[^>]*>", "{}", p)
    p = re.sub(r"(?<=/):[A-Za-z_][A-Za-z0-9_]*", "{}", p)
    parts = [seg if seg == "{}" else seg.lower() for seg in p.split("/")]
    p = "/".join(parts)
    for pre in strip_prefixes:
        if p.startswith(pre):
            p = p[len(pre):]
            break
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"
