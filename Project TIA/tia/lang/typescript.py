"""TypeScript/JavaScript adapter (PRD FR-040: Express, NestJS, fetch, axios).

Routes: Express (app/router/server.get|post|put|delete|patch("/path", handler)), NestJS (@Controller('orders') classes with @Get(':id'),
@Post(), ... methods; @Body() dto: CreateOrderDto is the request schema; @Param/@Query parameters; @UseGuards(...) is the authorisation
marker). DTOs: classes with class-validator decorators (@IsString, @IsNotEmpty, @IsOptional, @MaxLength, @MinLength, @Min, @Max,
@IsIn([...]), @IsEnum, @IsEmail, @Matches(/re/), @IsArray) and TypeScript interfaces (name?: optional). Outbound calls: fetch(url, {method}),
axios.get/post/put/delete/patch(url), axios({method, url}), got/superagent-style client.get(url); a base read from process.env.X (directly or
through a constant) resolves through the config key X. Tests: it()/test() blocks; supertest request(app).get('/path') calls are their HTTP
calls. Front-end pages in the same files are read by tia.extract_ui and merged by the caller.
"""
from __future__ import annotations

import re

import tree_sitter_javascript
import tree_sitter_typescript

from ..models import normalize_path
from .common import add_callsite, add_endpoint, add_function, add_schema, add_test, new_extract, parser, string_value, text_of, walk

_TS = parser(tree_sitter_typescript.language_typescript())
_TSX = parser(tree_sitter_typescript.language_tsx())
_JS = parser(tree_sitter_javascript.language())
VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
NEST = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH"}
ROUTERS = re.compile(r"^(app|router|server|api|r|routes|\w*[Rr]outer)$")


def _parser(path: str):
    p = path.lower()
    return _TSX if p.endswith(".tsx") else _TS if p.endswith(".ts") else _JS


def _dec(d) -> tuple:
    """A decorator -> (name, argument text)."""
    call = next((c for c in d.named_children if c.type == "call_expression"), None)
    if call is not None:
        return text_of(call.child_by_field_name("function")), text_of(call.child_by_field_name("arguments"))[1:-1]
    return text_of(d).lstrip("@"), ""


def _first_str(s: str):
    m = re.search(r"""['"`]([^'"`$]*)['"`]""", s or "")
    return m.group(1) if m else None


def _template(n, env: dict) -> tuple:
    """A URL expression -> (path, base_kind, base_value, reason)."""
    s = string_value(n)
    if s is not None:
        if s.startswith(("http://", "https://")):
            u = re.match(r"https?://([^/:]+)(:\d+)?", s)
            return (s[u.end():] or "/", "host", u.group(1), None)
        return (s, None, None, "base URL not present in call") if s.startswith("/") else (None, None, None, "path does not start with a literal segment")
    t = text_of(n)
    if n.type == "template_string":
        body = t[1:-1]
        base = None
        m = re.match(r"^\$\{\s*([^}]+?)\s*\}", body)
        if m:
            base = _base(m.group(1), env)
            if base is None:
                return (None, None, None, f"base expression not resolvable: {m.group(1)[:40]}")
            body = body[m.end():]
        elif body.startswith(("http://", "https://")):
            u = re.match(r"https?://([^/:$]+)(:\d+)?", body)
            base = ("host", u.group(1)) if u else None
            body = body[u.end():] if u else body
        path = re.sub(r"\$\{[^}]*\}", "{}", body).split("?", 1)[0]
        if not path.startswith("/"):
            return (None, None, None, "path does not start with a literal segment")
        return (path, base[0] if base else None, base[1] if base else None, None if base else "base URL not present in call")
    if n.type == "binary_expression":
        parts = re.split(r"\s*\+\s*", t)
        b = _base(parts[0], env)
        rest = "".join(re.sub(r"""^['"](.*)['"]$""", r"\1", p) if re.fullmatch(r"""['"].*['"]""", p) else "{}" for p in parts[1:])
        if b is not None and rest.startswith("/"):
            return (rest.split("?")[0], b[0], b[1], None)
    return (None, None, None, f"URL expression not statically resolvable: {t[:60]}")


def _base(expr: str, env: dict):
    expr = expr.strip()
    m = re.fullmatch(r"process\.env\.(\w+)|process\.env\[['\"](\w+)['\"]\]", expr)
    if m:
        return ("config_key", m.group(1) or m.group(2))
    return env.get(expr)


def _dto_field(decs: list, typ: str, optional: bool) -> dict:
    names = {n for n, _ in decs}
    c = {}
    for n, a in decs:
        num = re.fullmatch(r"\s*(-?[\d.]+)\s*", a or "")
        v = (int(float(num.group(1))) if float(num.group(1)).is_integer() else float(num.group(1))) if num else None
        if n in ("MaxLength",) and v is not None:
            c["max_length"] = v
        elif n == "MinLength" and v is not None:
            c["min_length"] = v
        elif n == "Min" and v is not None:
            c["minimum"] = v
        elif n == "Max" and v is not None:
            c["maximum"] = v
        elif n == "IsIn":
            c["allowed"] = re.findall(r"""['"]([^'"]+)['"]""", a)
        elif n == "IsEmail":
            c["format"] = "email"
        elif n == "Matches":
            m = re.match(r"\s*/(.+)/[a-z]*\s*$", a or "")
            if m:
                c["pattern"] = m.group(1)
        elif n == "Length":
            nums = re.findall(r"-?\d+", a or "")
            if nums:
                c["min_length"] = int(nums[0])
            if len(nums) > 1:
                c["max_length"] = int(nums[1])
    f = {"type": typ, "required": not optional and "IsOptional" not in names}
    if "IsArray" in names or typ.endswith("[]") or typ.startswith("Array<"):
        f["multi"] = True
    if c:
        f["constraints"] = c
    return f


def extract_file(repo: str, service: str, path: str, text: str, strip: tuple = ()):
    out = new_extract(repo, service)
    root = _parser(path).parse(text.encode("utf-8")).root_node
    out.files_analysed = 1
    is_test = re.search(r"\.(test|spec)\.[jt]sx?$", path) is not None or "/__tests__/" in "/" + path
    env = {}
    for n in walk(root):
        if n.type == "variable_declarator" and n.child_by_field_name("value") is not None:
            b = _base(text_of(n.child_by_field_name("value")), {})
            if b:
                env[text_of(n.child_by_field_name("name"))] = b
            else:
                s = string_value(n.child_by_field_name("value"))
                if s and s.startswith(("http://", "https://")):
                    u = re.match(r"https?://([^/:]+)", s)
                    env[text_of(n.child_by_field_name("name"))] = ("host", u.group(1))

    def scan_calls(node, fid, http_out=None):
        calls = []
        for x in walk(node):
            if x.type != "call_expression":
                continue
            fn = x.child_by_field_name("function")
            fname = text_of(fn)
            last = fname.rsplit(".", 1)[-1]
            calls.append(last)
            argn = x.child_by_field_name("arguments")
            args = argn.named_children if argn is not None else []
            line = x.start_point[0] + 1
            if http_out is not None and last in VERBS and re.search(r"request\(", fname) and args and string_value(args[0]):
                http_out.append([VERBS[last], normalize_path(string_value(args[0]).split("?")[0], strip)])
                continue
            if http_out is not None:
                continue
            if fname == "fetch" and args:
                m = re.search(r"""method\s*:\s*['"](\w+)['"]""", text_of(args[1]) if len(args) > 1 else "")
                add_callsite(out, repo, service, path, line, fid, (m.group(1).upper() if m else "GET"), _template(args[0], env), strip)
            elif last in VERBS and args and re.match(r"^(axios|http|got|client|\w*[Cc]lient|this\.http|this\.\w*[Cc]lient|superagent)\.\w+$", fname):
                add_callsite(out, repo, service, path, line, fid, VERBS[last], _template(args[0], env), strip)
            elif fname == "axios" and args and args[0].type == "object":
                o = text_of(args[0])
                m = re.search(r"""method\s*:\s*['"](\w+)['"]""", o)
                u = next((p.child_by_field_name("value") for p in args[0].named_children if p.type == "pair" and text_of(p.child_by_field_name("key")) == "url"), None)
                if u is not None:
                    add_callsite(out, repo, service, path, line, fid, m.group(1).upper() if m else "GET", _template(u, env), strip)
        return calls

    # ---- classes: NestJS controllers, DTOs ---------------------------------------------------------------------------------------
    for cls in walk(root):
        if cls.type not in ("class_declaration", "class"):
            continue
        cname = text_of(cls.child_by_field_name("name"))
        holder = cls.parent if cls.parent is not None and cls.parent.type == "export_statement" else cls
        cdecs = [_dec(d) for d in holder.children if d.type == "decorator"] + [_dec(d) for d in cls.children if d.type == "decorator"]
        cmap = dict(cdecs)
        body = cls.child_by_field_name("body")
        members = body.named_children if body is not None else []
        fields, pending = {}, []
        for m in members:
            if m.type == "decorator":
                pending.append(_dec(m))
                continue
            if m.type in ("public_field_definition", "field_definition"):
                decs = pending + [_dec(d) for d in m.children if d.type == "decorator"]
                nm = text_of(m.child_by_field_name("name") or m.child_by_field_name("property"))
                typ = text_of(m.child_by_field_name("type")).lstrip(":").strip()
                if nm:
                    fields[nm] = _dto_field(decs, typ, "?" in text_of(m).split(":")[0])
            elif m.type == "method_definition":
                mname = text_of(m.child_by_field_name("name"))
                qual = f"{cname}.{mname}"
                fid = f"{repo}:{path}::{qual}"
                mdecs = pending
                calls = scan_calls(m, fid)
                params = m.child_by_field_name("parameters")
                plist = []
                for p in (params.named_children if params is not None else []):
                    pd = [_dec(d) for d in p.children if d.type == "decorator"]
                    plist.append((text_of(p.child_by_field_name("pattern")), text_of(p.child_by_field_name("type")).lstrip(":").strip(), dict(pd)))
                sig = {"params": [{"name": n, "kind": "positional", "type": t} for n, t, _ in plist], "returns": text_of(m.child_by_field_name("return_type")).lstrip(":").strip() or None}
                add_function(out, repo, path, qual, m, calls, sig, m.start_point[0] + 1)
                verbs = [(NEST[n], a) for n, a in mdecs if n in NEST]
                if verbs and "Controller" in cmap:
                    prefix = _first_str(cmap.get("Controller", "")) or ""
                    rt = sig["returns"] or ""
                    inner = re.sub(r"^Promise<(.+)>$", r"\1", rt)
                    shape = "list" if inner.endswith("[]") or inner.startswith("Array<") else ("object" if inner and inner not in ("void", "any") else None)
                    resp = re.sub(r"\[\]$|^Array<|>$", "", inner) if shape else None
                    reqs = [t for n, t, d in plist if "Body" in d]
                    prm = [{"name": n, "in": "path" if "Param" in d else "query", "type": t, "required": "Query" not in d}
                           for n, t, d in plist if "Param" in d or "Query" in d]
                    auth = [f"@UseGuards({a})" for n, a in cdecs + mdecs if n == "UseGuards"]
                    for verb, a in verbs:
                        sub = _first_str(a) or ""
                        full = "/" + "/".join(x for x in (prefix.strip("/"), sub.strip("/")) if x)
                        add_endpoint(out, service, repo, path, verb, full, qual, fid, m.start_point[0] + 1, strip, request_schemas=reqs,
                                     response_schema=resp if resp and re.fullmatch(r"[A-Z]\w*", resp) else None, params=prm, auth=auth,
                                     response_shape=shape)
                pending = []
            else:
                pending = []
        if fields and "Controller" not in cmap:
            add_schema(out, service, repo, path, cname, fields, cls.start_point[0] + 1)
    for itf in walk(root):
        if itf.type == "interface_declaration":
            fields = {}
            for prop in walk(itf):
                if prop.type == "property_signature":
                    nm = text_of(prop.child_by_field_name("name"))
                    typ = text_of(prop.child_by_field_name("type")).lstrip(":").strip()
                    fields[nm] = _dto_field([], typ, "?" in text_of(prop).split(":")[0])
            add_schema(out, service, repo, path, text_of(itf.child_by_field_name("name")), fields, itf.start_point[0] + 1)
    # ---- functions, Express routes, tests ------------------------------------------------------------------------------------------
    for n in walk(root):
        if n.type == "function_declaration" or (n.type == "variable_declarator" and n.child_by_field_name("value") is not None
                                                and n.child_by_field_name("value").type in ("arrow_function", "function_expression", "function")):
            name = text_of(n.child_by_field_name("name"))
            qual, fid = name, f"{repo}:{path}::{name}"
            node = n if n.type == "function_declaration" else n.child_by_field_name("value")
            if not is_test:
                params = node.child_by_field_name("parameters")
                sig = {"params": [{"name": text_of(p), "kind": "positional", "type": ""} for p in (params.named_children if params is not None else [])],
                       "returns": None}
                add_function(out, repo, path, qual, node, scan_calls(node, fid), sig, n.start_point[0] + 1)
        elif n.type == "call_expression":
            fn = n.child_by_field_name("function")
            fname = text_of(fn)
            argn = n.child_by_field_name("arguments")
            args = argn.named_children if argn is not None else []
            if fn is not None and fn.type == "member_expression" and fname.rsplit(".", 1)[-1] in VERBS and args and string_value(args[0]) \
                    and ROUTERS.match(fname.rsplit(".", 1)[0]) and not is_test and len(args) >= 2:
                verb = VERBS[fname.rsplit(".", 1)[-1]]
                h = args[-1]
                hname = text_of(h) if h.type == "identifier" else f"{verb.lower()} {string_value(args[0])}"
                hid = f"{repo}:{path}::{hname}"
                if h.type != "identifier":
                    add_function(out, repo, path, hname, h, scan_calls(h, hid), {"params": [], "returns": None}, h.start_point[0] + 1)
                add_endpoint(out, service, repo, path, verb, string_value(args[0]), hname, hid, n.start_point[0] + 1, strip,
                             auth=[text_of(a) for a in args[1:-1] if re.search(r"auth|guard|jwt|passport|token|role", text_of(a), re.I)])
            elif is_test and fname in ("it", "test") and args and string_value(args[0]) is not None:
                http = []
                calls = scan_calls(args[-1], None, http)
                tname = string_value(args[0])
                add_test(out, repo, path, f"{tname}@{n.start_point[0] + 1}", tname, n.start_point[0] + 1, [], calls, http)
    return out
