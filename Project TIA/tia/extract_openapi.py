"""OpenAPI / Swagger adapter (PRD FR-017 "external catalogs enrich and corroborate", FR-018 full comparison).

A specification file in a repository (openapi*.yaml|yml|json, swagger*.yaml|yml|json) declares endpoints, request and response schemas,
parameter rules and security. They are extracted like code-declared ones, so the same contract comparison applies to them. Where the code
of the same repository declares the same endpoint, the code wins (the specification can be out of date); a specification never hides what
the code says. Parsed with yaml.safe_load / json only; nothing is resolved from the network ($ref to another file is reported, not fetched).
"""
from __future__ import annotations

import json
import re

import yaml

from . import EXTRACTOR_VERSION
from .models import Endpoint, RepoExtract, Schema, normalize_path

SPEC_NAME = re.compile(r"(?:^|/)(openapi|swagger)[^/]*\.(ya?ml|json)$", re.I)
METHODS = ("get", "post", "put", "patch", "delete")
MAX_SPEC = 10 * 1024 * 1024


def is_openapi_file(path: str) -> bool:
    return bool(SPEC_NAME.search(path)) and "/node_modules/" not in "/" + path


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def json_field(schema: dict, required: bool) -> dict:
    """A JSON-schema property -> TIA field: type, required, multi, constraints (allowed, lengths, ranges, pattern, format)."""
    schema = schema or {}
    multi = schema.get("type") == "array"
    inner = schema.get("items") or {} if multi else schema
    t = _ref_name(inner["$ref"]) if "$ref" in inner else str(inner.get("type") or "object")
    c = {}
    for k, ck in (("minLength", "min_length"), ("maxLength", "max_length"), ("minimum", "minimum"), ("maximum", "maximum"),
                  ("pattern", "pattern"), ("minItems", "min_items"), ("maxItems", "max_items"), ("multipleOf", "multiple_of")):
        src = schema if k in ("minItems", "maxItems") else inner
        if k in src and isinstance(src[k], (int, float, str)):
            c[ck] = src[k]
    for k, ck in (("exclusiveMinimum", "exclusive_minimum"), ("exclusiveMaximum", "exclusive_maximum")):
        v = inner.get(k)
        if isinstance(v, bool) and v and ("minimum" if ck == "exclusive_minimum" else "maximum") in c:   # OpenAPI 3.0 boolean form
            c[ck] = c.pop("minimum" if ck == "exclusive_minimum" else "maximum")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            c[ck] = v
    if isinstance(inner.get("enum"), list):
        c["allowed"] = [x for x in inner["enum"] if isinstance(x, (str, int, float, bool))]
    if inner.get("format") == "email":
        c["format"] = "email"
    out = {"type": f"List[{t}]" if multi else t, "required": bool(required)}
    if multi:
        out["multi"] = True
    if inner.get("nullable"):
        out["nullable"] = True
    if c:
        out["constraints"] = c
    return out


def _schema(name: str, s: dict, repo: str, service: str, path: str, line: int) -> Schema:
    req = set(s.get("required") or [])
    props = s.get("properties") or {}
    return Schema(id=f"{service}:schema:{name}", service=service, repo=repo, name=name,
                  fields={k: json_field(v if isinstance(v, dict) else {}, k in req) for k, v in props.items()}, file=path, line=line)


def _line_of(text: str, needle: str) -> int:
    i = text.find(needle)
    return text.count("\n", 0, i) + 1 if i >= 0 else 1


def extract_openapi(repo: str, service: str, path: str, text: str, strip: tuple = ()) -> RepoExtract:
    out = RepoExtract(repo=repo, service=service, commit="", extractor_version=EXTRACTOR_VERSION)
    if len(text) > MAX_SPEC:
        out.files_failed.append({"file": path, "error": "specification larger than 10 MB"})
        return out
    try:
        spec = json.loads(text) if path.lower().endswith(".json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        out.files_failed.append({"file": path, "error": f"specification does not parse: {str(e)[:80]}"})
        return out
    if not isinstance(spec, dict) or not ("openapi" in spec or "swagger" in spec) or not isinstance(spec.get("paths"), dict):
        out.non_python_files = 1                                       # named like a specification but is not one
        return out
    out.files_analysed = 1
    comps = (spec.get("components") or {}).get("schemas") or spec.get("definitions") or {}
    for name, s in comps.items():
        if isinstance(s, dict):
            if isinstance(s.get("enum"), list) and s.get("type") in ("string", "integer"):
                out.enums[name] = [x for x in s["enum"] if isinstance(x, (str, int))]
            out.schemas.append(_schema(name, s, repo, service, path, _line_of(text, f"{name}:")))
    base = "" if "openapi" in spec else (spec.get("basePath") or "").rstrip("/")
    root_sec = spec.get("security")
    for p, item in spec["paths"].items():
        if not isinstance(item, dict) or not str(p).startswith("/"):
            continue
        shared = item.get("parameters") or []
        for m in METHODS:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            full = base + p
            np_ = normalize_path(full, strip)
            oid = op.get("operationId") or f"{m.upper()} {full}"
            params, req_schema = [], None
            for prm in shared + (op.get("parameters") or []):
                if not isinstance(prm, dict) or "$ref" in prm:
                    continue
                if prm.get("in") == "body":                              # Swagger 2 body parameter
                    sch = prm.get("schema") or {}
                    req_schema = _ref_name(sch["$ref"]) if "$ref" in sch else None
                    continue
                if prm.get("in") not in ("path", "query", "header", "cookie"):
                    continue
                sch = prm.get("schema") or {k: v for k, v in prm.items() if k in ("type", "enum", "minimum", "maximum", "pattern", "maxLength", "minLength", "items")}
                params.append({"name": prm.get("name"), "in": prm["in"], **json_field(sch, prm.get("required") or prm["in"] == "path")})
            body = ((op.get("requestBody") or {}).get("content") or {})
            js = next((v.get("schema") for k, v in body.items() if "json" in k and isinstance(v, dict)), None)
            if isinstance(js, dict):
                if "$ref" in js:
                    req_schema = _ref_name(js["$ref"])
                elif js.get("properties"):
                    req_schema = f"{oid}Request".replace(" ", "_")
                    out.schemas.append(_schema(req_schema, js, repo, service, path, _line_of(text, p)))
            status, resp, shape = None, None, None
            for code, r in sorted((op.get("responses") or {}).items(), key=lambda kv: str(kv[0])):
                if str(code).startswith("2"):
                    status = int(code) if str(code).isdigit() else None
                    cont = (r or {}).get("content") or {}
                    sch = next((v.get("schema") for k, v in cont.items() if "json" in k and isinstance(v, dict)), None) or (r or {}).get("schema")
                    if isinstance(sch, dict):
                        shape = "list" if sch.get("type") == "array" else "object"
                        inner = sch.get("items") if shape == "list" else sch
                        if isinstance(inner, dict) and "$ref" in inner:
                            resp = _ref_name(inner["$ref"])
                    break
            sec = op.get("security", root_sec)
            auth = sorted({f"security:{k}" + (f"[{','.join(v)}]" if v else "") for req in (sec or []) if isinstance(req, dict) for k, v in req.items()})
            out.endpoints.append(Endpoint(id=f"{service}:{m.upper()} {np_}", service=service, repo=repo, method=m.upper(), path=full, norm_path=np_,
                                          handler=f"openapi:{oid}", handler_id=f"{repo}:{path}::openapi:{m.upper()} {full}", file=path,
                                          line=_line_of(text, f"{p}:"), response_schema=resp, request_schemas=[req_schema] if req_schema else [],
                                          params=params, response_shape=shape, success_status=status, auth=auth, source="openapi"))
    return out
