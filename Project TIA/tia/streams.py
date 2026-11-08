"""Event-stream and data-pipeline lineage (PRD FR-042).

Multi-hop event paths need no special code: a consumer's handler that publishes again links topic -> listener -> function -> topic, and
the traversal follows it. This module adds the pipelines declared in source control, which are not ordinary producers/consumers:

  Kafka Streams    builder.stream("a") / .table("a") / .globalTable("a") consume; .to("b") / .through("b") produce (Java, Kotlin, Scala);
                   topic names given as literals, constants in the same file, or Arrays.asList / List.of of them.
  Spark            readStream/read .option("subscribe", "a,b") consumes; writeStream/write .option("topic", "b") produces;
                   spark.table / read.table read a table; saveAsTable / insertInto / toTable write one (Python, Scala, Java). SQL passed to
                   spark.sql(...) is read by the SQL lineage (FR-041).
  Kafka Connect    connector configurations (JSON, with or without a "config" object, or .properties) under a path naming "connect":
                   sink connectors consume their topics and write tables (table.name.format, default the topic name); JDBC source and
                   Debezium connectors read tables and produce topics (topic.prefix / database.server.name + table).

Statements outside any function (a Spark job script) belong to a job node for the file; a connector is a pipeline node of its own. Both are
graph functions, so a change to the job or the connector configuration reaches what they publish and write. Topic patterns
(subscribePattern) and names built at run time are reported as unknowns, never ignored.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from .extract_data import norm_ident
from .models import Function

_STR = r"""["']([^"'\n]+)["']"""


def _constants(text: str) -> dict:
    out = {}
    for m in re.finditer(r"(?:static\s+final\s+String|final\s+String|const(?:\s+val)?|val|String|var|let)\s+(\w+)\s*(?::\s*String\s*)?=\s*" + _STR, text):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r"^([A-Z_][A-Z0-9_]*)\s*=\s*" + _STR, text, re.M):
        out[m.group(1)] = m.group(2)
    return out


def _args(raw: str, consts: dict) -> tuple:
    """Topic / table names in an argument expression -> (names, unresolved expression or None)."""
    raw = raw.strip()
    if raw.startswith("Pattern.compile"):
        return [], raw
    m = re.fullmatch(r"(?:Arrays\.asList|List\.of|Set\.of|listOf|setOf|Seq|Collections\.singletonList)\s*\((.*)\)", raw, re.S)
    items = [x.strip() for x in m.group(1).split(",")] if m else [raw]
    names = []
    for it in items:
        lit = re.fullmatch(_STR, it)
        if lit:
            names += [x.strip() for x in lit.group(1).split(",") if x.strip()]     # Spark: "a,b" lists several topics
        elif re.fullmatch(r"[A-Za-z_][\w.]*", it) and it.split(".")[-1] in consts:
            names.append(consts[it.split(".")[-1]])
        else:
            return [], raw
    return names, None


def _owner(functions: list, line: int) -> Optional[str]:
    inside = [f for f in functions if f.line <= line <= (f.end_line or f.line)]
    return min(inside, key=lambda f: (f.end_line or f.line) - f.line).id if inside else None


def stream_facts(repo: str, path: str, text: str, functions: list) -> tuple:
    """Kafka Streams and Spark in a source file -> (events, sql_access, job_functions, unknowns)."""
    streams = re.search(r"StreamsBuilder|KStream|KTable|org\.apache\.kafka\.streams", text) is not None
    spark = re.search(r"\breadStream\b|\bwriteStream\b|SparkSession|pyspark|org\.apache\.spark|\bspark\.(read|table|sql)\b", text) is not None
    if not (streams or spark):
        return [], [], [], []
    consts = _constants(text)
    events, access, unknowns, job_lines = [], [], [], []
    job_id = f"{repo}:{path}::<job>"

    def owner(line):
        fid = _owner(functions, line)
        if fid is None:
            job_lines.append(line)
            return job_id
        return fid

    def line_of(pos):
        return text.count("\n", 0, pos) + 1

    def emit(kind, raw, pos, via):
        names, bad = _args(raw, consts)
        line = line_of(pos)
        if bad is not None:
            unknowns.append({"file": path, "line": line, "kind": "event_topic_unresolved",
                             "reason": f"{via}: topic {bad[:60]} is a pattern or is named at run time"})
        for t in names:
            events.append({"kind": kind, "topic": t.strip(), "function_id": owner(line), "file": path, "line": line, "via": via})
    if streams:
        for m in re.finditer(r"\.(stream|table|globalTable)\s*\(\s*([^()]*(?:\([^()]*\))?[^()]*?)\s*[,)]", text):
            emit("consumes", m.group(2), m.start(), f"KafkaStreams.{m.group(1)}")
        for m in re.finditer(r"\.(to|through)\s*\(\s*([^(),]+)\s*[,)]", text):
            emit("produces", m.group(2), m.start(), f"KafkaStreams.{m.group(1)}")
    if spark:
        for m in re.finditer(r"""\.option\s*\(\s*["'](subscribe|subscribePattern|assign)["']\s*,\s*([^)]+)\)""", text):
            if m.group(1) == "subscribe":
                emit("consumes", m.group(2), m.start(), "Spark.readStream(kafka)")
            else:
                unknowns.append({"file": path, "line": line_of(m.start()), "kind": "event_topic_unresolved",
                                 "reason": f"Spark {m.group(1)} {m.group(2).strip()[:60]}: the topics are chosen at run time"})
        for m in re.finditer(r"""\.option\s*\(\s*["']topic["']\s*,\s*([^)]+)\)""", text):
            emit("produces", m.group(1), m.start(), "Spark.write(kafka)")
        for m in re.finditer(r"""\b(?:spark|read)\s*\.\s*table\s*\(\s*([^)]+)\)""", text):
            for t in _args(m.group(1), consts)[0]:
                line = line_of(m.start())
                access.append({"function_id": owner(line), "op": "read", "target": norm_ident(t), "file": path, "line": line,
                               "sql": f"spark.table({t})", "columns": ["*"]})
        for m in re.finditer(r"""\.(saveAsTable|insertInto|toTable)\s*\(\s*([^),]+)""", text):
            for t in _args(m.group(2), consts)[0]:
                line = line_of(m.start())
                access.append({"function_id": owner(line), "op": "write", "target": norm_ident(t), "file": path, "line": line,
                               "sql": f"{m.group(1)}({t})", "columns": ["*"]})
    jobs = []
    if job_lines:
        jobs.append(Function(id=job_id, repo=repo, file=path, qualname=f"<job {path.rsplit('/', 1)[-1]}>", line=min(job_lines),
                             hash=hashlib.sha256(text.encode()).hexdigest()[:16], end_line=max(job_lines),
                             body_hash=hashlib.sha256(text.encode()).hexdigest()[:16]))
    return events, access, jobs, unknowns


# ---- Kafka Connect -----------------------------------------------------------------------------------------------------------------
def is_connector_path(path: str) -> bool:
    p = "/" + path.lower()
    return p.endswith((".json", ".properties")) and ("connect" in p) and "/node_modules/" not in p and "package" not in p.rsplit("/", 1)[-1]


def _connector_config(path: str, text: str) -> Optional[dict]:
    if path.lower().endswith(".json"):
        try:
            d = json.loads(text)
        except ValueError:
            return None
        if isinstance(d, dict) and isinstance(d.get("config"), dict):
            d = {**d["config"], "name": d.get("name") or d["config"].get("name")}
        return d if isinstance(d, dict) and "connector.class" in d else None
    d = {}
    for line in text.splitlines():
        m = re.match(r"\s*([\w.]+)\s*[=:]\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith(("#", "!")):
            d[m.group(1)] = m.group(2)
    return d if "connector.class" in d else None


def connector_facts(repo: str, path: str, text: str) -> tuple:
    """A Kafka Connect connector configuration -> (events, sql_access, [pipeline function], unknowns); empty when it is not one."""
    c = _connector_config(path, text)
    if not c:
        return [], [], [], []
    name = str(c.get("name") or path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    fid = f"{repo}:{path}::connector:{name}"
    cls = str(c["connector.class"])
    digest = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16]
    fn = Function(id=fid, repo=repo, file=path, qualname=f"connector {name}", line=1, hash=digest, body_hash=digest, end_line=1)
    events, access, unknowns = [], [], []
    via = cls.rsplit(".", 1)[-1]

    def tables(key_list):
        for k in key_list:
            if c.get(k):
                return [t.strip() for t in str(c[k]).split(",") if t.strip()]
        return []
    if "sink" in cls.lower():
        topics = [t.strip() for t in str(c.get("topics") or "").split(",") if t.strip()]
        if c.get("topics.regex"):
            unknowns.append({"file": path, "line": 1, "kind": "event_topic_unresolved", "reason": f"connector {name}: topics.regex {c['topics.regex']}"})
        fmt = str(c.get("table.name.format") or "${topic}")
        for t in topics:
            events.append({"kind": "consumes", "topic": t, "function_id": fid, "file": path, "line": 1, "via": via})
            access.append({"function_id": fid, "op": "write", "target": norm_ident(fmt.replace("${topic}", t)), "file": path, "line": 1,
                           "sql": f"{via} -> {fmt}", "columns": ["*"]})
    else:
        tbls = tables(("table.include.list", "table.whitelist", "table.includelist", "tables"))
        prefix = str(c.get("topic.prefix") or c.get("database.server.name") or "")
        if c.get("query"):
            unknowns.append({"file": path, "line": 1, "kind": "dynamic_sql", "reason": f"connector {name}: custom query is not mapped to tables"})
        debezium = "debezium" in cls.lower()
        for t in tbls:
            access.append({"function_id": fid, "op": "read", "target": norm_ident(t), "file": path, "line": 1, "sql": f"{via} <- {t}",
                           "columns": ["*"]})
            topic = f"{prefix}.{t}" if debezium and prefix else f"{prefix}{t.split('.')[-1]}"
            events.append({"kind": "produces", "topic": topic, "function_id": fid, "file": path, "line": 1, "via": via})
        if not tbls and not c.get("query"):
            unknowns.append({"file": path, "line": 1, "kind": "event_topic_unresolved",
                             "reason": f"connector {name} ({via}): no table list, so the topics it produces are unknown"})
    return events, access, [fn], unknowns
