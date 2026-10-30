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
    extends: Optional[str] = None     # Java: simple name of the superclass (its fields are merged at assembly)


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
class Entity:
    """An ORM-mapped data entity (JPA entity, SQLAlchemy/SQLModel model, Django model) and the table it maps."""
    id: str
    service: str
    repo: str
    name: str
    table: str                  # normalised: lower-case, unqualified
    table_source: str           # declared | naming_strategy | django_default | unresolved
    columns: dict               # column name -> {"field": str, "type": str, "nullable": bool, "pk": bool}
    kind: str                   # jpa | sqlalchemy | django
    file: str
    line: int
    bases: list = field(default_factory=list)       # superclasses / mixins whose columns are merged at assembly
    abstract: bool = False      # @MappedSuperclass / abstract model / mixin: contributes columns, maps no table
    app: Optional[str] = None   # Django app label (table prefix)
    joins: list = field(default_factory=list)       # owning relationships whose column name depends on the target's key
    embedded: list = field(default_factory=list)    # @Embedded types merged at assembly
    join_tables: list = field(default_factory=list)  # [{"table", "columns": {name: {...}}}] many-to-many link tables
    notes: list = field(default_factory=list)       # why a mapping is approximate


@dataclass
class DataAccess:
    """A function that reads or writes an entity through the ORM."""
    function_id: str
    entity: str                 # Entity.id
    op: str                     # read | write
    file: str
    line: int
    via: str                    # e.g. OwnerRepository.save, Order.objects.filter, session.query


@dataclass
class Migration:
    """One versioned schema-migration file and the structural operations it declares."""
    id: str
    repo: str
    service: str
    file: str
    tool: str                   # flyway | liquibase | alembic | django | sql
    version: str
    ops: list = field(default_factory=list)         # [{"op", "table", "column"?, ...}]
    unparsed: list = field(default_factory=list)    # [{"line", "reason", "text"}] statements that could not be analysed
    parent: Optional[str] = None                    # Alembic down_revision


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
    clients: list = field(default_factory=list)            # Java: declared HTTP clients (OpenFeign interfaces)
    typed_calls: list = field(default_factory=list)        # Java: calls on typed variables, resolved at assembly
    unsupported_clients: list = field(default_factory=list)  # files that use HTTP client libraries the adapters do not analyse
    entities: list = field(default_factory=list)           # ORM-mapped data entities
    data_access: list = field(default_factory=list)        # DataAccess, resolved at assembly
    migrations: list = field(default_factory=list)
    data_unknowns: list = field(default_factory=list)      # data access that is declared but not analysed [{file, line, kind, reason}]
    repositories: list = field(default_factory=list)       # Java: Spring Data repository declarations (assembly input)
    data_refs: list = field(default_factory=list)          # Python: model references in functions (assembly input)

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
            contracts=d["contracts"], service_map=d["service_map"],
            clients=d.get("clients", []), typed_calls=d.get("typed_calls", []),
            unsupported_clients=d.get("unsupported_clients", []),
            entities=[Entity(**e) for e in d.get("entities", [])],
            data_access=[DataAccess(**a) for a in d.get("data_access", [])],
            migrations=[Migration(**m) for m in d.get("migrations", [])],
            data_unknowns=d.get("data_unknowns", []), repositories=d.get("repositories", []),
            data_refs=d.get("data_refs", []))


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
