"""Java source adapter (Spring Boot / Spring Web): endpoints, DTO schemas, outbound HTTP call sites, functions, tests.

Parses with tree-sitter-java (modern Java: records, var, text blocks, switch expressions); repository code is parsed,
never compiled or executed. Emits the same normalised entities as the Python adapter and never creates graph edges.

Supported
  provider   @RestController/@Controller with @RequestMapping/@Get|Post|Put|Delete|PatchMapping (value/path, arrays,
             class-level prefix, RequestMethod lists); controller interfaces that carry the mapping annotations
  schemas    records and classes used by endpoints (request @RequestBody, return type incl. ResponseEntity/Mono/List...)
             and, transitively, their field types; fields with required-ness (@NotNull/@NotBlank/@NotEmpty, primitives)
  consumer   RestTemplate, WebClient, RestClient, OpenFeign (@FeignClient) and shared client libraries (R1 via service map)
  tests      JUnit 4/5 @Test methods; MockMvc, WebTestClient and TestRestTemplate calls with literal paths
  base URLs  string constants, @Value("${key}") fields and constructor parameters, environment.getProperty / System.getenv,
             WebClient.create/baseUrl(...) and RestTemplateBuilder.rootUri(...)

Cross-file facts (a Feign interface and its callers live in different files) are kept per file and resolved when the
repository extract is assembled, so per-file caching and the incremental == full-rescan property are preserved."""
from __future__ import annotations

import hashlib
import re
from typing import Iterator, Optional
from urllib.parse import urlsplit

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from . import EXTRACTOR_VERSION
from .extract_data import SPRING_DATA_BASES, snake
from .models import CallSite, Endpoint, Entity, Function, RepoExtract, Schema, TestCase, normalize_path

_LANG = Language(tree_sitter_java.language())

VERB_ANNOTATIONS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
                    "PatchMapping": "PATCH"}
TEMPLATE_METHODS = {"getForObject": "GET", "getForEntity": "GET", "postForObject": "POST", "postForEntity": "POST",
                    "postForLocation": "POST", "put": "PUT", "delete": "DELETE", "patchForObject": "PATCH",
                    "headForHeaders": "HEAD", "exchange": None, "execute": None}
FLUENT_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH", "head": "HEAD",
                "options": "OPTIONS"}
TEMPLATE_TYPES = {"RestTemplate"}
FLUENT_TYPES = {"WebClient", "RestClient"}
TEST_CLIENT_TYPES = {"TestRestTemplate", "WebTestClient", "MockMvc", "RestTemplate"}
TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
STEREOTYPES = {"RestController", "Controller", "Service", "Component", "Repository", "Configuration", "SpringBootApplication",
               "FeignClient", "ControllerAdvice", "RestControllerAdvice", "Mapper"}
WRAPPERS = {"ResponseEntity", "Mono", "Optional", "CompletableFuture", "Future", "DeferredResult", "Callable", "HttpEntity",
            "Single", "Maybe", "ListenableFuture"}
COLLECTIONS = {"List", "Set", "Collection", "Iterable", "Flux", "Page", "Slice", "Stream", "Publisher", "ArrayList",
               "LinkedList", "SortedSet", "Queue"}
BUILTIN_TYPES = {"String", "Object", "Void", "void", "Integer", "Long", "Double", "Float", "Boolean", "Short", "Byte",
                 "Character", "BigDecimal", "BigInteger", "UUID", "LocalDate", "LocalDateTime", "Instant", "Map", "Date",
                 "OffsetDateTime", "ZonedDateTime", "Duration", "byte", "int", "long", "double", "float", "boolean", "char"}
FRAMEWORK_PREFIXES = ("java.", "javax.", "jakarta.", "org.springframework.", "org.slf4j.", "org.apache.", "com.fasterxml.",
                      "org.junit.", "org.mockito.", "lombok.", "org.hibernate.", "reactor.", "io.micrometer.")
BUILDER_TYPES = {"WebClient.Builder": "WebClient", "RestClient.Builder": "RestClient", "RestTemplateBuilder": "RestTemplate"}
# HTTP client libraries whose calls this adapter does not analyse: a file using one must be reported, never read as "no calls"
UNSUPPORTED_HTTP = {"okhttp3": "OkHttp", "org.apache.http": "Apache HttpClient", "org.apache.hc": "Apache HttpClient",
                    "java.net.http": "java.net.http.HttpClient", "retrofit2": "Retrofit", "io.vertx.ext.web.client": "Vert.x WebClient",
                    "kong.unirest": "Unirest", "com.mashape.unirest": "Unirest", "javax.ws.rs.client": "JAX-RS client",
                    "jakarta.ws.rs.client": "JAX-RS client", "org.glassfish.jersey.client": "Jersey client"}
# data-access libraries whose queries are not analysed: a file using one must be reported, never read as "no data access"
UNANALYSED_DATA_LIBS = {"jakarta.persistence.EntityManager": "JPA EntityManager", "javax.persistence.EntityManager": "JPA EntityManager",
                        "org.springframework.jdbc": "Spring JDBC", "org.jooq": "jOOQ", "org.hibernate.Session": "Hibernate Session",
                        "org.apache.ibatis": "MyBatis", "org.mybatis": "MyBatis", "java.sql": "JDBC", "org.springframework.data.mongodb": "MongoDB",
                        "org.springframework.data.redis": "Redis", "org.springframework.data.jdbc": "Spring Data JDBC"}
ENTITY_ANNOTATIONS = {"Entity", "MappedSuperclass", "Embeddable"}
PRIMITIVE_NODES = {"integral_type", "floating_point_type", "boolean_type"}
REQUIRED_ANNOTATIONS = {"NotNull", "NotBlank", "NotEmpty"}
_PLACEHOLDER = re.compile(r"^\$\{([^}:]+)(?::[^}]*)?\}$")
_FORMAT_SPEC = re.compile(r"%(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z%]")


def _t(n: Optional[Node]) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _walk(n: Node) -> Iterator[Node]:
    stack = [n]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")


def _string(n: Optional[Node]) -> Optional[str]:
    if n is not None and n.type == "string_literal":
        return _unescape("".join(_t(c) for c in n.children
                                 if c.type in ("string_fragment", "escape_sequence", "multiline_string_fragment")))
    return None


def _placeholder_key(s: str) -> Optional[str]:
    m = _PLACEHOLDER.match(s.strip())
    return m.group(1).strip() if m else None


def module_fqn(path: str) -> str:
    """Fully-qualified-name style module of a Java source file ('src/main/java/com/acme/A.java' -> 'com.acme.A')."""
    p = path[:-5] if path.endswith(".java") else path
    marker = "/java/"
    i = ("/" + p).rfind(marker)
    if i >= 0:
        p = ("/" + p)[i + len(marker):]
    return p.replace("/", ".")


def _is_test_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return "/src/test/" in "/" + path or base.endswith(("Test.java", "Tests.java", "IT.java", "ITCase.java"))


def _is_stub(path: str) -> bool:
    return path.endswith(("package-info.java", "module-info.java"))


class _JavaVisitor:
    def __init__(self, repo: str, service: str, path: str, src: bytes, root: Node, out: RepoExtract, strip: tuple):
        self.repo, self.service, self.path, self.root, self.out, self.strip = repo, service, path, root, out, strip
        self.is_test_file = _is_test_file(path)
        self.package = ""
        self.imports: dict[str, str] = {}                 # simple name -> fqn
        self.import_list: list[str] = []
        self.consts: dict[str, tuple[str, str]] = {}      # name -> (literal | config_key, value)
        self.types: dict[str, str] = {}                   # variable name -> simple type name
        self.bases: dict[str, tuple[str, str]] = {}       # http client variable -> (base_kind, base_value)
        self.method_returns: dict[str, Optional[Node]] = {}   # no-argument method -> the expression it returns (None when ambiguous)
        self._resolving: set = set()
        self.used_ids: dict[str, int] = {}
        self.type_names_used: set[str] = set()

    # ---- file-level pre-pass ------------------------------------------------------------------------------
    def prepass(self):
        for n in self.root.children:
            if n.type == "package_declaration":
                self.package = _t(next((c for c in n.children if c.type in ("scoped_identifier", "identifier")), None))
            elif n.type == "import_declaration":
                ident = next((c for c in n.children if c.type in ("scoped_identifier", "identifier")), None)
                full, star = _t(ident), any(c.type == "asterisk" for c in n.children)
                self.import_list.append(full + (".*" if star else ""))
                if not star and "static" not in [c.type for c in n.children]:
                    self.imports[_simple(full)] = full
        libs = sorted({lib for imp in self.import_list for prefix, lib in UNSUPPORTED_HTTP.items()
                       if imp == prefix or imp.startswith(prefix + ".")})
        self.out.unsupported_clients += [{"file": self.path, "library": lib} for lib in libs]
        if not self.is_test_file:
            for lib in sorted({name for imp in self.import_list for prefix, name in UNANALYSED_DATA_LIBS.items()
                               if imp == prefix or imp.startswith(prefix + ".")}):
                self.out.data_unknowns.append({"file": self.path, "line": 0, "kind": "data_access_library",
                                               "reason": f"{lib} is used here; the data it reads or writes is not analysed (FR-041)"})
        local_vals: dict[str, list] = {}
        for n in _walk(self.root):
            if n.type == "field_declaration":
                self._declare(n)
            elif n.type in ("formal_parameter", "spread_parameter"):
                self._param(n)
            elif n.type == "local_variable_declaration":
                tn = n.child_by_field_name("type")
                for d in n.children:
                    if d.type == "variable_declarator":
                        self._var_type(d.child_by_field_name("name"), tn, d.child_by_field_name("value"))
                        v = d.child_by_field_name("value")
                        if v is not None:
                            local_vals.setdefault(_t(d.child_by_field_name("name")), []).append(v)
            elif n.type == "method_declaration":
                params = n.child_by_field_name("parameters")
                body = n.child_by_field_name("body")
                if body is not None and not (params is not None and params.named_children):
                    rets = [r for r in _walk(body) if r.type == "return_statement" and r.named_children]
                    nm = _t(n.child_by_field_name("name"))
                    self.method_returns[nm] = rets[0].named_children[0] if len(rets) == 1 and nm not in self.method_returns else None
            elif n.type == "type_identifier":
                self.type_names_used.add(_t(n))
        # a local variable holding a URL or path ("String host = \"http://vets-service/\"") is a constant for the calls that use it, provided
        # every assignment of that name in the file agrees (the same name holding two different URLs in two methods stays unresolved)
        for nm, vals in local_vals.items():
            strings = {self._const_str(v) for v in vals}
            if len(strings) == 1 and None not in strings and nm not in self.consts:
                sv = next(iter(strings))
                if sv.startswith(("http://", "https://", "/")):
                    self.consts[nm] = ("literal", sv)
        for n in _walk(self.root):                         # http client bases: baseUrl(...), WebClient.create(...), rootUri(...)
            if n.type == "variable_declarator":
                self._bind_base(n.child_by_field_name("name"), n.child_by_field_name("value"))
            elif n.type == "assignment_expression":
                self._bind_base(n.child_by_field_name("left"), n.child_by_field_name("right"))

    def _var_type(self, name: Optional[Node], tn: Optional[Node], value: Optional[Node]):
        if name is None:
            return
        ty = self._type_name(tn)
        if ty == "var" and value is not None and value.type == "object_creation_expression":
            ty = self._type_name(value.child_by_field_name("type"))
        if ty:
            self.types[_t(name)] = ty

    def _type_name(self, tn: Optional[Node]) -> str:
        if tn is None:
            return ""
        if tn.type == "generic_type":
            tn = next((c for c in tn.children if c.type in ("type_identifier", "scoped_type_identifier")), tn)
        full = _t(tn)
        return BUILDER_TYPES.get(full) or BUILDER_TYPES.get(_simple(full)) or _simple(full)

    def _declare(self, f: Node):
        mods = self.annotations(next((c for c in f.children if c.type == "modifiers"), None))
        tn = f.child_by_field_name("type")
        static_final = any(c.type == "modifiers" and "static" in _t(c) and "final" in _t(c) for c in f.children)
        for d in f.children:
            if d.type != "variable_declarator":
                continue
            name, value = d.child_by_field_name("name"), d.child_by_field_name("value")
            self._var_type(name, tn, value)
            nm = _t(name)
            if "Value" in mods and mods["Value"]:
                s = _string(mods["Value"][0][1])
                key = _placeholder_key(s) if s is not None else None
                if key:
                    self.consts[nm] = ("config_key", key)
                elif s is not None:
                    self.consts[nm] = ("literal", s)
            elif value is not None:                                  # constants, and fields initialised with a literal URL/path
                s = _string(value)
                if s is not None and (static_final or s.startswith(("http://", "https://", "/"))):
                    self.consts[nm] = ("literal", s)

    def _param(self, p: Node):
        mods = self.annotations(next((c for c in p.children if c.type == "modifiers"), None))
        name, tn = p.child_by_field_name("name"), p.child_by_field_name("type")
        self._var_type(name, tn, None)
        if "Value" in mods and mods["Value"] and name is not None:
            s = _string(mods["Value"][0][1])
            key = _placeholder_key(s) if s is not None else None
            if key:
                self.consts[_t(name)] = ("config_key", key)

    def _bind_base(self, lhs: Optional[Node], rhs: Optional[Node]):
        if lhs is None or rhs is None:
            return
        for inv in _walk(rhs):
            if inv.type != "method_invocation":
                continue
            nm = _t(inv.child_by_field_name("name"))
            obj = inv.child_by_field_name("object")
            if nm in ("baseUrl", "rootUri") or (nm == "create" and _t(obj) in ("WebClient", "RestClient")):
                args = inv.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                base = self._base_of(first) if first is not None else None
                if base:
                    self.bases[self._var_of(lhs) or _t(lhs)] = base
                    return

    # ---- annotations ----------------------------------------------------------------------------------------
    def annotations(self, mods: Optional[Node]) -> dict:
        out: dict[str, list] = {}
        if mods is None:
            return out
        for a in mods.children:
            if a.type == "marker_annotation":
                out[_simple(_t(a.child_by_field_name("name")))] = []
            elif a.type == "annotation":
                items = []
                args = a.child_by_field_name("arguments")
                for c in (args.named_children if args is not None else []):
                    if c.type == "element_value_pair":
                        items.append((_t(c.child_by_field_name("key")), c.child_by_field_name("value")))
                    else:
                        items.append((None, c))
                out[_simple(_t(a.child_by_field_name("name")))] = items
        return out

    @staticmethod
    def _arg(items: list, *keys: str) -> Optional[Node]:
        for k, v in items:
            if k in keys or (k is None and "value" in keys):
                return v
        return None

    @staticmethod
    def _values(n: Optional[Node]) -> list:
        if n is None:
            return []
        if n.type in ("element_value_array_initializer", "array_initializer"):
            return list(n.named_children)
        return [n]

    # ---- constant / URL evaluation ------------------------------------------------------------------------
    def _const_str(self, n: Optional[Node]) -> Optional[str]:
        if n is None:
            return None
        s = _string(n)
        if s is not None:
            return s
        if n.type == "identifier" and self.consts.get(_t(n), ("",))[0] == "literal":
            return self.consts[_t(n)][1]
        if n.type == "binary_expression" and _t(n.child_by_field_name("operator")) == "+":
            a, b = self._const_str(n.child_by_field_name("left")), self._const_str(n.child_by_field_name("right"))
            return a + b if a is not None and b is not None else None
        if n.type == "parenthesized_expression" and n.named_children:
            return self._const_str(n.named_children[0])
        return None

    def _var_of(self, n: Optional[Node]) -> Optional[str]:
        if n is None:
            return None
        if n.type == "identifier":
            return _t(n)
        if n.type == "field_access":
            return _t(n.child_by_field_name("field"))
        return None

    def _base_of(self, n: Optional[Node]) -> Optional[tuple]:
        """(kind, value) for something that can only be the base of a URL: a constant, a config key, a literal URL."""
        if n is None:
            return None
        s = self._const_str(n)
        if s is not None:
            if s.startswith(("http://", "https://")):
                return ("literal", s)
            key = _placeholder_key(s)
            return ("config_key", key) if key else ("literal", s)
        v = self._var_of(n)
        if v and v in self.consts:
            return self.consts[v]
        if n.type == "method_invocation":
            nm = _t(n.child_by_field_name("name"))
            obj = _t(n.child_by_field_name("object"))
            args = n.child_by_field_name("arguments")
            first = _string(args.named_children[0]) if args is not None and args.named_children else None
            if first and ((nm == "getProperty" and obj.rsplit(".", 1)[-1].lower() in ("environment", "env")) or
                          (nm == "getenv" and obj == "System")):
                return ("config_key", first)
            if not obj and nm in self.method_returns and self.method_returns[nm] is not None and not (args is not None and args.named_children):
                return self._returned_base(nm)
        return None

    def _returned_base(self, name: str) -> Optional[tuple]:
        """The base a no-argument method in this file returns: a constant or config key, or the instance of a service that service discovery
        looks up BY NAME (discoveryClient.getInstances("customers-service").get(0).getUri()), which is that service's host."""
        if name in self._resolving:
            return None
        self._resolving.add(name)
        try:
            expr = self.method_returns[name]
            base = self._base_of(expr)
            if base:
                return base
            for inv in _walk(expr):
                if inv.type == "method_invocation" and _t(inv.child_by_field_name("name")) in ("getInstances", "choose", "getInstance"):
                    args = inv.child_by_field_name("arguments")
                    svc = _string(args.named_children[0]) if args is not None and args.named_children else None
                    if svc and re.fullmatch(r"[A-Za-z0-9._-]+", svc):
                        return ("literal", f"http://{svc}")
            return None
        finally:
            self._resolving.discard(name)

    def parse_url(self, n: Optional[Node]):
        """-> (path_template | None, base_kind, base_value, reason)"""
        if n is None:
            return (None, None, None, "no URL argument")
        s = self._const_str(n)
        if s is not None:
            return self._from_literal(s)
        if n.type == "binary_expression" and _t(n.child_by_field_name("operator")) == "+":
            parts: list = []

            def flat(x):
                if x.type == "binary_expression" and _t(x.child_by_field_name("operator")) == "+":
                    flat(x.child_by_field_name("left")); flat(x.child_by_field_name("right"))
                elif self._const_str(x) is not None and x.type != "identifier":
                    parts.append(("s", self._const_str(x)))
                else:
                    parts.append(("v", x))
            flat(n)
            return self._from_parts(parts)
        if n.type == "method_invocation":
            nm, obj = _t(n.child_by_field_name("name")), _t(n.child_by_field_name("object"))
            args = n.child_by_field_name("arguments")
            a = args.named_children if args is not None else []
            if nm == "format" and obj == "String" and a and self._const_str(a[0]) is not None:
                fmt, parts, last, i = self._const_str(a[0]), [], 0, 1
                for m in _FORMAT_SPEC.finditer(fmt):
                    if fmt[last:m.start()]:
                        parts.append(("s", fmt[last:m.start()]))
                    if m.group(0) != "%%" and i < len(a):
                        parts.append(("v", a[i])); i += 1
                    last = m.end()
                if fmt[last:]:
                    parts.append(("s", fmt[last:]))
                return self._from_parts(parts)
            if nm == "create" and obj == "URI" and a:
                return self.parse_url(a[0])
        if n.type == "object_creation_expression" and _t(n.child_by_field_name("type")) == "URI":
            args = n.child_by_field_name("arguments")
            return self.parse_url(args.named_children[0] if args is not None and args.named_children else None)
        return (None, None, None, f"URL expression not statically resolvable: {_t(n)[:60]}")

    def _from_literal(self, s: str):
        if s.startswith(("http://", "https://")):
            u = urlsplit(s)
            return (u.path or "/", "host", u.hostname, None)
        return (s, None, None, "base URL not present in call")

    def _from_parts(self, parts: list):
        base_kind = base_val = None
        path, i = "", 0
        if parts and parts[0][0] == "v":
            c = self._base_of(parts[0][1])
            if c and c[0] == "literal" and c[1].startswith(("http://", "https://")):
                u = urlsplit(c[1]); base_kind, base_val = "host", u.hostname; path += u.path
            elif c and c[0] == "config_key":
                base_kind, base_val = c
            elif c:
                base_kind, base_val = "host", c[1]
            else:
                return (None, None, None, f"base expression not resolvable: {_t(parts[0][1])[:40]}")
            i = 1
        elif parts and parts[0][0] == "s" and parts[0][1].startswith(("http://", "https://")):
            u = urlsplit(parts[0][1])
            base_kind, base_val = "host", u.hostname
            path += parts[0][1][len(f"{u.scheme}://{u.netloc}"):]
            i = 1
        for kind, v in parts[i:]:
            path += v if kind == "s" else "{}"
        if not path.startswith("/"):
            return (None, base_kind, base_val, "path does not start with a literal segment")
        return (path, base_kind, base_val, None)

    # ---- types ---------------------------------------------------------------------------------------------------
    def unwrap(self, tn: Optional[Node]) -> Optional[str]:
        """Schema name a return/body type refers to: ResponseEntity<Foo> -> Foo, List<Foo> -> List[Foo]."""
        if tn is None or tn.type in ("void_type",) or tn.type in PRIMITIVE_NODES:
            return None
        if tn.type == "array_type":
            inner = self.unwrap(tn.child_by_field_name("element"))
            return f"List[{inner}]" if inner else None
        if tn.type in ("type_identifier", "scoped_type_identifier"):
            return _simple(_t(tn))
        if tn.type == "generic_type":
            base = _simple(_t(next((c for c in tn.children if c.type in ("type_identifier", "scoped_type_identifier")), None)))
            args = next((c for c in tn.children if c.type == "type_arguments"), None)
            first = args.named_children[0] if args is not None and args.named_children else None
            if base in WRAPPERS:
                return self.unwrap(first)
            if base in COLLECTIONS:
                inner = self.unwrap(first)
                return f"List[{inner}]" if inner else None
            return base
        return None

    # ---- main walk -----------------------------------------------------------------------------------------------
    def run(self):
        self.prepass()
        for n in self.root.children:
            self.visit_type(n, [])

    def visit_type(self, node: Node, scope: list):
        if node.type not in ("class_declaration", "interface_declaration", "record_declaration", "enum_declaration"):
            return
        name = _t(node.child_by_field_name("name"))
        mods = self.annotations(next((c for c in node.children if c.type == "modifiers"), None))
        qual_scope = scope + [name]
        body = node.child_by_field_name("body")
        is_interface = node.type == "interface_declaration"
        feign = mods.get("FeignClient")
        class_paths = self._mapping_paths(mods.get("RequestMapping"), [""]) if "RequestMapping" in mods else [""]
        if not self.is_test_file and node.type == "class_declaration" and set(mods) & ENTITY_ANNOTATIONS:
            self._add_entity(node, name, mods)
        elif not self.is_test_file and is_interface and feign is None:
            self._add_repository(node, name)
        if feign is not None:
            self._add_client(node, name, feign, body)
        elif node.type in ("class_declaration", "record_declaration") and not (set(mods) & STEREOTYPES):
            self._add_schema(node, name)
        if body is not None:
            for m in body.children:
                if m.type in ("method_declaration", "constructor_declaration"):
                    if feign is not None:
                        continue                              # a Feign interface declares calls, it is not an implementation
                    self.visit_method(m, qual_scope, class_paths, is_interface)
                elif m.type in ("class_declaration", "interface_declaration", "record_declaration", "enum_declaration"):
                    self.visit_type(m, qual_scope)

    # ---- schemas ---------------------------------------------------------------------------------------------------
    def _required(self, mods: dict, tn: Optional[Node]) -> bool:
        return bool(set(mods) & REQUIRED_ANNOTATIONS) or (tn is not None and tn.type in PRIMITIVE_NODES)

    def _json_name(self, mods: dict, name: str) -> Optional[str]:
        """The name a field has on the wire: @JsonProperty("x") renames it, @JsonIgnore hides it."""
        if "JsonIgnore" in mods:
            return None
        jp = mods.get("JsonProperty")
        if jp:
            s = self._const_str(self._arg(jp, "value"))
            if s:
                return s
        return name

    def _add_schema(self, node: Node, name: str):
        fields: dict = {}
        sup = node.child_by_field_name("superclass")
        extends = _simple(_t(next((c for c in sup.children if c.type in ("type_identifier", "scoped_type_identifier",
                                                                          "generic_type")), None))).split("<")[0] if sup is not None else None
        if node.type == "record_declaration":
            params = node.child_by_field_name("parameters")
            for p in (params.named_children if params is not None else []):
                if p.type == "formal_parameter":
                    tn = p.child_by_field_name("type")
                    mods = self.annotations(next((c for c in p.children if c.type == "modifiers"), None))
                    key = self._json_name(mods, _t(p.child_by_field_name("name")))
                    if key:
                        fields[key] = {"type": _t(tn), "required": self._required(mods, tn)}
        else:
            body = node.child_by_field_name("body")
            for f in (body.children if body is not None else []):
                if f.type != "field_declaration":
                    continue
                m = next((c for c in f.children if c.type == "modifiers"), None)
                if m is not None and "static" in [c.type for c in m.children]:
                    continue
                tn, mods = f.child_by_field_name("type"), self.annotations(m)
                for d in f.children:
                    if d.type == "variable_declarator":
                        key = self._json_name(mods, _t(d.child_by_field_name("name")))
                        if key:
                            fields[key] = {"type": _t(tn), "required": self._required(mods, tn)}
        if fields or extends:
            self.out.schemas.append(Schema(id=f"{self.service}:schema:{name}", service=self.service, repo=self.repo, name=name,
                                           fields=fields, file=self.path, line=node.start_point[0] + 1, extends=extends))

    # ---- JPA entities and Spring Data repositories ---------------------------------------------------------------------------
    def _names_of(self, node: Optional[Node]) -> list:
        """name = "x" values of the @JoinColumn(s) held by an annotation attribute (single annotation or array)."""
        out = []
        for v in self._values(node):
            if v.type == "annotation":
                args = v.child_by_field_name("arguments")
                for c in (args.named_children if args is not None else []):
                    if c.type == "element_value_pair" and _t(c.child_by_field_name("key")) == "name":
                        s = self._const_str(c.child_by_field_name("value"))
                        if s:
                            out.append(s.lower())
        return out

    def _flag(self, items: list, key: str, default: bool) -> bool:
        v = self._arg(items, key)
        return _t(v) == "true" if v is not None and _t(v) in ("true", "false") else default

    def _add_entity(self, node: Node, name: str, mods: dict):
        table, source = snake(name), "naming_strategy"
        tbl = mods.get("Table")
        if tbl:
            s = self._const_str(self._arg(tbl, "name"))
            if s:
                table, source = s.lower(), "declared"
        sup = node.child_by_field_name("superclass")
        bases = []
        if sup is not None:
            sn = next((c for c in sup.children if c.type in ("type_identifier", "scoped_type_identifier", "generic_type")), None)
            if sn is not None:
                bases.append(_simple(_t(sn)).split("<")[0])
        cols, joins, embedded, join_tables, notes = {}, [], [], [], []
        body = node.child_by_field_name("body")
        line = node.start_point[0] + 1
        if set(mods) & {"Inheritance", "DiscriminatorColumn", "DiscriminatorValue"}:
            self.out.data_unknowns.append({"file": self.path, "line": line, "kind": "entity_inheritance",
                                           "reason": f"{name}: entity inheritance mapping is not analysed"})
        for f in (body.children if body is not None else []):
            if f.type != "field_declaration":
                continue
            m = next((c for c in f.children if c.type == "modifiers"), None)
            kinds = [c.type for c in m.children] if m is not None else []
            ann = self.annotations(m)
            if "static" in kinds or "transient" in kinds or "Transient" in ann:
                continue
            tn = f.child_by_field_name("type")
            for d in f.children:
                if d.type != "variable_declarator":
                    continue
                fld = _t(d.child_by_field_name("name"))
                fline = d.start_point[0] + 1
                rel = next((r for r in ("ManyToOne", "OneToOne", "OneToMany", "ManyToMany") if r in ann), None)
                if "EmbeddedId" in ann or "Embedded" in ann:
                    embedded.append(self.unwrap(tn) or _simple(_t(tn)))
                    continue
                if "ElementCollection" in ann:
                    ct = ann.get("CollectionTable")
                    ctn = self._const_str(self._arg(ct, "name")) if ct else None
                    if ctn:
                        jc = self._names_of(self._arg(ct, "joinColumns")) or []
                        vcol = ann.get("Column")
                        vname = (self._const_str(self._arg(vcol, "name")) if vcol else None) or snake(fld)
                        join_tables.append({"table": ctn.lower(), "columns": {c: {"field": fld, "type": "fk", "nullable": False, "pk": False}
                                                                             for c in jc + [vname.lower()]}})
                    else:
                        self.out.data_unknowns.append({"file": self.path, "line": fline, "kind": "default_collection_table",
                                                       "reason": f"{name}.{fld}: collection table name is not declared"})
                    continue
                if rel in ("OneToMany",):
                    continue                                           # the foreign key lives in the other table
                if rel == "ManyToMany":
                    jt = ann.get("JoinTable")
                    jtn = self._const_str(self._arg(jt, "name")) if jt else None
                    if jt and jtn:
                        cs = self._names_of(self._arg(jt, "joinColumns")) + self._names_of(self._arg(jt, "inverseJoinColumns"))
                        if cs:
                            join_tables.append({"table": jtn.lower(), "columns": {c: {"field": fld, "type": "fk", "nullable": False,
                                                                                      "pk": False} for c in cs}})
                            continue
                    if not self._arg(ann["ManyToMany"], "mappedBy"):
                        self.out.data_unknowns.append({"file": self.path, "line": fline, "kind": "default_join_table",
                                                       "reason": f"{name}.{fld}: many-to-many join table name is not declared"})
                    continue
                if rel in ("ManyToOne", "OneToOne"):
                    if self._arg(ann[rel], "mappedBy"):
                        continue
                    jc = ann.get("JoinColumn")
                    explicit = self._const_str(self._arg(jc, "name")) if jc else None
                    nullable = self._flag(jc, "nullable", True) if jc else True
                    nullable = nullable and self._flag(ann[rel], "optional", True)
                    joins.append({"field": fld, "target": self.unwrap(tn) or _simple(_t(tn)), "name": explicit.lower() if explicit else None,
                                  "nullable": nullable, "pk": "Id" in ann})
                    continue
                col = ann.get("Column")
                cname = (self._const_str(self._arg(col, "name")) if col else None) or snake(fld)
                pk = "Id" in ann
                nullable = (not pk) and not (tn is not None and tn.type in PRIMITIVE_NODES)
                if col and self._arg(col, "nullable") is not None:
                    nullable = self._flag(col, "nullable", nullable)
                cols[cname.lower()] = {"field": fld, "type": _t(tn), "nullable": nullable, "pk": pk}
        abstract = "Entity" not in mods
        self.out.entities.append(Entity(
            id=f"{self.service}:entity:{name}", service=self.service, repo=self.repo, name=name, table=table,
            table_source=source, columns=cols, kind="jpa", file=self.path, line=line, bases=bases, abstract=abstract,
            joins=joins, embedded=sorted(set(embedded)), join_tables=join_tables,
            notes=[] if source == "declared" or abstract else ["table name derived from the class name by Spring Boot's default naming strategy"]))

    def _add_repository(self, node: Node, name: str):
        ext = next((c for c in node.children if c.type == "extends_interfaces"), None)
        if ext is None or not any(i.startswith("org.springframework.data") for i in self.import_list):
            return
        entity = None
        for tl in ext.children:
            for g in tl.children:
                if g.type == "generic_type":
                    base = _simple(_t(next((c for c in g.children if c.type in ("type_identifier", "scoped_type_identifier")), None)))
                    args = next((c for c in g.children if c.type == "type_arguments"), None)
                    if base in SPRING_DATA_BASES and args is not None and args.named_children:
                        entity = _simple(_t(args.named_children[0])).split("<")[0]
        if entity is None:
            return
        methods: dict = {}
        body = node.child_by_field_name("body")
        for m in (body.children if body is not None else []):
            if m.type != "method_declaration":
                continue
            ann = self.annotations(next((c for c in m.children if c.type == "modifiers"), None))
            q = ann.get("Query")
            query = self._const_str(self._arg(q, "value")) if q else None
            entry = {"query": query, "native": bool(q and self._flag(q, "nativeQuery", False)), "modifying": "Modifying" in ann}
            if q and query is None:
                self.out.data_unknowns.append({"file": self.path, "line": m.start_point[0] + 1, "kind": "query_not_constant",
                                               "reason": f"{name}.{_t(m.child_by_field_name('name'))}: @Query text is not a constant"})
            methods.setdefault(_t(m.child_by_field_name("name")), entry)
        self.out.repositories.append({"name": name, "entity": entity, "methods": methods, "file": self.path,
                                      "line": node.start_point[0] + 1})

    # ---- OpenFeign clients ---------------------------------------------------------------------------------------------
    def _add_client(self, node: Node, name: str, feign: list, body: Optional[Node]):
        url = _string(self._arg(feign, "url")) if self._arg(feign, "url") is not None else None
        svc = self._arg(feign, "name", "value")
        svc_s = _string(svc) if svc is not None else None
        if url:
            key = _placeholder_key(url)
            bk, bv = ("config_key", key) if key else (self._from_literal(url)[1:3] if url.startswith("http") else ("host", url))
        elif svc_s:
            key = _placeholder_key(svc_s)
            bk, bv = ("config_key", key) if key else ("host", svc_s)
        else:
            bk, bv = None, None
        prefix_node = self._arg(feign, "path")
        prefix = (_string(prefix_node) if prefix_node is not None else "") or ""
        methods: dict = {}
        for m in (body.children if body is not None else []):
            if m.type != "method_declaration":
                continue
            ann = self.annotations(next((c for c in m.children if c.type == "modifiers"), None))
            for verb, path in self._mapped(ann):
                methods.setdefault(_t(m.child_by_field_name("name")), [verb, normalize_path(prefix + path, self.strip)])
        self.out.clients.append({"type": name, "fqn": (self.package + "." if self.package else "") + name,
                                 "base_kind": bk, "base_value": bv, "methods": methods, "file": self.path,
                                 "line": node.start_point[0] + 1})

    # ---- mapping annotations -----------------------------------------------------------------------------------------------
    def _mapping_paths(self, items: Optional[list], default: list) -> list:
        if items is None:
            return default
        node = self._arg(items, "value", "path")
        paths = [self._const_str(v) for v in self._values(node)]
        if node is None:
            return default
        return [p for p in paths if p is not None] or []

    def _mapped(self, ann: dict) -> list:
        """[(verb, path)] declared by the mapping annotations of one method (class prefix not applied)."""
        out = []
        for a, items in ann.items():
            if a in VERB_ANNOTATIONS:
                verbs = [VERB_ANNOTATIONS[a]]
            elif a == "RequestMapping":
                node = self._arg(items, "method")
                verbs = [_t(v).rsplit(".", 1)[-1].upper() for v in self._values(node)] or ["GET"]
            else:
                continue
            for p in self._mapping_paths(items, [""]):
                for v in verbs:
                    out.append((v, p))
        return out

    # ---- methods ----------------------------------------------------------------------------------------------------------
    def visit_method(self, m: Node, scope: list, class_paths: list, in_interface: bool):
        name = "<init>" if m.type == "constructor_declaration" else _t(m.child_by_field_name("name"))
        qual = ".".join(scope + [name])
        mods = self.annotations(next((c for c in m.children if c.type == "modifiers"), None))
        fid = f"{self.repo}:{self.path}::{qual}"
        self.used_ids[fid] = self.used_ids.get(fid, 0) + 1
        if self.used_ids[fid] > 1:                                       # overloads need distinct ids
            fid = f"{fid}~{len(self._params(m))}_{self.used_ids[fid] - 1}"
        body = m.child_by_field_name("body")
        mapped = self._mapped(mods)
        if in_interface and not mapped:
            return                                                       # abstract interface methods are not implementations
        calls = sorted({_t(n.child_by_field_name("name")) for n in _walk(body)
                        if n.type == "method_invocation"}) if body is not None else []
        toks = [_t(n) for n in _walk(m) if n.child_count == 0 and n.type not in ("line_comment", "block_comment")]
        h = hashlib.sha1(" ".join(toks).encode()).hexdigest()[:16]
        line = m.start_point[0] + 1
        if self.is_test_file and set(mods) & TEST_ANNOTATIONS:
            self._add_test(m, fid, name, calls, body, line)
            return
        self.out.functions.append(Function(id=fid, repo=self.repo, file=self.path, qualname=qual, line=line, hash=h, calls=calls))
        if mapped:
            self._add_endpoints(m, fid, qual, line, mapped, class_paths)
        if body is not None:
            self._scan_body(body, fid)

    def _params(self, m: Node) -> list:
        ps = m.child_by_field_name("parameters")
        return [p for p in (ps.named_children if ps is not None else []) if p.type in ("formal_parameter", "spread_parameter")]

    def _add_endpoints(self, m: Node, fid: str, qual: str, line: int, mapped: list, class_paths: list):
        resp = self.unwrap(m.child_by_field_name("type"))
        reqs = []
        for p in self._params(m):
            pm = self.annotations(next((c for c in p.children if c.type == "modifiers"), None))
            if "RequestBody" in pm or "ModelAttribute" in pm:
                t = self.unwrap(p.child_by_field_name("type"))
                if t:
                    reqs.append(t[5:-1] if t.startswith("List[") else t)
        for verb, path in mapped:
            for cp in class_paths:
                full = (cp.rstrip("/") + "/" + path.lstrip("/")) if path else cp
                if not full.startswith("/"):
                    full = "/" + full
                np_ = normalize_path(full, self.strip)
                self.out.endpoints.append(Endpoint(
                    id=f"{self.service}:{verb} {np_}", service=self.service, repo=self.repo, method=verb, path=full,
                    norm_path=np_, handler=qual, handler_id=fid, file=self.path, line=line,
                    response_schema=resp, request_schemas=sorted(set(reqs))))

    # ---- outbound calls ---------------------------------------------------------------------------------------------------------
    def _scan_body(self, body: Node, fid: str):
        for n in _walk(body):
            if n.type != "method_invocation":
                continue
            name = _t(n.child_by_field_name("name"))
            obj = n.child_by_field_name("object")
            args = n.child_by_field_name("arguments")
            a = args.named_children if args is not None else []
            line = n.start_point[0] + 1
            var = self._var_of(obj)
            if var is None and obj is not None and obj.type == "method_invocation" and _t(obj.child_by_field_name("name")) == "build":
                var = self._var_of(obj.child_by_field_name("object"))          # restTemplateBuilder.build().getForObject(...)
            typ = self.types.get(var or "", "")
            if typ in TEMPLATE_TYPES and name in TEMPLATE_METHODS and a:
                method = TEMPLATE_METHODS[name]
                if method is None and len(a) > 1:
                    method = _t(a[1]).rsplit(".", 1)[-1].upper() or None
                self._emit(fid, line, method, a[0], self.bases.get(var))
            elif name == "uri" and a and obj is not None:
                root, verb = self._chain(obj)
                if root and self.types.get(root) in FLUENT_TYPES:
                    self._emit(fid, line, verb, a[0], self.bases.get(root))
            elif var and typ and typ not in BUILTIN_TYPES:
                imported = self.imports.get(typ)
                fqn = imported or (f"{self.package}.{typ}" if self.package else typ)
                rec = {"type": typ, "fqn": fqn, "method": name, "function_id": fid, "file": self.path, "line": line}
                # a type imported from a framework package is the framework's; a type of the project's own package is the
                # project's even when that package is itself under org.springframework (spring-petclinic)
                if not (imported and imported.startswith(FRAMEWORK_PREFIXES)) and rec not in self.out.typed_calls:
                    self.out.typed_calls.append(rec)

    def _chain(self, obj: Node):
        """Root variable and HTTP verb of a fluent call chain such as client.get().uri(...)."""
        verb, cur = None, obj
        while cur is not None and cur.type == "method_invocation":
            nm = _t(cur.child_by_field_name("name"))
            if nm in FLUENT_VERBS:
                verb = FLUENT_VERBS[nm]
            elif nm == "method":
                args = cur.child_by_field_name("arguments")
                if args is not None and args.named_children:
                    verb = _t(args.named_children[0]).rsplit(".", 1)[-1].upper()
            cur = cur.child_by_field_name("object")
        return self._var_of(cur), verb

    def _emit(self, fid: str, line: int, method: Optional[str], url: Node, default_base: Optional[tuple]):
        if url.type == "lambda_expression":                              # uri(b -> b.path("/x").build(id))
            lit = next((_string(x.child_by_field_name("arguments").named_children[0]) for x in _walk(url)
                        if x.type == "method_invocation" and _t(x.child_by_field_name("name")) == "path"
                        and x.child_by_field_name("arguments") is not None and x.child_by_field_name("arguments").named_children), None)
            path, bk, bv, reason = (lit, None, None, None) if lit and lit.startswith("/") else (None, None, None, "uri builder not resolvable")
        else:
            path, bk, bv, reason = self.parse_url(url)
        if bk is None and default_base and path is not None:
            bk, bv = default_base
            if bk == "literal":
                u = urlsplit(bv)
                bk, bv = ("host", u.hostname) if u.hostname else (None, None)
            reason = None
        self.out.callsites.append(CallSite(
            id=f"{self.repo}:{self.path}:{line}", repo=self.repo, service=self.service, file=self.path, line=line,
            function_id=fid, method=method, norm_path=normalize_path(path, self.strip) if path is not None else None,
            base_kind=bk, base_value=bv, reason=reason))

    # ---- tests --------------------------------------------------------------------------------------------------------------------
    def _add_test(self, m: Node, fid: str, name: str, calls: list, body: Optional[Node], line: int):
        http = []
        mockmvc = any("MockMvcRequestBuilders" in i for i in self.import_list)
        for n in _walk(body) if body is not None else []:
            if n.type != "method_invocation":
                continue
            nm, obj = _t(n.child_by_field_name("name")), n.child_by_field_name("object")
            args = n.child_by_field_name("arguments")
            a = args.named_children if args is not None else []
            if nm in FLUENT_VERBS and mockmvc and a and (obj is None or _t(obj).endswith("MockMvcRequestBuilders")):
                p = self.parse_url(a[0])[0]
                if p and p.startswith("/"):
                    http.append([nm.upper(), normalize_path(p, self.strip)])
            elif nm == "uri" and a and obj is not None:
                root, verb = self._chain(obj)
                if root and self.types.get(root) in TEST_CLIENT_TYPES and verb:
                    p = self.parse_url(a[0])[0]
                    if p and p.startswith("/"):
                        http.append([verb, normalize_path(p, self.strip)])
            elif nm in TEMPLATE_METHODS and a and self.types.get(self._var_of(obj) or "") in TEST_CLIENT_TYPES:
                verb = TEMPLATE_METHODS[nm]
                p = self.parse_url(a[0])[0]
                if verb and p and p.startswith("/"):
                    http.append([verb, normalize_path(p, self.strip)])
        used = sorted(self.imports.get(t) or (f"{self.package}.{t}" if self.package else t) for t in self.type_names_used)
        self.out.tests.append(TestCase(
            id=fid, repo=self.repo, file=self.path, name=name, line=line,
            imports=sorted(set(self.import_list) | set(used)), called=calls, http_calls=http))


def extract_file(repo: str, service: str, path: str, text: str, strip_prefixes: tuple = ()) -> RepoExtract:
    """Facts from one Java file, as a partial RepoExtract. Depends only on (repo, service, path, text, strip)."""
    out = RepoExtract(repo=repo, service=service, commit="", extractor_version=EXTRACTOR_VERSION)
    if _is_stub(path):
        out.files_analysed = 1
        return out
    src = text.encode("utf-8")
    tree = Parser(_LANG).parse(src)
    if tree.root_node.has_error:
        bad = next((n for n in _walk(tree.root_node) if n.type == "ERROR" or n.is_missing), None)
        line = bad.start_point[0] + 1 if bad is not None else 0
        out.files_failed.append({"file": path, "error": f"SyntaxError line {line}"})
        return out
    out.files_analysed = 1
    _JavaVisitor(repo, service, path, src, tree.root_node, out, strip_prefixes).run()
    return out


# ---- repository-level resolution (runs when per-file facts are assembled) ---------------------------------------------------------
def resolve_clients(out: RepoExtract, smap: dict, strip_prefixes: tuple = ()) -> None:
    """Turn calls on typed client variables into call sites.
    * A Feign interface declared in this repository gives the call its HTTP method, path and base (R2 downstream).
    * A type from a package the service map assigns to a provider (shared client library) becomes an R1 generated-client call.
    Everything else is not an outbound HTTP call and is dropped. Facts used here are cleared from the extract."""
    clients = {c["type"]: c for c in out.clients}
    packages = smap.get("packages") or {}
    for tc in sorted(out.typed_calls, key=lambda t: (t["file"], t["line"], t["method"])):
        c = clients.get(tc["type"])                          # by simple name: unique within a repository in practice
        if c is not None:
            spec = c["methods"].get(tc["method"])
            if spec is None:
                continue
            out.callsites.append(CallSite(
                id=f"{out.repo}:{tc['file']}:{tc['line']}", repo=out.repo, service=out.service, file=tc["file"], line=tc["line"],
                function_id=tc["function_id"], method=spec[0], norm_path=spec[1], base_kind=c["base_kind"],
                base_value=c["base_value"]))
            continue
        pkg = tc["fqn"].rsplit(".", 1)[0]
        key = max((k for k in packages if pkg == k or pkg.startswith(k + ".")), key=len, default=None)
        if key:
            out.callsites.append(CallSite(
                id=f"{out.repo}:{tc['file']}:{tc['line']}", repo=out.repo, service=out.service, file=tc["file"], line=tc["line"],
                function_id=tc["function_id"], method=None, norm_path=None, base_kind=None, base_value=None,
                client_pkg=key, client_attr=tc["method"]))
    out.typed_calls = []


def keep_reachable_schemas(out: RepoExtract) -> None:
    """Java DTO candidates are kept only when an endpoint exposes them, directly or through the field types of one that does."""
    java = [s for s in out.schemas if s.file.endswith(".java")]
    if not java:
        return
    java_cls = {s.name for s in java}
    by_name = {s.name: s for s in java}

    def merged(name: str, seen: tuple = ()) -> dict:
        s = by_name.get(name)
        if s is None or name in seen:
            return {}
        return {**(merged(s.extends, seen + (name,)) if s.extends else {}), **s.fields}
    for s in java:
        if s.extends:
            s.fields = merged(s.name)
    todo = [n for e in out.endpoints for n in ([e.response_schema] if e.response_schema else []) + list(e.request_schemas)]
    keep: set = set()
    while todo:
        n = re.sub(r"^List\[(.*)\]$", r"\1", todo.pop())
        if n in keep or n not in by_name:
            continue
        keep.add(n)
        for f in by_name[n].fields.values():
            todo.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", f["type"]))
    out.schemas = [s for s in out.schemas if not s.file.endswith(".java") or s.name in keep]
