"""C#/.NET adapter (PRD FR-040: ASP.NET Core, HttpClient).

Routes: controllers ([ApiController]/[Route("api/[controller]")] + [HttpGet("{id}")], [Route] on actions; [controller] is the class name
without "Controller") and minimal APIs (app.MapGet/MapPost/MapPut/MapDelete/MapPatch("/path", handler)). Request bodies: [FromBody]
parameters or the single complex parameter of a POST/PUT/PATCH action; response: ActionResult<T>/Task<T>/IEnumerable<T>.
Authorisation: [Authorize(...)], [AllowAnonymous], .RequireAuthorization(). Outbound calls: HttpClient GetAsync/PostAsync/PutAsync/
DeleteAsync/PatchAsync/SendAsync and the GetFromJsonAsync/PostAsJsonAsync helpers; the base comes from an absolute URL, or from a named
client (httpClientFactory.CreateClient("orders") -> config key "orders"), else the call is path-only. DTOs: classes and records with
DataAnnotations ([Required], [StringLength], [MaxLength], [MinLength], [Range], [RegularExpression], [EmailAddress]).
Tests: xUnit [Fact]/[Theory], NUnit [Test]/[TestCase], MSTest [TestMethod]; their client.GetAsync("/api/...") calls are HTTP calls.
Entity Framework: classes named by a DbContext's DbSet<T> properties are entities (table: [Table], ToTable(), else the DbSet name;
columns: scalar properties, [Column], [Key], [NotMapped]); db.Set.Where/Find/... read and Add/Update/Remove write.
"""
from __future__ import annotations

import re

import tree_sitter_c_sharp

from ..models import Entity, normalize_path
from .common import (add_callsite, add_endpoint, add_function, add_schema, add_test, new_extract, parser, string_value, template_path,
                     text_of, url_parts, walk)

_P = parser(tree_sitter_c_sharp.language())
VERB_ATTR = {"HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD"}
MAP = {"MapGet": "GET", "MapPost": "POST", "MapPut": "PUT", "MapDelete": "DELETE", "MapPatch": "PATCH"}
CLIENT = {"GetAsync": "GET", "GetStringAsync": "GET", "GetStreamAsync": "GET", "GetByteArrayAsync": "GET", "GetFromJsonAsync": "GET",
          "PostAsync": "POST", "PostAsJsonAsync": "POST", "PutAsync": "PUT", "PutAsJsonAsync": "PUT", "DeleteAsync": "DELETE",
          "PatchAsync": "PATCH", "PatchAsJsonAsync": "PATCH"}
TEST_ATTRS = {"Fact", "Theory", "Test", "TestCase", "TestMethod"}
EF_CONTEXTS = {"DbContext", "IdentityDbContext", "ApiAuthorizationDbContext"}
EF_SCALAR = r"(string|int|long|short|byte|decimal|double|float|bool|char|Guid|DateTime|DateTimeOffset|DateOnly|TimeOnly|TimeSpan|byte\[\]|uint|ulong)\??"
EF_WRITES = {"Add", "AddAsync", "AddRange", "AddRangeAsync", "Remove", "RemoveRange", "Update", "UpdateRange", "Attach", "ExecuteDelete",
             "ExecuteDeleteAsync", "ExecuteUpdate", "ExecuteUpdateAsync"}
EF_READS = {"Where", "First", "FirstAsync", "FirstOrDefault", "FirstOrDefaultAsync", "Single", "SingleAsync", "SingleOrDefault",
            "SingleOrDefaultAsync", "Find", "FindAsync", "ToList", "ToListAsync", "ToArray", "ToArrayAsync", "Any", "AnyAsync", "Count",
            "CountAsync", "Select", "OrderBy", "OrderByDescending", "Include", "AsNoTracking", "Max", "Min", "Sum", "Average", "Join", "GroupBy"}


def _attrs(node) -> dict:
    """attribute name -> [argument nodes] for the attribute lists directly on a declaration."""
    out = {}
    for c in node.children:
        if c.type == "attribute_list":
            for a in c.named_children:
                if a.type == "attribute":
                    name = text_of(a.child_by_field_name("name")).split(".")[-1].removesuffix("Attribute")
                    args = next((x for x in a.children if x.type == "attribute_argument_list"), None)
                    out[name] = [x for x in (args.named_children if args is not None else []) if x.type == "attribute_argument"]
    return out


def _arg_value(arg):
    """(name or None, value node) of an attribute argument."""
    named = arg.child_by_field_name("name")
    vals = [c for c in arg.named_children if c is not named and c.type != "name_equals"]
    return (text_of(named) if named is not None else None, vals[-1] if vals else None)


def _wire(attrs: dict, name: str) -> str:
    """The JSON name a property has on the wire: [JsonPropertyName("x")] / [JsonProperty("x")], else System.Text.Json's camelCase default."""
    for a in ("JsonPropertyName", "JsonProperty"):
        if attrs.get(a):
            v = string_value(_arg_value(attrs[a][0])[1])
            if v:
                return v
    return name[:1].lower() + name[1:]


def _num(n):
    try:
        f = float(text_of(n).rstrip("mMdDfF"))
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return None


def _dto_field(attrs: dict, typ: str) -> dict:
    c, req = {}, "Required" in attrs or typ in ("int", "long", "decimal", "double", "bool", "Guid", "DateTime")
    for name, key in (("StringLength", "max_length"), ("MaxLength", "max_length"), ("MinLength", "min_length")):
        for a in attrs.get(name, []):
            nm, v = _arg_value(a)
            if nm == "MinimumLength":
                c["min_length"] = _num(v)
            elif nm is None and _num(v) is not None:
                c[key] = _num(v)
    if "Range" in attrs and len(attrs["Range"]) >= 2:
        lo, hi = (_num(_arg_value(a)[1]) for a in attrs["Range"][:2])
        if lo is not None:
            c["minimum"] = lo
        if hi is not None:
            c["maximum"] = hi
    if "RegularExpression" in attrs and attrs["RegularExpression"]:
        p = string_value(_arg_value(attrs["RegularExpression"][0])[1])
        if p:
            c["pattern"] = p
    if "EmailAddress" in attrs:
        c["format"] = "email"
    f = {"type": typ, "required": req}
    if re.match(r"(List|IList|IEnumerable|ICollection|IReadOnlyList)<|.*\[\]$", typ):
        f["multi"] = True
    if typ.endswith("?"):
        f["nullable"], f["required"] = True, "Required" in attrs
    if c:
        f["constraints"] = {k: v for k, v in c.items() if v is not None}
    return f


def _inner(t: str):
    """ActionResult<T>, Task<ActionResult<IEnumerable<T>>> -> (T, shape)."""
    shape = "object"
    while True:
        m = re.fullmatch(r"\s*(Task|ValueTask|ActionResult|IActionResult|Results|Ok)<(.+)>\s*", t)
        if not m:
            break
        t = m.group(2)
    m = re.fullmatch(r"\s*(IEnumerable|List|IList|ICollection|IReadOnlyList|IAsyncEnumerable)<(.+)>\s*", t)
    if m:
        t, shape = m.group(2), "list"
    if t.endswith("[]"):
        t, shape = t[:-2], "list"
    return (t.strip() if re.fullmatch(r"[A-Za-z_][\w.]*", t.strip()) else None), shape


def _interp(n, named_clients: dict) -> tuple:
    """A URL argument -> (path, base_kind, base_value, reason)."""
    s = string_value(n)
    if s is not None:
        return url_parts(s)
    if n.type == "interpolated_string_expression":
        parts = []
        for c in n.named_children:
            if c.type in ("string_content", "interpolated_string_text", "interpolated_verbatim_string_text"):
                parts.append(("s", text_of(c)))
            elif c.type == "interpolation":
                parts.append(("v", None))
        if parts and parts[0][0] == "s" and parts[0][1].startswith(("http://", "https://", "/")):
            return template_path(parts)
        if parts and parts[0][0] == "s":
            return template_path([("s", "/" + parts[0][1])] + parts[1:]) if not parts[0][1].startswith("/") else template_path(parts)
        return (None, None, None, "URL starts with an expression")
    return (None, None, None, f"URL expression not statically resolvable: {text_of(n)[:60]}")


def extract_file(repo: str, service: str, path: str, text: str, strip: tuple = ()):
    out = new_extract(repo, service)
    root = _P.parse(text.encode("utf-8")).root_node
    if root.has_error and not any(n.type in ("class_declaration", "record_declaration") for n in walk(root)):
        out.files_failed.append({"file": path, "error": "C# source does not parse"})
        return out
    out.files_analysed = 1
    named_clients = {}                                     # variable -> client name (CreateClient("orders"))
    for n in walk(root):
        if n.type == "variable_declarator" or n.type == "assignment_expression":
            m = re.search(r"(\w+)\s*=\s*\w+\.CreateClient\(\s*\"([^\"]+)\"\s*\)", text_of(n.parent if n.type == "variable_declarator" else n))
            if m:
                named_clients[m.group(1)] = m.group(2)
    for cls in walk(root):
        if cls.type not in ("class_declaration", "record_declaration"):
            continue
        cname = text_of(cls.child_by_field_name("name"))
        cattrs = _attrs(cls)
        body = cls.child_by_field_name("body")
        # ---- DTOs ---------------------------------------------------------------------------------------------------
        fields = {}
        if cls.type == "record_declaration":
            pl = next((c for c in cls.children if c.type == "parameter_list"), None)
            for p in (pl.named_children if pl is not None else []):
                if p.type == "parameter":
                    fields[_wire(_attrs(p), text_of(p.child_by_field_name("name")))] = _dto_field(_attrs(p), text_of(p.child_by_field_name("type")))
        for m in (body.named_children if body is not None else []):
            if m.type == "property_declaration" and "JsonIgnore" not in _attrs(m):
                fields[_wire(_attrs(m), text_of(m.child_by_field_name("name")))] = _dto_field(_attrs(m), text_of(m.child_by_field_name("type")))
        if fields and not cname.endswith(("Controller", "Tests", "Test")):
            add_schema(out, service, repo, path, cname, fields, cls.start_point[0] + 1)
        # ---- Entity Framework: candidate entity classes and DbContext sets (resolved across files in resolve_data) -----------------
        bases = [re.sub(r"<.*$", "", b.strip()) for b in re.split(r",", text_of(next((c for c in cls.children if c.type == "base_list"), None)).lstrip(":"))
                 if b.strip()]
        if any(b in EF_CONTEXTS for b in bases):
            to_table = dict(re.findall(r"Entity<(\w+)>\(\)\s*\.ToTable\(\s*\"([^\"]+)\"", text))
            for m in (body.named_children if body is not None else []):
                if m.type == "property_declaration":
                    t = re.fullmatch(r"(?:virtual\s+)?DbSet<(\w+)>\??", text_of(m.child_by_field_name("type")).strip())
                    if t:
                        out.repositories.append({"kind": "ef_dbset", "name": text_of(m.child_by_field_name("name")), "entity": t.group(1),
                                                 "context": cname, "table": to_table.get(t.group(1)), "file": path, "line": m.start_point[0] + 1})
        elif cls.type == "class_declaration" and not cname.endswith(("Controller", "Tests", "Test", "Service", "Repository", "Context")):
            cols = {}
            for m in (body.named_children if body is not None else []):
                if m.type != "property_declaration":
                    continue
                pa, pt, pn = _attrs(m), text_of(m.child_by_field_name("type")).strip(), text_of(m.child_by_field_name("name"))
                if "NotMapped" in pa or not re.fullmatch(EF_SCALAR, pt):
                    continue                                     # navigation properties and collections are relationships, not columns
                col = (string_value(_arg_value(pa["Column"][0])[1]) if pa.get("Column") else None) or pn
                cols[col.lower()] = {"field": pn, "type": pt, "nullable": pt.endswith("?") or (pt == "string" and "Required" not in pa),
                                     "pk": "Key" in pa or pn in ("Id", f"{cname}Id")}
            if cols:
                tname = string_value(_arg_value(cattrs["Table"][0])[1]) if cattrs.get("Table") else None
                out.entities.append(Entity(id=f"{service}:entity:{cname}", service=service, repo=repo, name=cname,
                                           table=(tname or "").lower(), table_source="declared" if tname else "unresolved", columns=cols,
                                           kind="ef", file=path, line=cls.start_point[0] + 1, bases=[b for b in bases if b[:1].isupper()]))
        # ---- controller routes -----------------------------------------------------------------------------------------
        prefix = ""
        if "Route" in cattrs and cattrs["Route"]:
            prefix = (string_value(_arg_value(cattrs["Route"][0])[1]) or "").replace("[controller]", cname.removesuffix("Controller").lower())
        class_auth = [f"[{a}]" for a in cattrs if a in ("Authorize", "AllowAnonymous")]
        is_test_class = any(t in text_of(cls) for t in ("[Fact", "[Theory", "[Test", "[TestMethod"))
        for m in (body.named_children if body is not None else []):
            if m.type not in ("method_declaration", "local_function_statement"):
                continue
            name = text_of(m.child_by_field_name("name"))
            qual = f"{cname}.{name}"
            fid = f"{repo}:{path}::{qual}"
            attrs = _attrs(m)
            mbody = m.child_by_field_name("body")
            calls, http = [], []
            for x in walk(mbody) if mbody is not None else []:
                if x.type != "invocation_expression":
                    continue
                fn = x.child_by_field_name("function")
                fname = text_of(fn)
                last = re.sub(r"<.*$", "", fname.rsplit(".", 1)[-1])
                calls.append(last)
                argl = x.child_by_field_name("arguments")
                args = [a.named_children[-1] for a in (argl.named_children if argl is not None else []) if a.named_children]
                if last in CLIENT and args:
                    owner = fname.rsplit(".", 1)[0] if "." in fname else ""
                    parts = _interp(args[0], named_clients)
                    if parts[0] and parts[1] is None and owner.lstrip("_") in {k.lstrip("_") for k in named_clients}:
                        key = next(v for k, v in named_clients.items() if k.lstrip("_") == owner.lstrip("_"))
                        parts = (parts[0], "config_key", key, None)
                    if is_test_class:
                        if parts[0]:
                            http.append([CLIENT[last], normalize_path(parts[0], strip)])
                    else:
                        add_callsite(out, repo, service, path, x.start_point[0] + 1, fid, CLIENT[last], parts, strip)
            params = m.child_by_field_name("parameters")
            plist = [p for p in (params.named_children if params is not None else []) if p.type == "parameter"]
            sig = {"params": [{"name": text_of(p.child_by_field_name("name")), "kind": "positional", "type": text_of(p.child_by_field_name("type"))}
                              for p in plist], "returns": text_of(m.child_by_field_name("returns")) or None}
            if is_test_class and set(attrs) & TEST_ATTRS:
                add_test(out, repo, path, qual, name, m.start_point[0] + 1, [], calls, http)
                continue
            add_function(out, repo, path, qual, m, calls, sig, m.start_point[0] + 1)
            # Entity Framework access through a DbSet (db.Orders.Where(...) / db.Orders.Add(...)) or db.Set<Order>()
            for x in re.finditer(r"\b\w+\.(\w+)\s*\.\s*(\w+)\s*[(<]|\bSet<(\w+)>\(\)\s*\.\s*(\w+)", text_of(mbody) if mbody is not None else ""):
                dbset, op_name, model = x.group(1), x.group(2) or x.group(4), x.group(3)
                op = "write" if op_name in EF_WRITES else "read" if op_name in EF_READS else None
                if op and (model or (dbset and dbset[:1].isupper())):
                    line = (mbody.start_point[0] + 1) + text_of(mbody).count("\n", 0, x.start())
                    out.data_refs.append({"function_id": fid, **({"model": model} if model else {"dbset": dbset}), "op": op,
                                          "file": path, "line": line, "via": f"{model or dbset}.{op_name}"})
            verbs = [(VERB_ATTR[a], attrs[a]) for a in attrs if a in VERB_ATTR]
            if not verbs:
                continue
            resp, shape = _inner(text_of(m.child_by_field_name("returns")))
            body_params = [text_of(p.child_by_field_name("type")) for p in plist if "FromBody" in _attrs(p)]
            auth = sorted(set(class_auth + [f"[{a}]" for a in attrs if a in ("Authorize", "AllowAnonymous")]))
            for verb, args in verbs:
                sub = string_value(_arg_value(args[0])[1]) if args else ""
                if not sub and "Route" in attrs and attrs["Route"]:
                    sub = string_value(_arg_value(attrs["Route"][0])[1]) or ""
                full = sub if (sub or "").startswith("/") else "/" + "/".join(x for x in (prefix.strip("/"), (sub or "").strip("/")) if x)
                reqs = body_params or ([text_of(plist[-1].child_by_field_name("type"))] if verb in ("POST", "PUT", "PATCH") and plist
                                       and text_of(plist[-1].child_by_field_name("type"))[:1].isupper() else [])
                prm = [{"name": text_of(p.child_by_field_name("name")), "in": "path" if "{" + text_of(p.child_by_field_name("name")) in full else "query",
                        "type": text_of(p.child_by_field_name("type")), "required": not text_of(p.child_by_field_name("type")).endswith("?")}
                       for p in plist if text_of(p.child_by_field_name("type")) not in reqs and "FromServices" not in _attrs(p)]
                add_endpoint(out, service, repo, path, verb, full, qual, fid, m.start_point[0] + 1, strip, request_schemas=reqs,
                             response_schema=resp, params=prm, auth=auth, response_shape=shape if resp else None)
    # ---- minimal APIs ----------------------------------------------------------------------------------------------------
    for x in walk(root):
        if x.type != "invocation_expression":
            continue
        fname = text_of(x.child_by_field_name("function"))
        last = fname.rsplit(".", 1)[-1]
        if last in MAP:
            argl = x.child_by_field_name("arguments")
            args = [a.named_children[-1] for a in (argl.named_children if argl is not None else []) if a.named_children]
            route = string_value(args[0]) if args else None
            if route:
                chain = text_of(x.parent.parent) if x.parent is not None and x.parent.parent is not None else ""
                auth = ["RequireAuthorization()"] if ".RequireAuthorization(" in chain else []
                h = text_of(args[1]) if len(args) > 1 else "lambda"
                add_endpoint(out, service, repo, path, MAP[last], route, h, f"{repo}:{path}::{h}", x.start_point[0] + 1, strip, auth=auth)
    return out
