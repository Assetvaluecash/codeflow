"""Go adapter (PRD FR-040: net/http, chi, gin; also echo and gorilla/mux).

Routes: http.HandleFunc / mux.HandleFunc / mux.Handle ("GET /orders/{id}" method patterns of Go 1.22 included), chi r.Get/Post/...,
gin router.GET/POST/..., echo e.GET/..., gorilla r.HandleFunc(path, h).Methods("GET"). Outbound calls: http.Get/Post/Head,
http.NewRequest(method, url, ...), client.Get/Post; a URL built from a variable that holds os.Getenv("X") resolves through the config key X.
Request bodies: json.NewDecoder(r.Body).Decode(&v) and gin's c.ShouldBindJSON/BindJSON(&v) name v's struct type. Schemas: structs with
json tags (field names from the tag); validate:"..." (go-playground) and binding:"..." (gin) give required and constraints.
Tests: Test* functions in *_test.go; httptest.NewRequest(method, path, ...) gives the HTTP calls they make.
"""
from __future__ import annotations

import re

import tree_sitter_go

from ..models import normalize_path
from .common import (add_callsite, add_endpoint, add_function, add_schema, add_test, new_extract, parser, string_value, template_path,
                     text_of, walk)

_P = parser(tree_sitter_go.language())
VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
CHI = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "Head": "HEAD"}
CLIENT_VERBS = {"Get": "GET", "Post": "POST", "Head": "HEAD", "PostForm": "POST"}


def _tag(tag: str, key: str):
    m = re.search(key + r':"([^"]*)"', tag or "")
    return m.group(1) if m else None


def _field_rules(tag: str) -> tuple:
    required, c = False, {}
    for src in (_tag(tag, "validate"), _tag(tag, "binding")):
        for rule in (src or "").split(","):
            k, _, v = rule.partition("=")
            if k == "required":
                required = True
            elif k == "email":
                c["format"] = "email"
            elif k == "oneof" and v:
                c["allowed"] = v.split()
            elif k in ("max", "min", "gt", "gte", "lt", "lte") and v:
                try:
                    num = float(v)
                    num = int(num) if num.is_integer() else num
                except ValueError:
                    continue
                c[{"max": "max_length", "min": "min_length", "gt": "exclusive_minimum", "gte": "minimum", "lt": "exclusive_maximum",
                   "lte": "maximum"}[k]] = num
    return required, c


def extract_file(repo: str, service: str, path: str, text: str, strip: tuple = ()):
    out = new_extract(repo, service)
    tree = _P.parse(text.encode("utf-8"))
    root = tree.root_node
    if root.has_error and not any(c.type in ("function_declaration", "method_declaration") for c in root.children):
        out.files_failed.append({"file": path, "error": "Go source does not parse"})
        return out
    out.files_analysed = 1
    is_test = path.endswith("_test.go")
    env_vars: dict = {}                                                    # variable -> config key (x := os.Getenv("X"))
    for n in walk(root):
        if n.type in ("var_spec", "const_spec", "short_var_declaration", "assignment_statement"):
            names = [c for c in (n.child_by_field_name("name"), n.child_by_field_name("left")) if c is not None]
            val = n.child_by_field_name("value") or n.child_by_field_name("right")
            if names and val is not None:
                vt = text_of(val)
                m = re.search(r'os\.(?:Getenv|LookupEnv)\("([^"]+)"\)', vt)
                lit = re.fullmatch(r'"(https?://[^"]*)"', vt.strip())
                for nm in re.findall(r"[A-Za-z_]\w*", text_of(names[0]))[:1]:
                    if m:
                        env_vars[nm] = ("config_key", m.group(1))
                    elif lit:
                        env_vars[nm] = ("literal", lit.group(1))
    # ---- schemas: structs with json tags ----------------------------------------------------------------------------------------
    for n in walk(root):
        if n.type == "type_spec" and n.child_by_field_name("type") is not None and n.child_by_field_name("type").type == "struct_type":
            fields = {}
            for fd in walk(n.child_by_field_name("type")):
                if fd.type != "field_declaration":
                    continue
                tag = text_of(fd.child_by_field_name("tag"))
                jname = (_tag(tag, "json") or "").split(",")[0]
                nm = text_of(fd.child_by_field_name("name"))
                if jname == "-" or not nm:
                    continue
                req, c = _field_rules(tag)
                typ = text_of(fd.child_by_field_name("type"))
                f = {"type": typ, "required": req}
                if typ.startswith("[]"):
                    f["multi"] = True
                if typ.startswith("*"):
                    f["nullable"] = True
                if c:
                    f["constraints"] = c
                fields[jname or nm] = f
            add_schema(out, service, repo, path, text_of(n.child_by_field_name("name")), fields, n.start_point[0] + 1)
    # ---- functions, routes, calls, tests ------------------------------------------------------------------------------------------
    funcs = {}
    for n in root.children:
        if n.type not in ("function_declaration", "method_declaration"):
            continue
        name = text_of(n.child_by_field_name("name"))
        recv = n.child_by_field_name("receiver")
        rtype = re.sub(r"[^A-Za-z0-9_]", "", text_of(recv).split()[-1]) if recv is not None and text_of(recv).split() else ""
        qual = f"{rtype}.{name}" if rtype else name
        fid = f"{repo}:{path}::{qual}"
        funcs[name] = fid
        body = n.child_by_field_name("body")
        calls, http = [], []
        params = n.child_by_field_name("parameters")
        sig = {"params": [{"name": text_of(p.child_by_field_name("name")), "kind": "positional", "type": text_of(p.child_by_field_name("type"))}
                          for p in (params.named_children if params is not None else []) if p.type == "parameter_declaration"],
               "returns": text_of(n.child_by_field_name("result")) or None}
        local_types = {}
        for x in walk(body) if body is not None else []:
            if x.type == "var_spec" and x.child_by_field_name("type") is not None:
                local_types[text_of(x.child_by_field_name("name"))] = text_of(x.child_by_field_name("type")).lstrip("*")
            elif x.type == "short_var_declaration":
                m = re.match(r"\s*(\w+)\s*:=\s*&?(\[\])?(\w+)\{", text_of(x))
                if m:
                    local_types[m.group(1)] = (m.group(2) or "") + m.group(3)
            if x.type != "call_expression":
                continue
            fn = x.child_by_field_name("function")
            args = [a for a in (x.child_by_field_name("arguments").named_children if x.child_by_field_name("arguments") is not None else [])]
            fname = text_of(fn)
            last = fname.rsplit(".", 1)[-1]
            calls.append(last)
            line = x.start_point[0] + 1
            # ---- routes
            if last in ("HandleFunc", "Handle") and args and string_value(args[0]) is not None:
                pat = string_value(args[0])
                verb, _, route = pat.partition(" ") if re.match(r"^[A-Z]+ /", pat) else ("", "", pat)
                methods = [verb] if verb in VERBS else []
                parent = x.parent
                mchain = text_of(parent.parent) if parent is not None and parent.parent is not None else ""
                mm = re.search(r'\.Methods\(([^)]*)\)', mchain)
                if mm:
                    methods = re.findall(r'"([A-Z]+)"', mm.group(1)) or methods
                h = text_of(args[1]) if len(args) > 1 else ""
                for m_ in methods or ["GET"]:
                    add_endpoint(out, service, repo, path, m_, route, h, funcs.get(h, f"{repo}:{path}::{h}"), line, strip)
            elif (last in CHI or last.upper() in VERBS and last.isupper()) and len(args) >= 2 and string_value(args[0]) is not None \
                    and fname.split(".")[0] not in ("http", "client", "c", "ctx") and not fname.startswith("http."):
                verb = CHI.get(last, last.upper())
                h = text_of(args[-1])
                add_endpoint(out, service, repo, path, verb, string_value(args[0]), h, funcs.get(h, f"{repo}:{path}::{h}"), line, strip)
            # ---- request bodies
            elif last in ("Decode", "ShouldBindJSON", "BindJSON", "ShouldBind", "Bind") and args:
                v = text_of(args[0]).lstrip("&")
                if v in local_types:
                    funcs.setdefault("__req__" + fid, local_types[v])
            # ---- response bodies: json.NewEncoder(w).Encode(v), gin/echo c.JSON(status, v)
            if (last == "Encode" and "NewEncoder" in fname and args) or (last in ("JSON", "IndentedJSON") and len(args) >= 2):
                v = text_of(args[-1]).lstrip("&")
                lit = re.match(r"^(\[\])?([A-Z]\w*)\{", v)
                t = local_types.get(v) or ((lit.group(1) or "") + lit.group(2) if lit else None)
                if t and re.fullmatch(r"(\[\])?[A-Z]\w*", t):
                    funcs.setdefault("__resp__" + fid, t)
            # ---- outbound calls
            if fname.startswith("http.") and last in CLIENT_VERBS and args or (re.search(r"[Cc]lient\.(Get|Post|Head)$", fname) and args):
                add_callsite(out, repo, service, path, line, fid, CLIENT_VERBS.get(last, "GET"), _url(args[0], env_vars), strip)
            elif last in ("NewRequest", "NewRequestWithContext") and fname.startswith("http.") and len(args) >= 2:
                a = args[1:] if last == "NewRequest" else args[2:]
                method = string_value(args[0] if last == "NewRequest" else args[1]) or ""
                method = method.upper() if method else re.sub(r"^http\.Method", "", text_of(args[0] if last == "NewRequest" else args[1])).upper() or None
                add_callsite(out, repo, service, path, line, fid, method, _url(a[0], env_vars), strip)
            elif fname == "httptest.NewRequest" and len(args) >= 2:
                m_ = string_value(args[0]) or re.sub(r"^http\.Method", "", text_of(args[0])).upper()
                p_ = string_value(args[1])
                if p_ and p_.startswith("/"):
                    http.append([m_.upper(), normalize_path(p_.split("?")[0], strip)])
        if is_test and name.startswith("Test"):
            add_test(out, repo, path, qual, name, n.start_point[0] + 1, [], calls, http)
        else:
            add_function(out, repo, path, qual, n, calls, sig, n.start_point[0] + 1)
    # request schemas found in handlers
    for e in out.endpoints:
        t = funcs.get("__req__" + e.handler_id)
        if t:
            e.request_schemas = [t.lstrip("[]")]
        r = funcs.get("__resp__" + e.handler_id)
        if r:
            e.response_schema, e.response_shape = r.lstrip("[]"), ("list" if r.startswith("[]") else "object")
    return out


def _url(n, env_vars: dict) -> tuple:
    """A Go URL expression -> (path, base_kind, base_value, reason)."""
    s = string_value(n)
    if s is not None:
        from .common import url_parts
        return url_parts(s)
    t = text_of(n)
    m = re.fullmatch(r'fmt\.Sprintf\(\s*"([^"]*)"\s*(?:,(.*))?\)', t, re.S)
    if m:
        fmt_, args = m.group(1), [a.strip() for a in (m.group(2) or "").split(",") if a.strip()]
        pieces, parts, ai = re.split(r"(%[sdvq])", fmt_), [], 0
        for p in pieces:
            if re.fullmatch(r"%[sdvq]", p):
                a = args[ai] if ai < len(args) else ""
                ai += 1
                parts.append(("v", _base(a, env_vars)) if not parts else ("v", None))
            elif p:
                parts.append(("s", p))
        return template_path(parts)
    if n.type == "binary_expression":
        flat = []

        def rec(x):
            if x.type == "binary_expression" and text_of(x.child_by_field_name("operator")) == "+":
                rec(x.child_by_field_name("left"))
                rec(x.child_by_field_name("right"))
            elif string_value(x) is not None:
                flat.append(("s", string_value(x)))
            else:
                flat.append(("v", _base(text_of(x), env_vars) if not flat else None))
        rec(n)
        return template_path(flat)
    return (None, None, None, f"URL expression not statically resolvable: {t[:60]}")


def _base(expr: str, env_vars: dict):
    expr = expr.strip()
    m = re.fullmatch(r'os\.Getenv\("([^"]+)"\)', expr)
    if m:
        return ("config_key", m.group(1))
    if expr in env_vars:
        kind, v = env_vars[expr]
        if kind == "literal":
            from urllib.parse import urlsplit
            return ("host", urlsplit(v).hostname)
        return (kind, v)
    return None
