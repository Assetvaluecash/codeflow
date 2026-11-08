"""SQL and stored-procedure data lineage (PRD FR-041, AC-037).

Sources
  * Stored procedures, functions, triggers and views defined in .sql files (PostgreSQL CREATE [OR REPLACE] FUNCTION/PROCEDURE with $$ bodies,
    Oracle CREATE OR REPLACE PROCEDURE ... IS/AS ... END and package bodies, SQL Server CREATE [OR ALTER] PROC[EDURE] ... AS ... (GO), DB2
    CREATE PROCEDURE ... LANGUAGE SQL BEGIN ... END): the tables each one reads and writes and the procedures it calls.
  * SQL embedded in application code, in any supported language: string literals that are SQL statements (adjacent literals and `+`
    concatenations are joined first) are attributed to the function that contains them, and procedure calls made through an API
    (cursor.callproc, SimpleJdbcCall.withProcedureName, @Procedure / @NamedStoredProcedureQuery, SqlCommand with CommandType.StoredProcedure).
Statements are read for their table references: INSERT INTO / UPDATE / DELETE FROM / MERGE INTO / TRUNCATE write; FROM / JOIN / USING read;
CALL / EXEC[UTE] / {call ...} call a procedure. CTE names, temporary tables (#t, @t), table functions and sub-queries are not tables.
Dynamic SQL (EXECUTE IMMEDIATE, sp_executesql, EXEC(@sql), a table name built from a variable or a format placeholder) cannot be read and is
reported as an unknown, never as "no access". Nothing is executed.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from .extract_data import norm_ident

DIALECTS = ("postgresql", "oracle", "sqlserver", "db2")
DYN = "__tia_dynamic__"
_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_#@][\w$#@]*)'
_QNAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"
_SQL_START = re.compile(r"^\s*(\(\s*)?(select|insert|update|delete|merge|with|call|exec|execute|truncate|upsert|replace\s+into|\{\s*\??=?\s*call)\b", re.I)
_NOT_TABLES = {"dual", "sysdummy1", "unnest", "generate_series", "lateral", "values", "table", "only"}
_KEYWORDS = {"select", "where", "set", "on", "group", "order", "having", "limit", "union", "as", "into", "values", "returning", "inner", "left",
             "right", "full", "outer", "cross", "join", "using", "when", "then", "and", "or", "not", "exists", "with", "from", "by", "offset",
             "fetch", "for", "window", "except", "intersect", "lateral", "natural"}


# =====================================================================================================================
# One statement
# =====================================================================================================================
def _strip(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)                     # string values never name tables
    return sql


def _name(raw: str) -> Optional[str]:
    raw = raw.strip()
    if DYN in raw:
        return DYN
    if raw.startswith(("#", "@")) or raw.split(".")[-1].strip().startswith(("#", "@")):
        return None                                                  # temporary table / table variable
    n = norm_ident(raw)
    return None if not n or n in _NOT_TABLES or n in _KEYWORDS else n


def statement_refs(sql: str) -> dict:
    """-> {reads, writes, calls: sorted lists, dynamic: reason or None}."""
    s = _strip(sql)
    low = s.lower()
    out = {"reads": set(), "writes": set(), "calls": set(), "dynamic": None}
    if re.search(r"\bexecute\s+immediate\b|\bsp_executesql\b|\bexec(?:ute)?\s*\(\s*@|\bexecute\s+format\s*\(|\bexecute\s+'|\bexecute\s+\w+\s*\|\|", low):
        out["dynamic"] = "dynamic SQL (EXECUTE IMMEDIATE / sp_executesql / EXEC(@sql)) cannot be read"
    ctes = {norm_ident(m.group(1)) for m in re.finditer(rf"(?:\bwith\s+(?:recursive\s+)?|,\s*)({_IDENT})\s*(?:\([^)]*\))?\s+as\s*\(", s, re.I)}

    def add(kind, raw):
        n = _name(raw)
        if n == DYN:
            out["dynamic"] = out["dynamic"] or "a table or procedure name is built at run time"
        elif n and n not in ctes:
            out[kind].add(n)
    for m in re.finditer(rf"\b(?:insert\s+(?:all\s+)?into|replace\s+into|upsert\s+into|merge\s+into|truncate\s+table|truncate)\s+({_QNAME})", s, re.I):
        add("writes", m.group(1))
    for m in re.finditer(rf"\bupdate\s+(?:only\s+)?({_QNAME})\s+(?:as\s+\w+\s+|\w+\s+)?set\b", s, re.I):
        add("writes", m.group(1))
    for m in re.finditer(rf"\bdelete\s+(?:from\s+)?(?:only\s+)?({_QNAME})", s, re.I):
        if m.group(1).lower() != "from":
            add("writes", m.group(1))
    for m in re.finditer(rf"\b(?:from|join|using)\s+({_QNAME})(\s*\()?", s, re.I):
        prev = low[max(0, m.start() - 12):m.start()]
        if m.group(2) or re.search(r"delete\s+$", prev):            # table function call / DELETE FROM (already a write)
            continue
        add("reads", m.group(1))
        tail = s[m.end():]                                            # FROM a, b, c (comma joins)
        for c in re.finditer(rf"^\s*(?:(?:as\s+)?\w+\s*)?,\s*({_QNAME})(\s*\()?", tail, re.I):
            if not c.group(2):
                add("reads", c.group(1))
    for m in re.finditer(rf"(?:\{{\s*\??\s*=?\s*call|\bcall|\bexec(?:ute)?(?:\s+procedure)?|\bperform)\s+(?!immediate\b)({_QNAME})", s, re.I):
        n = _name(m.group(1))
        if n == DYN:
            out["dynamic"] = out["dynamic"] or "the called procedure is named at run time"
        elif n and n not in ("sp_executesql",) and not m.group(1).startswith("@"):
            out["calls"].add(n)
    for k in ("reads", "writes", "calls"):
        out[k] = sorted(out[k] - ({DYN} if k != "calls" else set()))
    out["reads"] = [t for t in out["reads"] if t not in out["writes"] or re.search(rf"\b(from|join)\s+[\w.\"\[\]`]*{re.escape(t)}\b", low)]
    return out


def columns_named(sql: str) -> list:
    """Identifiers a statement or body names (candidate column names), plus "*" when it uses every column (SELECT *, INSERT without a
    column list). Used to link a statement to a column of a table it touches only when it names that column."""
    s = _strip(sql).lower()
    names = {w for w in re.findall(r"[a-z_][a-z0-9_$#]*", s) if w not in _KEYWORDS and len(w) > 1}
    if re.search(r"\bselect\s+(distinct\s+)?(\w+\.)?\*|\binsert\s+into\s+[\w.\"\[\]`]+\s+(select|values)\b", s):
        names.add("*")
    return sorted(names)[:600]


def is_sql(text: str) -> bool:
    return bool(_SQL_START.match(text or "")) and len(text.strip()) > 8 and not re.match(r"^\s*(select|update|delete|exec)\s*$", text, re.I)


# =====================================================================================================================
# Procedure definitions in .sql files
# =====================================================================================================================
_DEF = re.compile(r"\bcreate\s+(?:or\s+(?:replace|alter)\s+)?(?:definer\s*=\s*\S+\s+)?(?:editionable\s+)?"
                  rf"(procedure|proc|function|trigger|view|materialized\s+view|package\s+body)\s+(?:if\s+not\s+exists\s+)?({_QNAME})", re.I)
_PKG_MEMBER = re.compile(rf"(?:^|\s)(procedure|function)\s+({_IDENT})\b[^;]*?\b(?:is|as)\b", re.I)


def is_sql_source(path: str) -> bool:
    p = path.lower()
    return p.endswith((".sql", ".pls", ".plsql", ".pks", ".pkb", ".prc", ".fnc", ".trg", ".vw", ".db2"))


def _body_end(text: str, start: int) -> int:
    """End of a definition: its dollar-quoted body (PostgreSQL), a GO batch separator (SQL Server), a '/' line (Oracle), or the next CREATE."""
    m = re.compile(r"\$(\w*)\$").search(text, start, start + 4000)
    if m and re.match(r"[^;]*$", text[start:m.start()].replace("\n", " ")):
        end = text.find(m.group(0), m.end())
        if end > 0:
            return end + len(m.group(0))
    stops = [x.start() for x in (re.compile(r"^\s*go\s*$", re.I | re.M).search(text, start),
                                 re.compile(r"^\s*/\s*$", re.M).search(text, start),
                                 re.compile(r"\bcreate\s+(?:or\s+(?:replace|alter)\s+)?(?:procedure|proc|function|trigger|view|package)\b", re.I)
                                 .search(text, start + 10)) if x]
    return min(stops) if stops else len(text)


def parse_definitions(path: str, text: str) -> tuple:
    """-> (procedures, unknowns). procedures: [{name, kind, reads, writes, calls, line, end_line, digest, signature}]"""
    procs, unknowns = [], []
    for m in _DEF.finditer(text):
        kind = re.sub(r"\s+", "_", m.group(1).lower()).replace("proc_", "procedure_")
        kind = "procedure" if kind == "proc" else kind
        start, end = m.start(), _body_end(text, m.end())
        body = text[m.end():end]
        line = text.count("\n", 0, start) + 1
        if kind == "package_body":
            pkg = norm_ident(m.group(2))
            members = list(_PKG_MEMBER.finditer(body))
            for i, mm in enumerate(members):
                mbody = body[mm.end(): members[i + 1].start() if i + 1 < len(members) else len(body)]
                procs.append(_definition(f"{pkg}.{norm_ident(mm.group(2))}", mm.group(1).lower(), mbody, path,
                                         line + body.count("\n", 0, mm.start()), unknowns))
            continue
        if kind == "trigger":
            on = re.search(rf"\bon\s+({_QNAME})", body, re.I)
            d = _definition(norm_ident(m.group(2)), "trigger", body, path, line, unknowns)
            if on:
                d["on_table"] = norm_ident(on.group(1))
            procs.append(d)
            continue
        procs.append(_definition(norm_ident(m.group(2)), kind, body, path, line, unknowns))
    return procs, unknowns


def _definition(name: str, kind: str, body: str, path: str, line: int, unknowns: list) -> dict:
    head = re.split(r"\b(?:is|as|begin|returns)\b|\$\w*\$", body, 1, flags=re.I)[0]
    sig = " ".join(head.split())[:300]
    reads, writes, calls = set(), set(), set()
    inner = re.sub(r"^.*?\$(\w*)\$(.*)\$\1\$.*$", r"\2", body, flags=re.S)
    for i, stmt in enumerate(re.split(r";", inner)):
        r = statement_refs(stmt)
        reads |= set(r["reads"])
        writes |= set(r["writes"])
        calls |= set(r["calls"])
        if r["dynamic"]:
            unknowns.append({"file": path, "line": line, "kind": "dynamic_sql", "reason": f"{kind} {name}: {r['dynamic']}"})
    calls.discard(name)
    return {"name": name, "kind": kind, "reads": sorted(reads - writes | (reads & writes)), "writes": sorted(writes), "calls": sorted(calls),
            "line": line, "end_line": line + body.count("\n"), "file": path, "signature": sig, "columns": columns_named(inner),
            "digest": hashlib.sha256(" ".join(_strip(body).split()).lower().encode()).hexdigest()[:16]}


# =====================================================================================================================
# SQL embedded in application code
# =====================================================================================================================
_LITERAL = re.compile(r'(?P<q>"""|\'\'\'|@"|\$@"|@\$"|\$"|"|\'|`)', re.S)
_API_CALLS = [
    re.compile(r"\bcallproc\s*\(\s*[\"']([\w.$]+)[\"']"),                                         # Python DB-API
    re.compile(r"\bwithProcedureName\s*\(\s*\"([\w.$]+)\""),                                       # Spring SimpleJdbcCall
    re.compile(r"\bwithFunctionName\s*\(\s*\"([\w.$]+)\""),
    re.compile(r"@Procedure\s*\(\s*(?:procedureName\s*=\s*|value\s*=\s*|name\s*=\s*)?\"([\w.$]+)\""),  # Spring Data JPA
    re.compile(r"procedureName\s*=\s*\"([\w.$]+)\""),                                              # @NamedStoredProcedureQuery
    re.compile(r"createStoredProcedureQuery\s*\(\s*\"([\w.$]+)\""),                                # JPA EntityManager
]
_CS_PROC = re.compile(r"new\s+SqlCommand\s*\(\s*\"([\w.$\[\]]+)\"[^;]*;(?:(?!new\s+SqlCommand).){0,400}?CommandType\.StoredProcedure", re.S)


def _literals(text: str) -> list:
    """[(start, end, value)] for string literals in source text (languages differ; this is a lexical approximation that knows escapes,
    triple quotes, verbatim and interpolated C# strings, JavaScript templates and Go raw strings). Interpolations become the DYN marker."""
    out, i, n = [], 0, len(text)
    while i < n:
        m = _LITERAL.search(text, i)
        if not m:
            break
        q = m.group("q")
        start, j = m.start(), m.end()
        if q in ('"""', "'''"):
            end = text.find(q, j)
            end = n if end < 0 else end
            val, nxt = text[j:end], end + 3
        elif q in ("@\"", "$@\"", "@$\""):
            k = j
            while k < n and not (text[k] == '"' and (k + 1 >= n or text[k + 1] != '"')):
                k += 2 if text[k] == '"' else 1
            val, nxt = text[j:k].replace('""', '"'), k + 1
        else:
            close = '"' if q in ('"', '$"') else q
            k = j
            while k < n and text[k] != close and (text[k] != "\n" or q == "`"):
                k += 2 if text[k] == "\\" else 1
            val, nxt = text[j:k], k + 1
        if q == "`":
            val = re.sub(r"\$\{[^}]*\}", DYN, val)
        if "$" in q or (start > 0 and text[start - 1] in "fF" and q in ('"', "'", '"""', "'''")):
            val = re.sub(r"\{[^{}]*\}", DYN, val)                    # C# $"...{x}" / Python f"...{x}"
        out.append((start, nxt, val))
        i = nxt
    return out


def _joined(text: str, lits: list) -> list:
    """Adjacent literals and `+` concatenations as one string; a concatenated non-literal becomes DYN. -> [(start, value)]"""
    groups, cur = [], None
    for k, (s, e, v) in enumerate(lits):
        if cur is not None:
            between = text[cur[1]:s]
            if re.fullmatch(r"\s*\+?\s*", between):                   # "a" "b"  /  "a" + "b"
                cur = (cur[0], e, cur[2] + v)
                continue
            if re.fullmatch(r"\s*\+\s*[\w.()\[\]]+\s*\+\s*", between):  # "a" + table + "b"
                cur = (cur[0], e, cur[2] + DYN + v)
                continue
            tail = re.match(r"\s*\+\s*[\w.()\[\]]+", text[cur[1]:cur[1] + 80])
            groups.append((cur[0], cur[2] + (DYN if tail else "")))
        cur = (s, e, v)
    if cur is not None:
        tail = re.match(r"\s*\+\s*[\w.()\[\]]+", text[cur[1]:cur[1] + 80])
        groups.append((cur[0], cur[2] + (DYN if tail else "")))
    return groups


def embedded(path: str, text: str, functions: list) -> tuple:
    """-> (accesses, unknowns, covered_lines). accesses: [{function_id, op: read|write|call, target, file, line, sql}]. Only statements inside
    a function are attributed; the lines of every statement that was read (dynamic or not) are returned so callers can drop the generic
    'raw SQL not analysed' entries they replace."""
    spans = sorted(((f.line, f.end_line or f.line, f.id) for f in functions), key=lambda x: (x[1] - x[0], x[0]))

    def owner(line):
        return next((fid for a, b, fid in spans if a <= line <= b), None)
    acc, unk, covered = [], [], set()
    for start, val in _joined(text, _literals(text)):
        v = re.sub(r"%\((\w+)\)s|%s|\?|:\w+|\$\d+|@\w+", "0", val) if DYN not in val else val
        v = re.sub(r"(?<![\w$])\{\d*\}", DYN, v)                      # "... from {}".format(t)
        if not is_sql(v.replace(DYN, " x ")):
            continue
        line = text.count("\n", 0, start) + 1
        fid = owner(line)
        if fid is None:
            continue
        covered.add((fid, line))
        r = statement_refs(v)
        snippet = " ".join(val.replace(DYN, "<expr>").split())[:120]
        cols = columns_named(v.replace(DYN, " "))
        for op, key in (("read", "reads"), ("write", "writes"), ("call", "calls")):
            for t in r[key]:
                acc.append({"function_id": fid, "op": op, "target": t, "file": path, "line": line, "sql": snippet,
                            **({"columns": cols} if op != "call" else {})})
        if r["dynamic"]:
            unk.append({"file": path, "line": line, "kind": "dynamic_sql", "reason": f"{fid.split('::')[-1]}: {r['dynamic']}: {snippet}"})
    for rx in _API_CALLS:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            fid = owner(line)
            if fid:
                covered.add((fid, line))
                acc.append({"function_id": fid, "op": "call", "target": norm_ident(m.group(1)), "file": path, "line": line,
                            "sql": m.group(0)[:120]})
    for m in _CS_PROC.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        fid = owner(line)
        if fid:
            acc.append({"function_id": fid, "op": "call", "target": norm_ident(m.group(1)), "file": path, "line": line, "sql": m.group(0)[:120]})
    uniq = {(a["function_id"], a["op"], a["target"], a["line"]): a for a in acc}
    return [uniq[k] for k in sorted(uniq)], unk, covered
