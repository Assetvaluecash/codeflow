"""Data layer of the graph and data-change detection (FR-006, FR-006a, FR-015, AC-024).

Graph nodes     entity (an ORM-mapped class), table, column, migration
Graph edges     entity -MAPS_TO-> table, entity -MAPS_COLUMN-> column, function -READS|WRITES-> entity,
                migration -MIGRATES-> table
Table identity  a table belongs to a database. A service's database is the ``database:`` its owner declares in
                tia-services.yaml or the one named by a literal spring.datasource.url; without either the database is private to
                the service. Two services map the same table only when they name the same database - table names alone never
                link services (the same rule as for HTTP: names alone are not evidence).
Data changes    the structural operations a proposed revision adds to a repository's migrations, classified against the
                schema built by replaying the repository's earlier migrations.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Optional

from .models import SOURCE_CONFIRMED, Edge, RepoExtract

ADDITIVE = ("table_added", "column_added", "column_made_nullable", "column_type_widened")


def database_of(ex: RepoExtract) -> str:
    db = (ex.service_map or {}).get("database")
    return str(db).lower() if db else f"private:{ex.service}"


def database_declared(ex: RepoExtract) -> bool:
    return bool((ex.service_map or {}).get("database"))


def table_id(db: str, table: str) -> str:
    return f"table:{db}:{table}"


def col_id(db: str, table: str, col: str) -> str:
    return f"col:{db}:{table}.{col}"


# =====================================================================================================================
# Schema replay
# =====================================================================================================================
def _version_key(m) -> tuple:
    if m.version == "R":
        return (1, (), m.file)
    nums = tuple(int(x) for x in re.findall(r"\d+", m.version))
    return (0, nums, m.file)


def order_migrations(migrations: list) -> list:
    """Migrations in the order they apply: Flyway/Django by version number, Alembic along its revision chain, Liquibase and
    plain SQL by file name."""
    out, alembic = [], [m for m in migrations if m.tool == "alembic"]
    for tool in ("flyway", "django"):
        out += sorted((m for m in migrations if m.tool == tool), key=_version_key)
    for tool in ("sql", "liquibase"):
        out += sorted((m for m in migrations if m.tool == tool), key=lambda m: m.file)
    revs = {m.version for m in alembic}
    children = defaultdict(list)
    for m in sorted(alembic, key=lambda m: m.file):
        children[m.parent if m.parent in revs else None].append(m)
    seen: set = set()

    def walk(parent):
        for m in children.get(parent, []):
            if m.id not in seen:
                seen.add(m.id)
                out.append(m)
                walk(m.version)
    walk(None)
    out += [m for m in sorted(alembic, key=lambda m: m.file) if m.id not in seen]
    return out


def apply_op(state: dict, op: dict) -> None:
    """Apply one structural operation to {table: {column: spec}}."""
    t, kind = op.get("table"), op["op"]
    if kind == "create_table":
        state[t] = {c: dict(s) for c, s in (op.get("columns") or {}).items()}
    elif kind == "drop_table":
        state.pop(t, None)
    elif kind == "rename_table":
        if t in state:
            state[op["new_table"]] = state.pop(t)
    elif t in state:
        cols = state[t]
        c = op.get("column")
        if kind == "add_column":
            cols[c] = {k: op.get(k) for k in ("type", "nullable", "pk") if k in op}
        elif kind == "drop_column":
            cols.pop(c, None)
        elif kind == "rename_column":
            if c in cols:
                cols[op["new_column"]] = cols.pop(c)
        elif kind == "alter_column" and c in cols:
            cols[c] = {**cols[c], **{k: op[k] for k in ("type", "nullable") if k in op}}


def schema_state(migrations: list) -> dict:
    state: dict = {}
    for m in order_migrations(migrations):
        for op in m.ops:
            apply_op(state, op)
    return state


# =====================================================================================================================
# Graph layer
# =====================================================================================================================
def add_data_layer(g, extracts: dict) -> None:
    # A table/column is owned by the repository whose migrations create it, else by the first entity that maps it.
    owners: dict = {}
    for repo, ex in sorted(extracts.items()):
        for m in order_migrations(ex.migrations):
            for o in m.ops:
                if o["op"] == "create_table":
                    owners.setdefault((database_of(ex), o["table"]), (repo, m.file, o["line"]))
    for repo, ex in sorted(extracts.items()):
        for ent in ex.entities:
            for t in [ent.table] + [jt["table"] for jt in ent.join_tables]:
                owners.setdefault((database_of(ex), t), (repo, ent.file, ent.line))

    def table_node(db, table, repo, file, line):
        nid = table_id(db, table)
        if nid not in g.nodes:
            r, f, ln = owners.get((db, table), (repo, file, line))
            g.add_node(nid, type="table", repo=r, service=None, file=f, line=ln, name=table, database=db)
        return nid

    def col_node(db, table, col, repo, file, line):
        nid = col_id(db, table, col)
        if nid not in g.nodes:
            r, f, ln = owners.get((db, table), (repo, file, line))
            g.add_node(nid, type="column", repo=r, service=None, file=f, line=ln, name=f"{table}.{col}", database=db,
                       table=table, column=col)
        return nid
    for repo, ex in sorted(extracts.items()):
        db = database_of(ex)
        ev = lambda file, line: {"repo": repo, "file": file, "line": line, "commit": ex.commit}      # noqa: E731
        for ent in ex.entities:
            g.add_node(ent.id, type="entity", repo=repo, service=ex.service, file=ent.file, line=ent.line, name=ent.name,
                       table=ent.table, database=db, orm=ent.kind)
            tn = table_node(db, ent.table, repo, ent.file, ent.line)
            g.add_edge(Edge(ent.id, "MAPS_TO", tn, SOURCE_CONFIRMED, "orm", None, ev(ent.file, ent.line)))
            for col in sorted(ent.columns):
                g.add_edge(Edge(ent.id, "MAPS_COLUMN", col_node(db, ent.table, col, repo, ent.file, ent.line),
                                SOURCE_CONFIRMED, "orm", None, ev(ent.file, ent.line)))
            for jt in ent.join_tables:
                jn = table_node(db, jt["table"], repo, ent.file, ent.line)
                g.add_edge(Edge(ent.id, "MAPS_TO", jn, SOURCE_CONFIRMED, "orm", None, ev(ent.file, ent.line)))
                for col in sorted(jt["columns"]):
                    g.add_edge(Edge(ent.id, "MAPS_COLUMN", col_node(db, jt["table"], col, repo, ent.file, ent.line),
                                    SOURCE_CONFIRMED, "orm", None, ev(ent.file, ent.line)))
        for a in ex.data_access:
            if a.function_id in g.nodes and a.entity in g.nodes:
                g.add_edge(Edge(a.function_id, "READS" if a.op == "read" else "WRITES", a.entity, SOURCE_CONFIRMED, "orm", None,
                                ev(a.file, a.line)))
        if ex.migrations:
            for t, cols in sorted(schema_state(ex.migrations).items()):
                first = next((m for m in ex.migrations for o in m.ops if o.get("table") == t), ex.migrations[0])
                table_node(db, t, repo, first.file, 0)
                for c in sorted(cols):
                    col_node(db, t, c, repo, first.file, 0)
            for m in ex.migrations:
                mid = f"mig:{m.id}"
                g.add_node(mid, type="migration", repo=repo, service=ex.service, file=m.file, line=0, name=m.version, tool=m.tool)
                seen = set()
                for o in m.ops:
                    for t in (o.get("table"), o.get("new_table")):
                        if t and t not in seen:
                            seen.add(t)
                            g.add_edge(Edge(mid, "MIGRATES", table_node(db, t, repo, m.file, o["line"]), SOURCE_CONFIRMED, m.tool,
                                            None, ev(m.file, o["line"])))


def data_summary(extracts: dict) -> dict:
    """Coverage figures for the report; empty when no repository has a data layer."""
    ents = sum(len(e.entities) for e in extracts.values())
    migs = sum(len(e.migrations) for e in extracts.values())
    if not ents and not migs:
        return {}
    return {"entities": ents, "data_access_relationships": sum(len(e.data_access) for e in extracts.values()),
            "migrations": migs, "databases": sorted({database_of(e) for e in extracts.values() if e.entities or e.migrations})}


# =====================================================================================================================
# Data changes
# =====================================================================================================================
_TYPE = re.compile(r"^([a-z0-9_ ]+?)(?:\((\d+)(?:,(\d+))?\))?$")
_INT_RANK = {"smallint": 1, "int2": 1, "smallserial": 1, "int": 2, "integer": 2, "int4": 2, "serial": 2, "mediumint": 2,
             "bigint": 3, "int8": 3, "bigserial": 3}
_TEXT = {"varchar", "character varying", "varchar2", "nvarchar", "nvarchar2", "char", "character", "nchar"}


def type_widened(old: Optional[str], new: Optional[str]) -> bool:
    """True when every value of the old type fits the new one (same string family with a longer length, or a larger
    integer). Anything not provably a widening is treated as a breaking type change."""
    if not old or not new:
        return False
    a, b = _TYPE.match(old.strip().lower()), _TYPE.match(new.strip().lower())
    if not a or not b:
        return False
    (fa, la, sa), (fb, lb, sb) = a.groups(), b.groups()
    fa, fb = fa.strip(), fb.strip()
    if fa in _INT_RANK and fb in _INT_RANK:
        return _INT_RANK[fb] >= _INT_RANK[fa]
    if fa in _TEXT and fb in _TEXT and fa == fb and la and lb:
        return int(lb) >= int(la)
    if fa == fb == "text":
        return True
    if fa in _TEXT and fb == "text":
        return True
    if fa in ("numeric", "decimal") and fb == fa and la and lb:
        return int(lb) >= int(la) and int(sb or 0) >= int(sa or 0) and int(lb) - int(sb or 0) >= int(la) - int(sa or 0)
    return False


def _op_key(o: dict) -> str:
    return json.dumps({k: v for k, v in o.items() if k != "line"}, sort_keys=True, default=str)


def _change(kind, breaking, elem, seeds, m, o, detail, db, **extra):
    return {"kind": kind, "breaking": breaking, "element": elem, "seeds": seeds, "file": m.file, "line": o.get("line", 0),
            "detail": detail, "database": db, "table": o.get("table"), "column": o.get("column"), **extra}


def classify_op(o: dict, state: dict, m, db: str) -> list:
    """Changes one migration operation makes, judged against the schema before it ([] when it changes nothing downstream
    could depend on)."""
    t, c, kind = o.get("table"), o.get("column"), o["op"]
    tid = table_id(db, t) if t else None
    if kind == "create_table":
        return [_change("table_added", False, tid, [], m, o, f"table {t} added", db)]
    if kind == "drop_table":
        return [_change("table_dropped", True, tid, [{"node": tid, "access": None}], m, o, f"table {t} dropped", db)]
    if kind == "rename_table":
        return [_change("table_renamed", True, tid, [{"node": tid, "access": None}], m, o,
                        f"table {t} renamed to {o['new_table']}", db, new_table=o["new_table"])]
    cid = col_id(db, t, c) if t and c else None
    if kind == "drop_column":
        return [_change("column_dropped", True, cid, [{"node": cid, "access": None}], m, o, f"column {t}.{c} dropped", db)]
    if kind == "rename_column":
        return [_change("column_renamed", True, cid, [{"node": cid, "access": None}], m, o,
                        f"column {t}.{c} renamed to {o['new_column']}", db, new_column=o["new_column"])]
    if kind == "add_column":
        if not o.get("nullable", True) and not o.get("default", False):
            return [_change("column_added_required", True, tid, [{"node": tid, "access": "write"}], m, o,
                            f"column {t}.{c} added NOT NULL without a default", db)]
        return [_change("column_added", False, cid, [], m, o, f"column {t}.{c} added", db)]
    out = []
    if kind == "alter_column":
        prior = (state.get(t) or {}).get(c) or {}
        if "type" in o and o["type"] != prior.get("type"):
            if type_widened(prior.get("type"), o["type"]):
                out.append(_change("column_type_widened", False, cid, [], m, o,
                                   f"column {t}.{c} type {prior.get('type')} -> {o['type']} (widening)", db))
            else:
                was = prior.get("type") or "unknown"
                out.append(_change("column_type_changed", True, cid, [{"node": cid, "access": None}], m, o,
                                   f"column {t}.{c} type {was} -> {o['type']}", db))
        if "nullable" in o and o["nullable"] != prior.get("nullable", True):
            if o["nullable"]:
                out.append(_change("column_made_nullable", False, cid, [], m, o, f"column {t}.{c} made nullable", db))
            else:
                out.append(_change("column_made_required", True, cid, [{"node": cid, "access": "write"}], m, o,
                                   f"column {t}.{c} made NOT NULL", db))
    return out


def diff_data(base: RepoExtract, head: RepoExtract) -> tuple:
    """-> (changes, changed_migrations). Changes are the operations the head revision adds to the repository's migrations;
    changed_migrations are the head migrations that are new or edited (their unparsed statements matter)."""
    db = database_of(base)
    base_ops: dict = {}
    for m in base.migrations:
        base_ops.setdefault(m.id, Counter()).update(_op_key(o) for o in m.ops)
    state: dict = {}
    changes, changed = [], []
    for m in order_migrations(head.migrations):
        seen = Counter(base_ops.get(m.id, {}))
        is_new = m.id not in base_ops
        touched = is_new
        for o in m.ops:
            k = _op_key(o)
            if seen[k] > 0:
                seen[k] -= 1
            else:
                touched = True
                changes += classify_op(o, state, m, db)
            apply_op(state, o)
        if touched or any(seen.values()):
            changed.append(m)
    changes.sort(key=lambda c: (c["file"], c["line"], c["kind"], c["detail"]))
    return changes, changed
