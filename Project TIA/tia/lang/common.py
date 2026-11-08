"""Shared helpers for the tree-sitter based language adapters."""
from __future__ import annotations

import hashlib
import re
from typing import Iterator, Optional
from urllib.parse import urlsplit

from tree_sitter import Language, Node, Parser

from .. import EXTRACTOR_VERSION
from ..models import CallSite, Endpoint, Function, RepoExtract, Schema, TestCase, normalize_path

COMMENTS = ("comment", "line_comment", "block_comment", "multiline_comment")


def parser(lang) -> Parser:
    return Parser(Language(lang))


def text_of(n: Optional[Node]) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def walk(n: Node) -> Iterator[Node]:
    stack = [n]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def token_hash(n: Node) -> str:
    """Hash of the node's tokens without comments or whitespace: formatting and comments never count as a change."""
    toks = [text_of(x) for x in walk(n) if x.child_count == 0 and x.type not in COMMENTS]
    return hashlib.sha1(" ".join(toks).encode()).hexdigest()[:16]


def string_value(n: Optional[Node]) -> Optional[str]:
    """The value of a plain string literal (no interpolation)."""
    if n is None:
        return None
    t = text_of(n)
    if n.type in ("interpreted_string_literal", "raw_string_literal", "string", "string_literal", "verbatim_string_literal", "line_string_literal"):
        if any(c.type in ("interpolation", "template_substitution", "interpolated_expression", "string_content_interpolation") for c in n.children):
            return None
        m = re.fullmatch(r'@?"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`([^`]*)`', t, re.S)
        if m:
            return next(g for g in m.groups() if g is not None)
    return None


def new_extract(repo: str, service: str) -> RepoExtract:
    return RepoExtract(repo=repo, service=service, commit="", extractor_version=EXTRACTOR_VERSION)


def url_parts(raw: str) -> tuple:
    """A literal URL or path -> (path, base_kind, base_value, reason)."""
    if raw.startswith(("http://", "https://")):
        u = urlsplit(raw)
        return (u.path or "/", "host", u.hostname, None)
    if raw.startswith("/"):
        return (raw, None, None, "base URL not present in call")
    return (None, None, None, "path does not start with a literal segment")


def template_path(parts: list) -> tuple:
    """[("s", text) | ("v", base_ref or None)] -> (path, base_kind, base_value, reason): a leading variable is the base (its kind/value given
    by the caller, or None when unknown); later variables become path parameters."""
    base_kind = base_val = None
    path, i = "", 0
    if parts and parts[0][0] == "v":
        ref = parts[0][1]
        if ref is None:
            return (None, None, None, "base expression not resolvable")
        base_kind, base_val = ref
        i = 1
    elif parts and parts[0][0] == "s" and parts[0][1].startswith(("http://", "https://")):
        u = urlsplit(parts[0][1])
        base_kind, base_val = "host", u.hostname
        path = parts[0][1][len(f"{u.scheme}://{u.netloc}"):]
        i = 1
    for kind, v in parts[i:]:
        path += v if kind == "s" else "{}"
    path = path.split("?", 1)[0]
    if not path.startswith("/"):
        return (None, base_kind, base_val, "path does not start with a literal segment")
    return (path, base_kind, base_val, None)


def add_endpoint(out: RepoExtract, service: str, repo: str, path: str, method: str, full: str, handler: str, handler_id: str,
                 line: int, strip: tuple, request_schemas=None, response_schema=None, params=None, auth=None, response_shape=None,
                 success_status=None):
    np_ = normalize_path(full, strip)
    out.endpoints.append(Endpoint(id=f"{service}:{method} {np_}", service=service, repo=repo, method=method, path=full, norm_path=np_,
                                  handler=handler, handler_id=handler_id, file=path, line=line, response_schema=response_schema,
                                  request_schemas=list(request_schemas or []), params=list(params or []), auth=list(auth or []),
                                  response_shape=response_shape, success_status=success_status))


def add_callsite(out: RepoExtract, repo: str, service: str, path: str, line: int, fid: Optional[str], method: Optional[str], parts: tuple, strip: tuple):
    p, bk, bv, reason = parts
    out.callsites.append(CallSite(id=f"{repo}:{path}:{line}", repo=repo, service=service, file=path, line=line, function_id=fid, method=method,
                                  norm_path=normalize_path(p, strip) if p else None, base_kind=bk, base_value=bv, reason=reason))


def add_function(out: RepoExtract, repo: str, path: str, qual: str, node: Node, calls: list, signature: dict, line: int):
    out.functions.append(Function(id=f"{repo}:{path}::{qual}", repo=repo, file=path, qualname=qual, line=line, hash=token_hash(node),
                                  calls=sorted(set(calls)), signature=signature, body_hash=token_hash(node), end_line=node.end_point[0] + 1))


def add_test(out: RepoExtract, repo: str, path: str, qual: str, name: str, line: int, imports: list, called: list, http: list, statuses=None, literals=None):
    out.tests.append(TestCase(id=f"{repo}:{path}::{qual}", repo=repo, file=path, name=name, line=line, imports=sorted(set(imports)),
                              called=sorted(set(called)), http_calls=http, statuses=sorted(set(statuses or [])), literals=list(literals or [])[:60]))


def add_schema(out: RepoExtract, service: str, repo: str, path: str, name: str, fields: dict, line: int):
    if fields:
        out.schemas.append(Schema(id=f"{service}:schema:{name}", service=service, repo=repo, name=name, fields=fields, file=path, line=line))


def is_test_path(path: str) -> bool:
    p = path.lower()
    return any(s in "/" + p for s in ("/test/", "/tests/", "/__tests__/", "/spec/")) or re.search(r"(_test\.go|\.test\.[jt]sx?|\.spec\.[jt]sx?|tests?\.cs|test\.kt)$", p) is not None


VALIDATE_RULES = {"max": "max_length", "min": "min_length", "len": None, "gt": "exclusive_minimum", "gte": "minimum", "lt": "exclusive_maximum",
                  "lte": "maximum"}
