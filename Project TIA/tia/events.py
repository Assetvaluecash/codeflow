"""Event and message contracts (PRD FR-019, FR-020 events, AC-028).

* Producers and consumers come from code (Python kafka-python / confluent-kafka / aiokafka: producer.send/produce, KafkaConsumer(topics),
  consumer.subscribe([...]); Java Spring Kafka: KafkaTemplate.send, @KafkaListener(topics = ...)) and from the owner's declaration in
  tia-services.yaml:
      events:
        produces: [{topic: payments.created, schema: schemas/payment-created.avsc}]
        consumes: [{topic: payments.created}]
* Schemas: Avro (.avsc) and JSON Schema (*.schema.json, or JSON under schemas/ or events/ with "$schema" or "properties"). A schema is
  tied to a topic by the declaration (contract_confirmed) or, failing that, by its file name matching the topic (inferred).
* Compatibility is judged from the consumers' side (they read with the schema they were built against); every rule is configurable
  (compatibility.rules in the tenant configuration).
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Optional

EVENT_RULES = {"event_field_removed": "breaking", "event_field_type_changed": "breaking", "event_enum_value_added": "breaking",
               "event_enum_value_removed": "compatible", "event_field_added": "compatible", "event_required_field_added": "compatible",
               "event_schema_removed": "breaking", "event_topic_no_longer_produced": "breaking"}
TOPIC = re.compile(r"^[A-Za-z0-9._\-]{1,249}$")


def is_event_schema_file(path: str) -> bool:
    p = path.lower()
    if "/node_modules/" in "/" + p:
        return False
    return p.endswith(".avsc") or p.endswith(".schema.json") or (p.endswith(".json") and any(d in "/" + p for d in ("/schemas/", "/events/")))


def _avro_type(t) -> tuple[str, bool, Optional[list]]:
    """-> (type text, nullable, enum symbols)."""
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        inner = _avro_type(non_null[0]) if len(non_null) == 1 else (" | ".join(_avro_type(x)[0] for x in non_null), False, None)
        return inner[0], "null" in t, inner[2]
    if isinstance(t, dict):
        k = t.get("type")
        if k == "enum":
            return f"enum {t.get('name', '')}".strip(), False, [str(s) for s in t.get("symbols") or []]
        if k == "array":
            return f"array<{_avro_type(t.get('items'))[0]}>", False, None
        if k == "map":
            return f"map<{_avro_type(t.get('values'))[0]}>", False, None
        if k == "record":
            return str(t.get("name") or "record"), False, None
        return str(t.get("logicalType") or k), False, None
    return str(t), False, None


def parse_event_schema(path: str, text: str) -> Optional[dict]:
    """-> {"name", "format", "fields": {name: {"type", "required", "allowed"?, "has_default"}}, "topic"} or None when it is not a schema."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("type") == "record" and isinstance(doc.get("fields"), list):
        fields = {}
        for f in doc["fields"]:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            t, nullable, symbols = _avro_type(f.get("type"))
            fields[f["name"]] = {"type": t, "required": "default" not in f and not nullable, "has_default": "default" in f,
                                 **({"allowed": symbols} if symbols else {})}
        name = ".".join(x for x in (doc.get("namespace"), doc.get("name")) if x)
        return {"name": name or posixpath.basename(path), "format": "avro", "fields": fields, "topic": doc.get("x-topic") or doc.get("topic")}
    if "properties" in doc or "$schema" in doc:
        req = set(doc.get("required") or [])
        fields = {}
        for k, v in (doc.get("properties") or {}).items():
            v = v if isinstance(v, dict) else {}
            t = v.get("type") or (v.get("$ref", "").rsplit("/", 1)[-1] if "$ref" in v else "object")
            t = " | ".join(map(str, t)) if isinstance(t, list) else str(t)
            fields[k] = {"type": t, "required": k in req, "has_default": "default" in v,
                         **({"allowed": [str(x) for x in v["enum"]]} if isinstance(v.get("enum"), list) else {})}
        return {"name": str(doc.get("title") or posixpath.basename(path).split(".")[0]), "format": "json-schema", "fields": fields,
                "topic": doc.get("x-topic")}
    return None


def declared_events(smap: dict) -> tuple[list, dict]:
    """tia-services.yaml `events:` -> ([{kind, topic}], {schema path: topic})."""
    ev = (smap or {}).get("events") or {}
    out, schema_topic = [], {}
    if not isinstance(ev, dict):
        return out, schema_topic
    for kind in ("produces", "consumes"):
        for item in ev.get(kind) or []:
            if isinstance(item, str):
                item = {"topic": item}
            if isinstance(item, dict) and isinstance(item.get("topic"), str) and TOPIC.match(item["topic"]):
                out.append({"kind": kind, "topic": item["topic"], "declared": True})
                if item.get("schema"):
                    schema_topic[posixpath.normpath(str(item["schema"]))] = item["topic"]
    return out, schema_topic


def assign_topics(ex, smap: dict):
    """Ties each extracted event schema to its topic (declared > in the schema > file name, the last labelled inferred) and adds the
    owner's declared producers/consumers."""
    declared, schema_topic = declared_events(smap)
    known = {e["topic"] for e in ex.events} | {d["topic"] for d in declared}
    for s in ex.event_schemas:
        t = schema_topic.get(posixpath.normpath(s["file"]))
        if t:
            s["topic"], s["topic_source"] = t, "declared"
        elif s.get("topic"):
            s["topic_source"] = "schema"
        else:
            stem = posixpath.basename(s["file"]).split(".")[0]
            cand = next((k for k in sorted(known) if k.replace(".", "-").replace("_", "-").lower() == stem.replace("_", "-").lower()
                         or k.lower() == stem.lower()), None)
            if cand:
                s["topic"], s["topic_source"] = cand, "file_name"
    for d in declared:
        ex.events.append({"kind": d["kind"], "topic": d["topic"], "function_id": None, "file": "tia-services.yaml", "line": 1, "via": "declared",
                          "declared": True})


def diff_events(base, head, rules: dict = None) -> list:
    """Event contract changes between two revisions of one repository: schema changes per field (consumer's view) and topics no longer
    produced. Each change seeds the schema node (or the topic) so the traversal reaches every known consumer."""
    rules = {**EVENT_RULES, **(rules or {})}
    out = []

    def add(rule, s, detail, seed):
        out.append({"kind": rule, "rule": rule, "breaking": rules[rule] == "breaking", "element": s["id"] if s else seed, "seeds": [seed],
                    "file": (s or {}).get("file") or "tia-services.yaml", "line": (s or {}).get("line") or 1, "detail": detail,
                    "topic": (s or {}).get("topic")})
    hs = {s["id"]: s for s in head.event_schemas}
    for s in base.event_schemas:
        h = hs.get(s["id"])
        label = f"event {s['name']}" + (f" (topic {s['topic']})" if s.get("topic") else "")
        if h is None:
            add("event_schema_removed", s, f"{label}: schema removed", s["id"])
            continue
        for f, bf in s["fields"].items():
            hf = h["fields"].get(f)
            if hf is None:
                add("event_field_removed", s, f"{label}: field {f} removed", s["id"])
            elif hf["type"] != bf["type"]:
                add("event_field_type_changed", s, f"{label}: {f} {bf['type']} -> {hf['type']}", s["id"])
            else:
                ba, ha = set(bf.get("allowed") or []), set(hf.get("allowed") or [])
                if ha - ba and ba:
                    add("event_enum_value_added", s, f"{label}: {f} may now be {', '.join(sorted(ha - ba))}", s["id"])
                if ba - ha and ha:
                    add("event_enum_value_removed", s, f"{label}: {f} no longer {', '.join(sorted(ba - ha))}", s["id"])
        for f, hf in h["fields"].items():
            if f not in s["fields"]:
                add("event_required_field_added" if hf["required"] else "event_field_added", s,
                    f"{label}: field {f} added ({'required' if hf['required'] else 'optional'})", s["id"])
    produced_h = {e["topic"] for e in head.events if e["kind"] == "produces"}
    for t in sorted({e["topic"] for e in base.events if e["kind"] == "produces"} - produced_h):
        add("event_topic_no_longer_produced", None, f"topic {t} is no longer produced by {base.service}", f"topic:{t}")
    out.sort(key=lambda c: (c["element"], c["kind"], c["detail"]))
    return out
