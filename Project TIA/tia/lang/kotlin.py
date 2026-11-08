"""Kotlin adapter (PRD FR-040: Spring).

Routes: @RestController/@Controller classes with @RequestMapping prefixes and @GetMapping/@PostMapping/@PutMapping/@DeleteMapping/
@PatchMapping/@RequestMapping(method = [...]) functions; @RequestBody / @PathVariable / @RequestParam parameters; @PreAuthorize/@Secured.
DTOs: (data) classes' constructor properties with Bean Validation (@field:NotBlank, @field:Size(max = 18), @field:Min/Max, @field:Pattern,
@field:Email; the use-site target is optional). Outbound calls: RestTemplate getForObject/getForEntity/postForObject/postForEntity/put/
delete/exchange, WebClient .get()/.post()....uri("..."); a "$base/path" template whose base is a property bound with @Value("${key}")
resolves through that configuration key. Tests: @Test functions; MockMvc perform(get("/path")) and the Kotlin DSL mockMvc.get("/path").
"""
from __future__ import annotations

import re

import tree_sitter_kotlin

from ..models import normalize_path
from .common import add_callsite, add_endpoint, add_function, add_schema, add_test, new_extract, parser, text_of, walk

_P = parser(tree_sitter_kotlin.language())
MAPPING = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}
TEMPLATE = {"getForObject": "GET", "getForEntity": "GET", "postForObject": "POST", "postForEntity": "POST", "put": "PUT", "delete": "DELETE",
            "patchForObject": "PATCH"}
ANN = re.compile(r"@(?:field:|param:|get:)?(\w+)(?:\(((?:[^()]|\([^()]*\))*)\))?")
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _anns(text: str) -> dict:
    return {m.group(1): (m.group(2) or "") for m in ANN.finditer(text or "")}


def _first_str(args: str):
    m = STR.search(args or "")
    return m.group(1) if m else None


def _num_arg(args: str, key: str):
    m = re.search(rf"\b{key}\s*=\s*(-?[\d.]+)", args or "") or (re.fullmatch(r"\s*(-?[\d.]+)\s*", args or "") if key == "value" else None)
    if not m:
        return None
    f = float(m.group(1))
    return int(f) if f.is_integer() else f


def _field(anns: dict, typ: str) -> dict:
    nullable = typ.endswith("?")
    f = {"type": typ, "required": ("NotNull" in anns or "NotBlank" in anns or "NotEmpty" in anns) or not nullable}
    c = {}
    if "Size" in anns:
        for k, ck in (("max", "max_length"), ("min", "min_length")):
            v = _num_arg(anns["Size"], k)
            if v is not None:
                c[ck] = v
    for a, ck in (("Min", "minimum"), ("Max", "maximum"), ("DecimalMin", "minimum"), ("DecimalMax", "maximum")):
        if a in anns:
            v = _num_arg(anns[a], "value") if not anns[a].strip().startswith('"') else _num_arg(anns[a].strip('"'), "value")
            if v is not None:
                c[ck] = v
    if "Pattern" in anns:
        p = re.search(r'regexp\s*=\s*"((?:[^"\\]|\\.)*)"', anns["Pattern"])
        if p:
            c["pattern"] = p.group(1).replace("\\\\", "\\")
    if "Email" in anns:
        c["format"] = "email"
    if "Positive" in anns:
        c["exclusive_minimum"] = 0
    if set(anns) & {"NotBlank", "NotEmpty"} and "min_length" not in c:
        c["min_length"] = 1
    if nullable:
        f["nullable"] = True
    if re.match(r"(List|Set|Collection|MutableList|Array)<", typ):
        f["multi"] = True
    if c:
        f["constraints"] = c
    return f


def _template(s: str, values: dict) -> tuple:
    """A Kotlin string template URL -> (path, base_kind, base_value, reason)."""
    m = re.match(r"^\$\{?(\w+)\}?", s)
    base = None
    if m:
        base = values.get(m.group(1))
        if base is None:
            return (None, None, None, f"base expression not resolvable: {m.group(1)}")
        s = s[m.end():]
    elif s.startswith(("http://", "https://")):
        u = re.match(r"https?://([^/$]+)", s)
        base = ("host", u.group(1).split(":")[0]) if u else None
        s = s[u.end():] if u else s
    path = re.sub(r"\$\{[^}]*\}|\$\w+", "{}", s).split("?", 1)[0]
    if not path.startswith("/"):
        return (None, base[0] if base else None, base[1] if base else None, "path does not start with a literal segment")
    return (path, base[0] if base else None, base[1] if base else None, None if base else "base URL not present in call")


def extract_file(repo: str, service: str, path: str, text: str, strip: tuple = ()):
    out = new_extract(repo, service)
    root = _P.parse(text.encode("utf-8")).root_node
    if not any(n.type in ("class_declaration", "function_declaration", "object_declaration") for n in walk(root)):
        if root.has_error:
            out.files_failed.append({"file": path, "error": "Kotlin source does not parse"})
        return out
    out.files_analysed = 1
    values = {m.group(2): ("config_key", m.group(1)) for m in re.finditer(r'@Value\(\s*"\\?\$\{([^}:]+)(?::[^}]*)?\}"\s*\)\s*(?:private\s+)?(?:lateinit\s+)?va[lr]\s+(\w+)', text)}
    values.update({m.group(2): ("config_key", m.group(1)) for m in re.finditer(r'@Value\(\s*"\\?\$\{([^}:]+)(?::[^}]*)?\}"\s*\)\s*(?:private\s+)?va[lr]\s+(\w+)', text)})
    is_test = "/src/test/" in "/" + path or path.endswith("Test.kt") or path.endswith("Tests.kt")
    prev_end = 0
    for cls in [n for n in walk(root) if n.type in ("class_declaration", "object_declaration")]:
        name_node = next((c for c in cls.children if c.type in ("identifier", "type_identifier", "simple_identifier")), None)
        cname = text_of(name_node)
        top = cls.parent is not None and cls.parent.type == "source_file"
        head = text[prev_end:cls.start_byte] if top else ""
        mods = next((c for c in cls.children if c.type == "modifiers"), None)
        canns = _anns(head + " " + text_of(mods))
        prev_end = cls.end_byte if top else prev_end
        # ---- DTO: constructor properties ----------------------------------------------------------------------------------------
        fields = {}
        for p in walk(cls):
            if p.type == "class_parameter":
                pname = next((text_of(c) for c in p.children if c.type in ("identifier", "simple_identifier")), None)
                ptype = next((text_of(c) for c in p.children if c.type in ("user_type", "nullable_type", "type")), "")
                if pname and ("val " in text_of(p) or "var " in text_of(p)) and "private" not in text_of(p).split(pname)[0]:
                    fields[pname] = _field(_anns(text_of(next((c for c in p.children if c.type == "modifiers"), None))), ptype)
        if fields and not ({"RestController", "Controller", "Service", "Component", "Repository"} & set(canns)):
            add_schema(out, service, repo, path, cname, fields, cls.start_point[0] + 1)
        prefix = _first_str(canns.get("RequestMapping", "")) or ""
        class_auth = [f"@{a}({canns[a]})" for a in ("PreAuthorize", "Secured", "RolesAllowed") if a in canns]
        body = next((c for c in cls.children if c.type == "class_body"), None)
        for fn in (body.named_children if body is not None else []):
            if fn.type != "function_declaration":
                continue
            fname = next((text_of(c) for c in fn.children if c.type in ("identifier", "simple_identifier")), "")
            qual = f"{cname}.{fname}"
            fid = f"{repo}:{path}::{qual}"
            fmods = text_of(next((c for c in fn.children if c.type == "modifiers"), None))
            fanns = _anns(fmods)
            calls, http = [], []
            ftext = text_of(fn)
            for x in walk(fn):
                if x.type != "call_expression":
                    continue
                callee = text_of(x.children[0]) if x.children else ""
                last = callee.rsplit(".", 1)[-1]
                calls.append(last)
                args = text_of(next((c for c in x.children if c.type in ("value_arguments", "call_suffix")), None))
                line = x.start_point[0] + 1
                if last in TEMPLATE or last == "exchange":
                    s = _first_str(args)
                    if s is not None and not is_test:
                        method = TEMPLATE.get(last) or (re.search(r"HttpMethod\.(\w+)", args).group(1) if re.search(r"HttpMethod\.(\w+)", args) else None)
                        add_callsite(out, repo, service, path, line, fid, method, _template(s, values), strip)
                elif last == "uri":
                    verb = re.search(r"\.(get|post|put|delete|patch)\(\)\s*\.?\s*$", callee.rsplit(".uri", 1)[0] + ".") or \
                        re.search(r"\.(get|post|put|delete|patch)\(\)", callee)
                    s = _first_str(args)
                    if s is not None and not is_test:
                        add_callsite(out, repo, service, path, line, fid, verb.group(1).upper() if verb else None, _template(s, values), strip)
                elif is_test and last in ("get", "post", "put", "delete", "patch") and _first_str(args):
                    p_ = _first_str(args)
                    if p_.startswith("/"):
                        http.append([last.upper(), normalize_path(re.sub(r"\$\{[^}]*\}|\$\w+", "{}", p_).split("?")[0], strip)])
            params = next((c for c in fn.children if c.type == "function_value_parameters"), None)
            plist, pending = [], ""
            for c in (params.named_children if params is not None else []):
                if c.type == "parameter_modifiers":
                    pending = text_of(c)
                elif c.type == "parameter":
                    pn = next((text_of(x) for x in c.children if x.type in ("identifier", "simple_identifier")), "")
                    pt = next((text_of(x) for x in c.children if x.type in ("user_type", "nullable_type", "type")), "")
                    plist.append((pn, pt, _anns(pending)))
                    pending = ""
            sig = {"params": [{"name": n, "kind": "positional", "type": t} for n, t, _ in plist], "returns": None}
            if is_test and "Test" in fanns:
                add_test(out, repo, path, qual, fname, fn.start_point[0] + 1, [], calls, http)
                continue
            add_function(out, repo, path, qual, fn, calls, sig, fn.start_point[0] + 1)
            verbs = [(MAPPING[a], fanns[a]) for a in fanns if a in MAPPING]
            if "RequestMapping" in fanns:
                ms = re.findall(r"RequestMethod\.(\w+)", fanns["RequestMapping"]) or ["GET"]
                verbs += [(m, fanns["RequestMapping"]) for m in ms]
            if not verbs:
                continue
            reqs = [t.rstrip("?") for n, t, a in plist if "RequestBody" in a]
            prm = [{"name": n, "in": "path" if "PathVariable" in a else "query", "type": t, "required": not t.endswith("?")}
                   for n, t, a in plist if "PathVariable" in a or "RequestParam" in a]
            ret = re.search(r"\)\s*:\s*([\w<>?, .]+?)\s*(=|\{)", ftext)
            rtype = ret.group(1).strip() if ret else ""
            shape = "list" if re.match(r"(List|Set|Collection|Flux)<", rtype) else ("object" if rtype and rtype not in ("Unit", "ResponseEntity<Unit>") else None)
            resp = re.sub(r"^(ResponseEntity|Mono|List|Set|Collection|Flux)<(.+)>$", r"\2", rtype).rstrip("?") or None
            auth = sorted(set(class_auth + [f"@{a}({fanns[a]})" for a in ("PreAuthorize", "Secured", "RolesAllowed") if a in fanns]))
            for verb, aargs in verbs:
                sub = _first_str(aargs) or ""
                full = "/" + "/".join(x for x in (prefix.strip("/"), sub.strip("/")) if x)
                add_endpoint(out, service, repo, path, verb, full, qual, fid, fn.start_point[0] + 1, strip, request_schemas=reqs,
                             response_schema=resp if resp and re.fullmatch(r"[A-Z]\w*", resp) else None, params=prm, auth=auth,
                             response_shape=shape)
    return out
