"""Data-layer adapter: schema migrations and assembly-time resolution of ORM mappings (FR-006, FR-006a).

Migrations (versioned, declarative schema changes)
  Flyway / plain SQL   CREATE/ALTER/DROP TABLE, RENAME, column add/drop/rename/type/nullability (PostgreSQL, MySQL, Oracle,
                       SQL Server dialects of those statements); procedural SQL is reported, never read as "no change"
  Liquibase            XML / YAML / JSON changelogs: createTable, addColumn, dropColumn, renameColumn, modifyDataType,
                       addNotNullConstraint, dropNotNullConstraint, dropTable, renameTable and embedded <sql>
  Alembic              op.create_table / add_column / drop_column / alter_column / drop_table / rename_table / execute,
                       including batch_alter_table
  Django               CreateModel / AddField / RemoveField / AlterField / RenameField / DeleteModel / RenameModel /
                       AlterModelTable / RunSQL; RunPython is reported as not analysed

Everything a statement does that this adapter cannot read is returned as ``unparsed`` and surfaces in the report as data
access that is not fully analysed. Repository files are parsed, never executed.

ORM mappings are extracted per file (extract_java.py, extract_orm.py); anything that needs several files (a Spring Data
repository and the entity it names, a mapped superclass, a Django app label) is resolved by ``resolve_data`` when the
repository extract is assembled, so per-file caching and the incremental == full-rescan property are preserved."""
from __future__ import annotations

import ast
import json
import posixpath
import re
from typing import Optional
from xml.etree import ElementTree

import yaml

from . import EXTRACTOR_VERSION
from .models import DataAccess, Entity, Migration, RepoExtract

IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$#]*)'
QNAME = rf"{IDENT}(?:\s*\.\s*{IDENT})*"
READ_PREFIXES = ("find", "get", "read", "query", "stream", "search", "count", "exists", "load", "fetch", "list", "select")
WRITE_PREFIXES = ("save", "delete", "remove", "insert", "update", "persist", "merge", "upsert", "flush", "create", "add", "store")


def norm_ident(s: str) -> str:
    """Table / column name as compared across sources: unquoted, unqualified, lower-case."""
    parts = re.findall(IDENT, s.strip())
    return (parts[-1] if parts else s).strip('"`[]').lower()


def snake(name: str) -> str:
    """Spring Boot's default physical naming strategy: an underscore goes where a lower-case letter is followed by an
    upper-case letter that is followed by a lower-case letter (userId -> user_id, userID -> userid)."""
    out = [name[0]] if name else []
    for i in range(1, len(name)):
        c = name[i]
        if (i < len(name) - 1 and name[i - 1].islower() and c.isupper() and name[i + 1].islower()):
            out.append("_")
        out.append(c)
    return "".join(out).lower()


def snake_flask(name: str) -> str:
    """Flask-SQLAlchemy's default table name: CamelCase -> camel_case."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


# =====================================================================================================================
# Paths
# =====================================================================================================================
def is_sql_migration(path: str) -> bool:
    p = "/" + path.lower()
    base = p.rsplit("/", 1)[-1]
    if not base.endswith(".sql") or "/src/test/" in p or base.startswith("u"):
        return False
    return ("/db/migration" in p or "/migrations/" in p or "/flyway/" in p or base.startswith("r__")
            or re.match(r"^v\d[\d._]*__", base) is not None or re.match(r"^schema([-_].+)?\.sql$", base) is not None)


def is_liquibase_path(path: str) -> bool:
    p = "/" + path.lower()
    return p.endswith((".xml", ".yaml", ".yml", ".json")) and ("changelog" in p or "/liquibase/" in p) and "/src/test/" not in p


def is_data_file(path: str) -> bool:
    return is_sql_migration(path) or is_liquibase_path(path)


# =====================================================================================================================
# SQL
# =====================================================================================================================
_PROC_HEAD = re.compile(r"^\s*(create\s+(or\s+replace\s+)?(definer\s*=\s*\S+\s+)?(function|procedure|trigger|package|type\s+body)\b|do\b|declare\b)",
                        re.I)
_WORD = re.compile(r"[A-Za-z_]+")


def split_statements(text: str) -> list:
    """[(statement, first_line)]. Comments are dropped, quoted text is kept intact, a procedural block ends at its
    matching END, a '/' line or GO, and dollar-quoted bodies are never split."""
    out, buf = [], []
    i, n, line = 0, len(text), 1
    start, depth, procedural, skip_case = None, 0, False, False

    def push(s):
        nonlocal start
        if start is None and s.strip():
            start = line
        buf.append(s)

    def close():
        nonlocal start, depth, procedural, buf, skip_case
        s = "".join(buf).strip()
        if s:
            out.append((s, start or line))
        buf, start, depth, procedural, skip_case = [], None, 0, False, False

    while i < n:
        c = text[i]
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            push(" ")
            i = j
            continue
        if c in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            seg = text[i:j + 1]
            push(seg)
            line += seg.count("\n")
            i = j + 1
            continue
        if c == "[" and re.match(r"\[[A-Za-z_][^\]\n]*\]", text[i:i + 128]):
            j = text.find("]", i)
            push(text[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$([A-Za-z_]\w*)?\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                seg = text[i:j]
                push(seg)
                line += seg.count("\n")
                i = j
                continue
        if c == "\n":
            line += 1
            ln = text[i + 1:text.find("\n", i + 1) if text.find("\n", i + 1) >= 0 else n].strip()
            if ln == "/" or ln.lower() == "go":
                close()
                i += 1 + len(text[i + 1:text.find("\n", i + 1) if text.find("\n", i + 1) >= 0 else n])
                continue
        if c == ";":
            if not procedural or depth <= 0:
                close()
                i += 1
                continue
            push(c)
            i += 1
            continue
        if c.isalpha() or c == "_":
            m = _WORD.match(text, i)
            w = m.group(0)
            wl = w.lower()
            first = start is None
            push(w)
            fw_m = re.match(r"\s*(\w+|;|$)", text[m.end():])
            fw = fw_m.group(1).lower() if fw_m else ""
            if not procedural:
                head = "".join(buf)
                if len(head) < 80 and _PROC_HEAD.match(head):
                    procedural = True
                elif first and wl == "begin" and fw not in ("", ";", "transaction", "work", "tran", "isolation", "read",
                                                           "deferred", "immediate", "exclusive"):
                    procedural = True                           # BEGIN ... END block; a bare BEGIN; starts a transaction
            if procedural:
                if wl == "case" and skip_case:
                    skip_case = False
                elif wl in ("begin", "case"):
                    depth += 1
                elif wl == "end" and fw not in ("if", "loop", "while", "for", "repeat"):
                    depth -= 1
                    skip_case = fw == "case"                   # END CASE closes the CASE counted above
            i = m.end()
            continue
        push(c)
        i += 1
    close()
    return out


def split_top(s: str, sep: str = ",") -> list:
    """Split on a separator that is outside parentheses and quotes."""
    out, cur, depth, q = [], [], 0, None
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            continue
        if ch in ("'", '"', "`"):
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _balanced(s: str) -> tuple:
    """('(...)' content, remainder) for a string that starts with '('."""
    depth, q = 0, None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
            continue
        if ch in ("'", '"', "`"):
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[1:i], s[i + 1:]
    return s[1:], ""


_TYPE_END = re.compile(r"\b(not\s+null|null|default|primary\s+key|references|unique|check|generated|auto_increment|autoincrement|"
                       r"identity|collate|constraint|comment|on\s+update|encode|as\s*\(|first|after)\b", re.I)
_SKIP_ITEM = re.compile(r"^(constraint|primary\s+key|foreign\s+key|unique|check|index|key|fulltext|spatial|like|exclude|period|"
                        r"partition)\b", re.I)
_SERIAL = re.compile(r"^(big|small)?serial\d*$", re.I)


def _norm_type(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*,\s*", ",", t.strip())).lower()


def parse_column_def(item: str) -> Optional[tuple]:
    """'name type [constraints]' -> (name, {"type", "nullable", "default"}). None when it is not a column definition."""
    m = re.match(rf"^({IDENT})\s*(.*)$", item.strip(), re.S)
    if not m or _SKIP_ITEM.match(item.strip()):
        return None
    name, rest = norm_ident(m.group(1)), m.group(2)
    t = _TYPE_END.search(rest)
    ty = rest[:t.start()] if t else rest
    constraints = rest[t.start():] if t else ""
    low = constraints.lower()
    serial = bool(_SERIAL.match(ty.strip()))
    pk = bool(re.search(r"\bprimary\s+key\b", low))
    return name, {"type": _norm_type(ty), "nullable": not (pk or serial or re.search(r"\bnot\s+null\b", low)),
                  "default": bool(re.search(r"\bdefault\b|auto_increment|autoincrement|\bidentity\b|\bgenerated\b", low) or serial),
                  "pk": pk}


def _ops_alter_action(table: str, action: str, line: int, state: dict) -> tuple:
    """-> (ops, unparsed_reason | None) for one action of an ALTER TABLE statement."""
    a = " ".join(action.split())
    if not a:
        return [], None
    low = a.lower()
    if re.match(r"^add\s+(constraint|primary|foreign|unique|check|index|key|fulltext|spatial|partition)\b", low):
        return [], None
    m = re.match(r"^add\s+\(", a, re.I)
    if m:
        inner, _ = _balanced(a[a.index("("):])
        ops = []
        for it in split_top(inner):
            col = parse_column_def(it)
            if col:
                ops.append({"op": "add_column", "table": table, "column": col[0], "line": line, **col[1]})
        return ops, None
    m = re.match(r"^add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?(.+)$", a, re.I)
    if m:
        col = parse_column_def(m.group(1))
        if col:
            state["last"] = "add"
            return [{"op": "add_column", "table": table, "column": col[0], "line": line, **col[1]}], None
        return [], f"ALTER TABLE ADD not analysed: {a[:60]}"
    if re.match(r"^drop\s+(constraint|primary\s+key|foreign\s+key|index|key|check|unique|default|partition)\b", low):
        return [], None
    m = re.match(rf"^drop\s+(?:column\s+)?(?:if\s+exists\s+)?({IDENT})(?:\s+(?:cascade|restrict))?$", a, re.I)
    if m:
        state["last"] = "drop"
        return [{"op": "drop_column", "table": table, "column": norm_ident(m.group(1)), "line": line}], None
    if state.get("last") == "drop" and re.fullmatch(IDENT, a):                       # T-SQL: DROP COLUMN a, b
        return [{"op": "drop_column", "table": table, "column": norm_ident(a), "line": line}], None
    m = re.match(rf"^alter\s+(?:column\s+)?({IDENT})\s+(?:set\s+data\s+)?type\s+(.+?)(?:\s+using\s+.*)?$", a, re.I)
    if m:
        return [{"op": "alter_column", "table": table, "column": norm_ident(m.group(1)),
                 "type": _norm_type(m.group(2)), "line": line}], None
    m = re.match(rf"^alter\s+(?:column\s+)?({IDENT})\s+(set|drop)\s+not\s+null$", a, re.I)
    if m:
        return [{"op": "alter_column", "table": table, "column": norm_ident(m.group(1)),
                 "nullable": m.group(2).lower() == "drop", "line": line}], None
    if re.match(rf"^alter\s+(?:column\s+)?{IDENT}\s+(set|drop|add|reset)\b", low):
        return [], None                                                                # defaults, statistics, identity, storage
    m = re.match(rf"^alter\s+column\s+({IDENT})\s+(.+)$", a, re.I)                    # T-SQL: ALTER COLUMN c type [NOT] NULL
    if m:
        col = parse_column_def(f"{m.group(1)} {m.group(2)}")
        if col:
            return [{"op": "alter_column", "table": table, "column": col[0], "type": col[1]["type"],
                     "nullable": col[1]["nullable"], "line": line}], None
    m = re.match(r"^modify\s+(?:column\s+)?(.+)$", a, re.I)                           # MySQL / Oracle
    if m:
        body = m.group(1)
        items = split_top(_balanced(body)[0]) if body.startswith("(") else [body]
        ops = []
        for it in items:
            col = parse_column_def(it)
            if col:
                op = {"op": "alter_column", "table": table, "column": col[0], "type": col[1]["type"], "line": line}
                if re.search(r"\b(not\s+)?null\b", it, re.I):
                    op["nullable"] = col[1]["nullable"]
                ops.append(op)
        return ops, (None if ops else f"ALTER TABLE MODIFY not analysed: {a[:60]}")
    m = re.match(rf"^change\s+(?:column\s+)?({IDENT})\s+({IDENT})\s+(.+)$", a, re.I)    # MySQL
    if m:
        old, new = norm_ident(m.group(1)), norm_ident(m.group(2))
        col = parse_column_def(f"{m.group(2)} {m.group(3)}")
        ops = [{"op": "rename_column", "table": table, "column": old, "new_column": new, "line": line}] if old != new else []
        if col:
            ops.append({"op": "alter_column", "table": table, "column": new, "type": col[1]["type"],
                        "nullable": col[1]["nullable"], "line": line})
        return ops, None
    m = re.match(rf"^rename\s+column\s+({IDENT})\s+to\s+({IDENT})$", a, re.I)
    if m:
        return [{"op": "rename_column", "table": table, "column": norm_ident(m.group(1)),
                 "new_column": norm_ident(m.group(2)), "line": line}], None
    m = re.match(rf"^rename\s+(?:to|as)\s+({QNAME})$", a, re.I)
    if m:
        return [{"op": "rename_table", "table": table, "new_table": norm_ident(m.group(1)), "line": line}], None
    if re.match(r"^(owner\s+to|enable|disable|set\b|reset|cluster|inherit|no\s+inherit|validate|attach|detach|force|no\s+force|"
                r"replica|alter\s+constraint|alter\s+index|comment|engine|auto_increment|convert|charset|character|collate|"
                r"algorithm|lock|row_format|rename\s+(index|key|constraint)|nocheck|with\s+(check|nocheck)|check\s+constraint|"
                r"add\s+partition|drop\s+partition|row\s+level|alter\s+primary)", low):
        return [], None
    return [], f"ALTER TABLE action not analysed: {a[:70]}"


_IGNORE_STATEMENT = re.compile(
    r"^(create\s+(unique\s+|clustered\s+|nonclustered\s+|bitmap\s+|fulltext\s+|spatial\s+)*index|drop\s+index|"
    r"create\s+(sequence|extension|schema|type|domain|synonym|role|user|database|tablespace|rule|policy|collation|aggregate|"
    r"operator|cast|language|publication|subscription|statistics|text\s+search)|"
    r"drop\s+(sequence|extension|schema|type|domain|synonym|role|user|database|tablespace|trigger|function|procedure|view|"
    r"materialized|rule|policy|aggregate|publication|subscription|statistics)|"
    r"alter\s+(sequence|schema|type|index|database|role|user|view|default|system|extension|domain|materialized|policy)|"
    r"comment\s+on|grant|revoke|set\b|use\b|commit|rollback|savepoint|release|start\s+transaction|analyze|vacuum|reindex|lock\b|"
    r"insert\b|update\b|delete\b|merge\b|truncate|copy\b|select\b|with\b|create\s+(or\s+replace\s+)?(temp\w*\s+)?(view|materialized\s+view)|"
    r"refresh|alter\s+session|print\b|exec\s+sp_addextendedproperty|values\b|declare\b.*cursor)", re.I)
_NOOP_BEGIN = re.compile(r"^begin(\s+(transaction|work|tran))?$", re.I)


def parse_statement(sql: str, line: int) -> tuple:
    """-> (ops, unparsed_entry | None) for one SQL statement."""
    s = " ".join(sql.split())
    if not s:
        return [], None

    def unparsed(reason):
        return [], {"line": line, "reason": reason, "text": s[:160]}

    if _NOOP_BEGIN.match(s):
        return [], None
    if _PROC_HEAD.match(s) or re.match(r"^begin\b", s, re.I):
        return unparsed("procedural SQL (stored procedure/function/trigger/block) is not analysed")
    if re.match(r"^(call|exec|execute)\b", s, re.I):
        return unparsed("stored-procedure call is not analysed")
    m = re.match(rf"^create\s+((?:global\s+|local\s+)?(?:temporary|temp)\s+|unlogged\s+|external\s+)?table\s+(?:if\s+not\s+exists\s+)?({QNAME})\s*(.*)$",
                 s, re.I | re.S)
    if m:
        if m.group(1) and "unlogged" not in m.group(1).lower():
            return [], None                                       # temporary / external tables are not mapped persistent tables
        table, rest = norm_ident(m.group(2)), m.group(3).strip()
        if not rest.startswith("("):
            return [{"op": "create_table", "table": table, "columns": None, "line": line}], None
        inner, _ = _balanced(rest)
        cols, pk_cols = {}, set()
        for it in split_top(inner):
            pm = re.match(r"^(?:constraint\s+\S+\s+)?primary\s+key\s*(?:\w+\s*)?\((.*?)\)", it, re.I)
            if pm:
                pk_cols |= {norm_ident(x) for x in split_top(pm.group(1))}
                continue
            col = parse_column_def(it)
            if col:
                cols[col[0]] = {k: v for k, v in col[1].items() if k in ("type", "nullable", "pk")}
        for c in pk_cols & set(cols):
            cols[c]["nullable"], cols[c]["pk"] = False, True
        return [{"op": "create_table", "table": table, "columns": cols, "line": line}], None
    m = re.match(rf"^alter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?({QNAME})\s+(.*)$", s, re.I)
    if m:
        table, ops, bad, state = norm_ident(m.group(1)), [], None, {}
        acts = split_top(m.group(2))
        if len(acts) == 1 and re.match(r"^(add|modify)\s+\(", acts[0], re.I):
            acts = [acts[0]]
        for act in acts:
            o, why = _ops_alter_action(table, act, line, state)
            ops += o
            bad = bad or why
        return ops, ({"line": line, "reason": bad, "text": s[:160]} if bad else None)
    m = re.match(r"^drop\s+table\s+(?:if\s+exists\s+)?(.*?)(?:\s+if\s+exists)?(?:\s+(?:cascade(?:\s+constraints)?|restrict|purge))*$",
                 s, re.I)
    if m:
        names = split_top(m.group(1))
        if all(re.fullmatch(QNAME, t) for t in names) and names:
            return [{"op": "drop_table", "table": norm_ident(t), "line": line} for t in names], None
        return unparsed(f"DROP TABLE target not analysed: {m.group(1)[:50]}")
    m = re.match(r"^rename\s+table\s+(.*)$", s, re.I)
    if m:
        ops = []
        for pair in split_top(m.group(1)):
            pm = re.match(rf"^({QNAME})\s+to\s+({QNAME})$", pair, re.I)
            if pm:
                ops.append({"op": "rename_table", "table": norm_ident(pm.group(1)), "new_table": norm_ident(pm.group(2)), "line": line})
        return ops, None
    if _IGNORE_STATEMENT.match(s):
        return [], None
    return unparsed(f"statement not analysed: {s[:50]}")


def parse_sql(text: str, base_line: int = 1) -> tuple:
    """-> (ops, unparsed) for a SQL script."""
    ops, bad = [], []
    for stmt, line in split_statements(text):
        o, u = parse_statement(stmt, line + base_line - 1)
        ops += o
        if u:
            bad.append(u)
    return ops, bad


def _flyway_version(path: str) -> Optional[str]:
    base = posixpath.basename(path)
    m = re.match(r"^[vV]([\d._]+)__", base)
    if m:
        return m.group(1).replace("_", ".")
    if base.lower().startswith("r__"):
        return "R"
    return posixpath.splitext(base)[0]


# =====================================================================================================================
# Liquibase
# =====================================================================================================================
_LB_IGNORED = {"createindex", "dropindex", "addprimarykey", "dropprimarykey", "addforeignkeyconstraint", "dropforeignkeyconstraint",
               "dropallforeignkeyconstraints", "adduniqueconstraint", "dropuniqueconstraint", "adddefaultvalue", "dropdefaultvalue",
               "createsequence", "dropsequence", "altersequence", "insert", "update", "delete", "loaddata", "loadupdatedata",
               "tagdatabase", "comment", "preconditions", "rollback", "settablemarks", "setcolumnremarks", "settableremarks",
               "addautoincrement", "createview", "dropview", "renameview", "output", "empty", "stop", "validchecksum",
               "modifysql", "include", "includeall", "property", "context", "label", "changelogproperties", "addlookuptable",
               "droptrigger", "dropprocedure", "dropsequence", "renamesequence", "mergecolumns", "dropviewifexists"}
_LB_PROC = {"createprocedure", "createfunction", "createpackage", "createtrigger", "customchange", "executecommand", "sqlfile",
            "createpackagebody", "alterpackage"}


def _flag(v, default=None):
    if v is None:
        return default
    return str(v).strip().lower() == "true"


def _lb_change(tag: str, attrs: dict, columns: list, text: str, line: int) -> tuple:
    """One Liquibase change -> (ops, unparsed_entry | None). columns: [{name, type, constraints: {...}}]."""
    t = tag.lower()
    tn = attrs.get("tableName") or ""
    table = norm_ident(tn) if tn else ""
    if t == "createtable":
        cols = {}
        for c in columns:
            con = c.get("constraints") or {}
            pk = bool(_flag(con.get("primaryKey"), False))
            cols[norm_ident(str(c.get("name", "")))] = {"type": _norm_type(str(c.get("type", ""))),
                                                        "nullable": not (pk or _flag(con.get("nullable"), True) is False),
                                                        "pk": pk}
        return [{"op": "create_table", "table": table, "columns": cols, "line": line}], None
    if t == "addcolumn":
        ops = []
        for c in columns:
            con = c.get("constraints") or {}
            has_default = any(k in c for k in ("defaultValue", "defaultValueNumeric", "defaultValueBoolean", "defaultValueDate",
                                               "defaultValueComputed", "autoIncrement"))
            ops.append({"op": "add_column", "table": table, "column": norm_ident(str(c.get("name", ""))),
                        "type": _norm_type(str(c.get("type", ""))), "nullable": _flag(con.get("nullable"), True) is not False,
                        "default": has_default, "line": line})
        return ops, None
    if t == "dropcolumn":
        names = [attrs["columnName"]] if attrs.get("columnName") else [c.get("name") for c in columns if c.get("name")]
        return [{"op": "drop_column", "table": table, "column": norm_ident(str(n)), "line": line} for n in names], None
    if t == "renamecolumn":
        return [{"op": "rename_column", "table": table, "column": norm_ident(str(attrs.get("oldColumnName", ""))),
                 "new_column": norm_ident(str(attrs.get("newColumnName", ""))), "line": line}], None
    if t == "modifydatatype":
        return [{"op": "alter_column", "table": table, "column": norm_ident(str(attrs.get("columnName", ""))),
                 "type": _norm_type(str(attrs.get("newDataType", ""))), "line": line}], None
    if t in ("addnotnullconstraint", "dropnotnullconstraint"):
        op = {"op": "alter_column", "table": table, "column": norm_ident(str(attrs.get("columnName", ""))),
              "nullable": t == "dropnotnullconstraint", "line": line}
        if attrs.get("columnDataType"):
            op["type"] = _norm_type(str(attrs["columnDataType"]))
        return [op], None
    if t == "droptable":
        return [{"op": "drop_table", "table": table, "line": line}], None
    if t == "renametable":
        return [{"op": "rename_table", "table": norm_ident(str(attrs.get("oldTableName", ""))),
                 "new_table": norm_ident(str(attrs.get("newTableName", ""))), "line": line}], None
    if t == "sql":
        ops, bad = parse_sql(text or "", line)
        return ops, (bad[0] if bad else None)
    if t in _LB_PROC:
        return [], {"line": line, "reason": f"Liquibase {tag} is not analysed", "text": tag}
    if t in _LB_IGNORED:
        return [], None
    return [], {"line": line, "reason": f"Liquibase change type not analysed: {tag}", "text": tag}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _lb_xml(text: str) -> tuple:
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        raise ValueError("XML with a DOCTYPE/ENTITY declaration is not parsed")
    root = ElementTree.fromstring(text)
    if _local(root.tag) != "databaseChangeLog":
        return None, [], []
    ops, bad, cursor = [], [], 0

    def line_of(tag):
        nonlocal cursor
        m = re.compile(rf"<(?:\w+:)?{re.escape(tag)}\b").search(text, cursor)
        if m:
            cursor = m.end()
            return text.count("\n", 0, m.start()) + 1
        return 0
    for cs in root.iter():
        if _local(cs.tag) != "changeSet":
            continue
        line_of("changeSet")
        for el in list(cs):
            tag = _local(el.tag)
            line = line_of(tag)
            if tag.lower() == "rollback":
                continue
            cols = []
            for c in el:
                if _local(c.tag) == "column":
                    con = next((dict(x.attrib) for x in c if _local(x.tag) == "constraints"), {})
                    cols.append({**dict(c.attrib), "constraints": con})
            o, u = _lb_change(tag, dict(el.attrib), cols, el.text if tag.lower() == "sql" else "", line)
            ops += o
            if u:
                bad.append(u)
    return True, ops, bad


def _lb_data(doc, text: str) -> tuple:
    if not isinstance(doc, dict) or "databaseChangeLog" not in doc:
        return None, [], []
    ops, bad, cursor = [], [], 0

    def line_of(key):
        nonlocal cursor
        m = re.compile(rf"(?m)^[\s\-]*\"?{re.escape(key)}\"?\s*:").search(text, cursor)
        if m:
            cursor = m.end()
            return text.count("\n", 0, m.start()) + 1
        return 0
    for item in doc.get("databaseChangeLog") or []:
        cs = item.get("changeSet") if isinstance(item, dict) else None
        if not isinstance(cs, dict):
            continue
        line_of("changeSet")
        for ch in cs.get("changes") or []:
            if not isinstance(ch, dict):
                continue
            for tag, attrs in ch.items():
                line = line_of(tag)
                attrs = attrs if isinstance(attrs, dict) else {"sql": attrs}
                cols = [(c.get("column") if isinstance(c, dict) and "column" in c else c) for c in (attrs.get("columns") or [])]
                cols = [c for c in cols if isinstance(c, dict)]
                o, u = _lb_change(tag, {k: v for k, v in attrs.items() if k != "columns"}, cols,
                                  str(attrs.get("sql", "")) if tag.lower() == "sql" else "", line)
                ops += o
                if u:
                    bad.append(u)
    return True, ops, bad


# =====================================================================================================================
# Python migrations (Alembic, Django)
# =====================================================================================================================
def _cs(n) -> Optional[str]:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _bool(n) -> Optional[bool]:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, bool) else None


def _unp(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:                                                   # pragma: no cover
        return "?"


_SA_TYPES = {"string": "varchar", "unicode": "varchar", "text": "text", "unicodetext": "text", "integer": "integer",
             "biginteger": "bigint", "smallinteger": "smallint", "boolean": "boolean", "datetime": "timestamp", "date": "date",
             "float": "float", "numeric": "numeric", "uuid": "uuid", "largebinary": "bytea", "time": "time"}


def sa_type(expr: str) -> str:
    """SQLAlchemy type expression -> the SQL type family it creates: 'sa.String(50)' -> 'varchar(50)', 'sa.Integer()' ->
    'integer'. Types it does not know keep their own (lower-cased) name."""
    s = re.sub(r"^(?:\w+\.)+", "", expr.strip())
    m = re.match(r"^(\w+)\s*(?:\((.*)\))?$", s, re.S)
    if not m:
        return _norm_type(expr)
    name, args = m.group(1).lower(), (m.group(2) or "").strip()
    args = re.sub(r"\s+", "", args)
    if name in ("string", "unicode", "numeric") and re.fullmatch(r"\d+(?:,\d+)?", args):
        return f"{_SA_TYPES[name]}({args})"
    if name in _SA_TYPES:
        return _SA_TYPES[name]
    return name if not args else f"{name}({args})"


def _sa_column(call: ast.Call) -> Optional[tuple]:
    """sa.Column("name", sa.String(50), nullable=False, ...) -> (name, {"type", "nullable", "default", "pk"})."""
    if not isinstance(call, ast.Call) or _unp(call.func).rsplit(".", 1)[-1] != "Column":
        return None
    name, ty = None, ""
    for a in call.args:
        s = _cs(a)
        if s is not None and name is None:
            name = s
        elif not isinstance(a, ast.Constant) and not ty and _unp(a).rsplit(".", 1)[-1] not in ("ForeignKey", "Sequence", "Identity"):
            ty = _unp(a)
    if name is None:
        return None
    pk = _bool(_kw(call, "primary_key")) is True
    nullable = _bool(_kw(call, "nullable"))
    return name.lower(), {"type": sa_type(ty), "nullable": (not pk) if nullable is None else nullable, "pk": pk,
                          "default": any(_kw(call, k) is not None for k in ("server_default", "default", "autoincrement", "identity"))}


def alembic_migration(repo: str, service: str, path: str, tree: ast.Module) -> Optional[Migration]:
    rev, parent, upgrade = None, None, None
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            val = n.value
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "revision":
                    rev = _cs(val)
                elif isinstance(t, ast.Name) and t.id == "down_revision":
                    parent = _cs(val) or (next((_cs(e) for e in val.elts if _cs(e)), None) if isinstance(val, (ast.Tuple, ast.List)) else None)
        elif isinstance(n, ast.FunctionDef) and n.name == "upgrade":
            upgrade = n
    if rev is None or upgrade is None:
        return None
    ops, bad, batch = [], [], {}
    for n in ast.walk(upgrade):
        if isinstance(n, ast.With):
            for it in n.items:
                c = it.context_expr
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "batch_alter_table"
                        and c.args and _cs(c.args[0]) and isinstance(it.optional_vars, ast.Name)):
                    batch[it.optional_vars.id] = _cs(c.args[0]).lower()
    for n in ast.walk(upgrade):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)):
            continue
        owner, meth, args = n.func.value.id, n.func.attr, list(n.args)
        if owner == "op":
            table = _cs(args[0]).lower() if args and _cs(args[0]) else None
            rest = args[1:]
        elif owner in batch:
            table, rest = batch[owner], args
        else:
            continue
        line = n.lineno
        if meth == "add_column" and table and rest:
            col = _sa_column(rest[0])
            if col:
                ops.append({"op": "add_column", "table": table, "column": col[0], "line": line, **col[1]})
        elif meth == "drop_column" and table and rest and _cs(rest[0]):
            ops.append({"op": "drop_column", "table": table, "column": _cs(rest[0]).lower(), "line": line})
        elif meth == "create_table" and table:
            cols = {}
            for a in rest:
                col = _sa_column(a)
                if col:
                    cols[col[0]] = {k: v for k, v in col[1].items() if k in ("type", "nullable", "pk")}
            ops.append({"op": "create_table", "table": table, "columns": cols, "line": line})
        elif meth == "drop_table" and table:
            ops.append({"op": "drop_table", "table": table, "line": line})
        elif meth == "rename_table" and table and rest and _cs(rest[0]):
            ops.append({"op": "rename_table", "table": table, "new_table": _cs(rest[0]).lower(), "line": line})
        elif meth == "alter_column" and table and rest and _cs(rest[0]):
            col = _cs(rest[0]).lower()
            new = _kw(n, "new_column_name")
            if new is not None and _cs(new):
                ops.append({"op": "rename_column", "table": table, "column": col, "new_column": _cs(new).lower(), "line": line})
                col = _cs(new).lower()
            op = {"op": "alter_column", "table": table, "column": col, "line": line}
            if _kw(n, "type_") is not None:
                op["type"] = sa_type(_unp(_kw(n, "type_")))
            if _bool(_kw(n, "nullable")) is not None:
                op["nullable"] = _bool(_kw(n, "nullable"))
            if "type" in op or "nullable" in op:
                ops.append(op)
        elif meth == "execute" and owner == "op":
            arg = args[0] if args else None
            sql = _cs(arg)
            if sql is None and isinstance(arg, ast.Call) and _unp(arg.func).rsplit(".", 1)[-1] == "text" and arg.args:
                sql = _cs(arg.args[0])
            if sql is None:
                bad.append({"line": line, "reason": "dynamic SQL in op.execute is not analysed", "text": _unp(arg)[:120]})
            else:
                o, u = parse_sql(sql, line)
                ops += o
                bad += u
    ops.sort(key=lambda o: o["line"])
    return Migration(id=f"{repo}:{path}", repo=repo, service=service, file=path, tool="alembic", version=rev, ops=ops,
                     unparsed=bad, parent=parent)


def m2m_through(table: str, model: str, target: str) -> dict:
    """Columns of the link table Django creates for a ManyToManyField: <model>_id and <target>_id; a self-referential
    relation uses from_<model>_id and to_<model>_id."""
    me, other = model.lower(), target.rsplit(".", 1)[-1].lower()
    fk = {"type": "fk", "nullable": False, "pk": False}
    if other in ("self", me):
        return {"id": {"type": "autofield", "nullable": False, "pk": True}, f"from_{me}_id": dict(fk), f"to_{me}_id": dict(fk)}
    return {"id": {"type": "autofield", "nullable": False, "pk": True}, f"{me}_id": dict(fk), f"{other}_id": dict(fk)}


def m2m_target(call: ast.Call) -> str:
    """The model a relation points to: the first argument, or the to= keyword that migrations serialise it as."""
    node = _kw(call, "to") if _kw(call, "to") is not None else (call.args[0] if call.args else None)
    return (_cs(node) or _unp(node)) if node is not None else ""


def _dj_field(call, attname: str, app: str, model: str) -> tuple:
    """-> (column | None, spec, through_table | None). ForeignKey/OneToOne use <name>_id; ManyToMany has a link table."""
    kind = _unp(call.func).rsplit(".", 1)[-1] if isinstance(call, ast.Call) else ""
    null = _bool(_kw(call, "null")) is True if isinstance(call, ast.Call) else False
    pk = isinstance(call, ast.Call) and _bool(_kw(call, "primary_key")) is True
    has_default = isinstance(call, ast.Call) and (null or _kw(call, "db_default") is not None)
    dbcol = _cs(_kw(call, "db_column")) if isinstance(call, ast.Call) else None
    if kind == "ManyToManyField":
        return None, None, (f"{app}_{model.lower()}_{attname}", m2m_through(f"{app}_{model.lower()}_{attname}", model, m2m_target(call)))
    if kind in ("ForeignKey", "OneToOneField"):
        col = (dbcol or f"{attname}_id").lower()
        return col, {"type": "fk", "nullable": null, "pk": pk, "default": has_default}, None
    col = (dbcol or attname).lower()
    ty = kind
    for k in ("max_length", "max_digits", "decimal_places"):
        if isinstance(call, ast.Call) and _kw(call, k) is not None:
            ty += f"({_unp(_kw(call, k))})" if k == "max_length" else f":{k}={_unp(_kw(call, k))}"
    return col, {"type": ty.lower(), "nullable": null and not pk, "pk": pk or kind in ("AutoField", "BigAutoField", "SmallAutoField"),
                 "default": has_default or kind in ("AutoField", "BigAutoField", "SmallAutoField")}, None


def django_migration(repo: str, service: str, path: str, tree: ast.Module) -> Optional[Migration]:
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Migration"), None)
    if cls is None:
        return None
    app = posixpath.basename(posixpath.dirname(posixpath.dirname(path))) or repo
    version = posixpath.splitext(posixpath.basename(path))[0]
    parent, ops_node = None, None
    for st in cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            if st.targets[0].id == "operations" and isinstance(st.value, (ast.List, ast.Tuple)):
                ops_node = st.value
            elif st.targets[0].id == "dependencies" and isinstance(st.value, (ast.List, ast.Tuple)):
                for d in st.value.elts:
                    if isinstance(d, (ast.Tuple, ast.List)) and len(d.elts) == 2 and _cs(d.elts[0]) == app and _cs(d.elts[1]):
                        parent = _cs(d.elts[1])
    if ops_node is None:
        return None
    ops, bad = [], []

    def tbl(model: str) -> str:
        return f"{app}_{model.lower()}"
    for o in ops_node.elts:
        if not isinstance(o, ast.Call):
            continue
        kind, line = _unp(o.func).rsplit(".", 1)[-1], o.lineno
        a = list(o.args)

        def arg(i, name):
            v = a[i] if len(a) > i else _kw(o, name)
            return _cs(v) if v is not None else None
        if kind == "CreateModel":
            name = arg(0, "name")
            fields = _kw(o, "fields") if len(a) < 2 else a[1]
            cols, extra = {}, []
            for f in (fields.elts if isinstance(fields, (ast.List, ast.Tuple)) else []):
                if isinstance(f, (ast.Tuple, ast.List)) and len(f.elts) == 2 and _cs(f.elts[0]):
                    col, spec, through = _dj_field(f.elts[1], _cs(f.elts[0]), app, name or "")
                    if col:
                        cols[col] = {k: v for k, v in spec.items() if k in ("type", "nullable", "pk")}
                    if through:
                        extra.append({"op": "create_table", "table": through[0], "columns": through[1], "line": line})
            opts = _kw(o, "options")
            custom = None
            if isinstance(opts, ast.Dict):
                for k, v in zip(opts.keys, opts.values):
                    if _cs(k) == "db_table":
                        custom = _cs(v)
            if name:
                if not any(c["pk"] for c in cols.values()) and not (isinstance(opts, ast.Dict) and any(
                        _cs(k) in ("proxy", "abstract") for k in opts.keys)):
                    cols = {"id": {"type": "autofield", "nullable": False, "pk": True}, **cols}
                ops.append({"op": "create_table", "table": (custom or tbl(name)).lower(), "columns": cols, "line": line,
                            "model": name.lower(), "app": app, **({"declared": True} if custom else {})})
                ops += extra
        elif kind in ("AddField", "AlterField"):
            model, attname = arg(0, "model_name"), arg(1, "name")
            fld = a[2] if len(a) > 2 else _kw(o, "field")
            if model and attname and isinstance(fld, ast.Call):
                col, spec, through = _dj_field(fld, attname, app, model)
                if through and kind == "AddField":
                    ops.append({"op": "create_table", "table": through[0], "columns": through[1], "line": line})
                elif col and kind == "AddField":
                    ops.append({"op": "add_column", "table": tbl(model), "column": col, "line": line, "model": model.lower(),
                                "app": app, **spec})
                elif col:
                    ops.append({"op": "alter_column", "table": tbl(model), "column": col, "type": spec["type"],
                                "nullable": spec["nullable"], "line": line, "model": model.lower(), "app": app})
        elif kind == "RemoveField":
            model, attname = arg(0, "model_name"), arg(1, "name")
            if model and attname:
                ops.append({"op": "drop_column", "table": tbl(model), "column": attname.lower(), "line": line,
                            "model": model.lower(), "app": app, "orm_field": attname.lower()})
        elif kind == "RenameField":
            model, old, new = arg(0, "model_name"), arg(1, "old_name"), arg(2, "new_name")
            if model and old and new:
                ops.append({"op": "rename_column", "table": tbl(model), "column": old.lower(), "new_column": new.lower(),
                            "line": line, "model": model.lower(), "app": app})
        elif kind == "DeleteModel":
            name = arg(0, "name")
            if name:
                ops.append({"op": "drop_table", "table": tbl(name), "line": line, "model": name.lower(), "app": app})
        elif kind == "RenameModel":
            old, new = arg(0, "old_name"), arg(1, "new_name")
            if old and new:
                ops.append({"op": "rename_table", "table": tbl(old), "new_table": tbl(new), "line": line, "model": old.lower(),
                            "app": app})
        elif kind == "AlterModelTable":
            name, table = arg(0, "name"), arg(1, "table")
            if name and table:
                ops.append({"op": "rename_table", "table": tbl(name), "new_table": table.lower(), "line": line,
                            "model": name.lower(), "app": app})
        elif kind == "RunSQL":
            sql = a[0] if a else _kw(o, "sql")
            texts = [_cs(x) for x in sql.elts] if isinstance(sql, (ast.List, ast.Tuple)) else [_cs(sql)]
            if any(t is None for t in texts):
                bad.append({"line": line, "reason": "dynamic RunSQL is not analysed", "text": _unp(sql)[:120]})
            for t in texts:
                if t:
                    po, pb = parse_sql(t, line)
                    ops += po
                    bad += pb
        elif kind == "RunPython":
            bad.append({"line": line, "reason": "RunPython: its schema or data effect is not analysed", "text": _unp(o)[:120]})
    return Migration(id=f"{repo}:{path}", repo=repo, service=service, file=path, tool="django", version=version, ops=ops,
                     unparsed=bad, parent=parent)


# =====================================================================================================================
# Non-Python / non-Java data files
# =====================================================================================================================
def extract_file(repo: str, service: str, path: str, text: str) -> RepoExtract:
    """Facts from one migration file (SQL or Liquibase changelog). Depends only on (repo, service, path, text)."""
    out = RepoExtract(repo=repo, service=service, commit="", extractor_version=EXTRACTOR_VERSION)
    try:
        if path.endswith(".sql"):
            ops, bad = parse_sql(text)
            tool, version = "flyway" if re.match(r"^[vVrR]", posixpath.basename(path)) else "sql", _flyway_version(path)
        else:
            if path.endswith(".xml"):
                ok, ops, bad = _lb_xml(text)
            elif path.endswith(".json"):
                ok, ops, bad = _lb_data(json.loads(text), text)
            else:
                docs = [d for d in yaml.safe_load_all(text) if d is not None]
                ok, ops, bad = next(((o, a, b) for o, a, b in (_lb_data(d, text) for d in docs) if o), (None, [], []))
            if not ok:
                return out                                   # not a Liquibase changelog (e.g. CHANGELOG.yaml release notes)
            tool, version = "liquibase", posixpath.basename(path)
    except (ElementTree.ParseError, ValueError, yaml.YAMLError, json.JSONDecodeError) as e:
        out.files_failed.append({"file": path, "error": f"cannot parse migration: {str(e)[:80]}"})
        return out
    out.migrations.append(Migration(id=f"{repo}:{path}", repo=repo, service=service, file=path, tool=tool, version=version,
                                    ops=ops, unparsed=bad))
    return out


# =====================================================================================================================
# Assembly-time resolution
# =====================================================================================================================
SPRING_DATA_BASES = {"Repository", "CrudRepository", "PagingAndSortingRepository", "ListCrudRepository",
                     "ListPagingAndSortingRepository", "JpaRepository", "ReactiveCrudRepository", "RxJava3CrudRepository"}
_QUERY_TARGET = re.compile(r"\b(from|join|update|into)\s+([A-Za-z_][\w.$]*)", re.I)


def _op_of_method(name: str) -> Optional[str]:
    low = name.lower()
    if low.startswith(WRITE_PREFIXES):
        return "write"
    if low.startswith(READ_PREFIXES):
        return "read"
    return None


def _query_targets(query: str, entities_by_key: dict) -> list:
    """[(entity, op)] for the entities/tables a JPQL or native query names: UPDATE/DELETE/INSERT targets are written, FROM/JOIN read."""
    out = []
    head = query.strip().split(None, 1)[0].lower() if query.strip() else ""
    for m in _QUERY_TARGET.finditer(query):
        kw, name = m.group(1).lower(), m.group(2).split(".")[-1].lower()
        ent = entities_by_key.get(name)
        if ent is None:
            continue
        write = kw in ("update", "into") or (kw == "from" and head == "delete" and m.start() == query.lower().find("from"))
        out.append((ent, "write" if write else "read"))
    return out


def _auth_base(name: str, table: str, model: str) -> tuple:
    """Columns and link tables that Django's django.contrib.auth base models add (they are not defined in the repository)."""
    cols: dict = {}
    spec = lambda ty, null=False: {"field": "", "type": ty, "nullable": null, "pk": False}      # noqa: E731
    if name in ("AbstractBaseUser", "AbstractUser"):
        cols.update({"password": spec("charfield(128)"), "last_login": spec("datetimefield", True)})
    if name in ("PermissionsMixin", "AbstractUser"):
        cols["is_superuser"] = spec("booleanfield")
    if name == "AbstractUser":
        cols.update({"username": spec("charfield(150)"), "first_name": spec("charfield(150)"), "last_name": spec("charfield(150)"),
                     "email": spec("emailfield(254)"), "is_staff": spec("booleanfield"), "is_active": spec("booleanfield"),
                     "date_joined": spec("datetimefield")})
    tables = []
    if name in ("PermissionsMixin", "AbstractUser"):
        for att, target in (("groups", "group"), ("user_permissions", "permission")):
            tables.append({"table": f"{table}_{att}", "columns": {c: {"field": att, **s} for c, s in
                                                                 m2m_through("", model, target).items()}})
    return cols, tables


DJANGO_AUTH_BASES = {"AbstractBaseUser", "AbstractUser", "PermissionsMixin"}


def merge_entity_columns(entities: list) -> list:
    """Fold mapped superclasses, mixins, abstract models and @Embedded types into the entities that extend them, and give
    owning relationships their join columns. Returns the unknowns this raises: Django models that inherit columns from a base
    that is not defined in the repository."""
    unknowns: list = []
    by_name: dict = {}
    for e in entities:
        by_name.setdefault(e.name, []).append(e)

    def pick(name: str, kind: str):
        c = [x for x in by_name.get(name, []) if x.kind == kind]
        return c[0] if len(c) == 1 else None

    def merged(e: Entity, seen: tuple = ()) -> dict:
        cols: dict = {}
        for b in list(e.bases) + list(e.embedded):
            p = pick(b, e.kind)
            if p is not None and p.abstract and p.id not in seen and p is not e:
                cols.update(merged(p, seen + (e.id,)))
        cols.update(e.columns)
        return cols
    full = {e.id: merged(e) for e in entities}
    for e in entities:
        e.columns = dict(full[e.id])
    for e in entities:
        for j in e.joins:
            target = pick(j["target"], e.kind)
            pk = next((c for c, spec in (target.columns if target else {}).items() if spec.get("pk")), "id")
            col = (j.get("name") or f"{snake(j['field'])}_{pk}").lower()
            e.columns[col] = {"field": j["field"], "type": f"fk({j['target']})", "nullable": j.get("nullable", True),
                              "pk": bool(j.get("pk"))}
            if not j.get("name") and not target:
                e.notes.append(f"join column of {j['field']} assumed to be {col} (target entity {j['target']} not analysed)")
        e.joins = []
        if e.kind == "django" and not e.abstract:
            for b in e.bases:
                if pick(b, "django") is not None:
                    continue
                if b in DJANGO_AUTH_BASES:
                    cols, tables = _auth_base(b, e.table, e.name)
                    e.columns = {**cols, **e.columns}
                    e.join_tables += [t for t in tables if t["table"] not in {x["table"] for x in e.join_tables}]
                else:
                    unknowns.append({"file": e.file, "line": e.line, "kind": "unresolved_base_model",
                                     "reason": f"{e.name} inherits from {b}, which is not defined in this repository: the columns "
                                               "it adds are not analysed"})
            if not any(s.get("pk") for s in e.columns.values()):
                e.columns = {"id": {"field": "id", "type": "autofield", "nullable": False, "pk": True}, **e.columns}
    return unknowns


def resolve_data(out: RepoExtract, smap: dict) -> None:
    """Resolve what needs several files: inherited columns, join columns, Django table overrides, and data access through
    Spring Data repositories and Python model references. Runs before resolve_clients (it reads typed_calls)."""
    entities = list(out.entities)
    unknowns = list(out.data_unknowns)
    unknowns += merge_entity_columns(entities)
    # Django: a model whose table is named by Meta.db_table (on the model or in its CreateModel) keeps that name everywhere
    declared = {(e.app, e.name.lower()): e.table for e in entities if e.kind == "django" and e.table_source == "declared"}
    for m in out.migrations:
        for o in m.ops:
            if o.get("declared") and o.get("app"):
                declared[(o["app"], o["model"])] = o["table"]
    for m in out.migrations:
        if m.tool != "django":
            continue
        for o in m.ops:
            key = (o.get("app"), o.get("model"))
            if key in declared and o["op"] != "rename_table":
                o["table"] = declared[key]
    concrete = [e for e in entities if not e.abstract]
    for e in concrete:
        if e.kind == "django" and (e.app, e.name.lower()) in declared:
            e.table, e.table_source = declared[(e.app, e.name.lower())], "declared"
    names: dict = {}
    for e in concrete:
        names.setdefault(e.name, []).append(e)
    for nm, group in names.items():
        if len(group) > 1:
            for e in group:
                e.id = f"{e.id}~{e.file}"
    for e in concrete:
        if e.table_source == "unresolved" or not e.table:
            unknowns.append({"file": e.file, "line": e.line, "kind": "entity_table_unresolved",
                             "reason": f"table of {e.name} could not be determined statically"})
    out.entities = sorted((e for e in concrete if e.table), key=lambda e: e.id)
    if (smap or {}).get("jpa_custom_naming") and any(e.kind == "jpa" for e in out.entities):
        unknowns.append({"file": "", "line": 0, "kind": "custom_naming_strategy",
                         "reason": "a custom Hibernate naming strategy is configured: table and column names that are not "
                                   "declared explicitly may differ from the names derived here"})
    by_unique = {n: g[0] for n, g in names.items() if len(g) == 1 and g[0].table}
    by_key = {e.name.lower(): e for e in by_unique.values()}
    by_key.update({e.table: e for e in by_unique.values()})

    access: dict = {}

    def add(fn, ent, op, file, line, via):
        k = (fn, ent.id, op)
        if k not in access or line < access[k].line:
            access[k] = DataAccess(function_id=fn, entity=ent.id, op=op, file=file, line=line, via=via)
    # ---- Spring Data repositories ------------------------------------------------------------------------------------
    repos = {}
    for r in out.repositories:
        repos.setdefault(r["name"], []).append(r)
    for tc in sorted(out.typed_calls, key=lambda t: (t["file"], t["line"], t["method"])):
        cands = repos.get(tc["type"], [])
        if not cands:
            continue
        r = cands[0]
        ent = by_unique.get(r["entity"])
        via = f"{r['name']}.{tc['method']}"
        if ent is None:
            unknowns.append({"file": tc["file"], "line": tc["line"], "kind": "repository_entity_unresolved",
                             "reason": f"repository {r['name']} manages {r['entity']}, which is not a mapped entity analysed in this repository"})
            continue
        meth = r["methods"].get(tc["method"])
        if meth and meth.get("query"):
            head = meth["query"].strip().split(None, 1)[0].lower() if meth["query"].strip() else ""
            op = "write" if meth.get("modifying") or head in ("update", "delete", "insert") else "read"
            add(tc["function_id"], ent, op, tc["file"], tc["line"], via)
            for other, o in _query_targets(meth["query"], by_key):
                if other.id != ent.id:
                    add(tc["function_id"], other, o, tc["file"], tc["line"], via)
            if meth.get("native"):
                for t in re.findall(r"\b(?:from|join|update|into)\s+([A-Za-z_][\w.$]*)", meth["query"], re.I):
                    if t.split(".")[-1].lower() not in by_key:
                        unknowns.append({"file": tc["file"], "line": tc["line"], "kind": "native_query_table_unmapped",
                                         "reason": f"native query of {via} touches table {t}, which no analysed entity maps"})
            continue
        op = _op_of_method(tc["method"])
        if op is None:
            unknowns.append({"file": tc["file"], "line": tc["line"], "kind": "repository_method_unknown",
                             "reason": f"{via}: the effect of this repository method on {ent.name} cannot be determined"})
            continue
        add(tc["function_id"], ent, op, tc["file"], tc["line"], via)
    # ---- Python model references -------------------------------------------------------------------------------------
    for ref in sorted(out.data_refs, key=lambda x: (x["file"], x["line"], x["model"])):
        cands = names.get(ref["model"], [])
        if not cands:
            continue
        if len(cands) > 1:
            unknowns.append({"file": ref["file"], "line": ref["line"], "kind": "model_name_ambiguous",
                             "reason": f"model name {ref['model']} matches {len(cands)} mapped models; access not attributed"})
            continue
        if not cands[0].table:
            continue
        add(ref["function_id"], cands[0], ref["op"], ref["file"], ref["line"], ref["via"])
    out.data_access = sorted(access.values(), key=lambda a: (a.function_id, a.entity, a.op))
    out.data_unknowns = sorted({(u["file"], u["line"], u["kind"], u["reason"]): u for u in unknowns}.values(),
                               key=lambda u: (u["file"], u["line"], u["kind"]))
    out.repositories, out.data_refs = [], []
    out.migrations.sort(key=lambda m: m.id)
