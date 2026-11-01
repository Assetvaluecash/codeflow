"""Python source adapter: endpoints, schemas, outbound call sites, functions, tests.

Uses the stdlib ``ast`` module only; repository code is parsed, never executed.
Emits normalised entities with source locations. It never creates graph edges.
"""
from __future__ import annotations

import ast
import hashlib
import json
import posixpath
import re
from typing import Optional
from urllib.parse import urlsplit

import yaml

from . import EXTRACTOR_VERSION
from .extract_data import (alembic_migration, django_migration, extract_file as extract_data_file, is_data_file,
                           resolve_data)
from .extract_java import extract_file as extract_java_file, keep_reachable_schemas, module_fqn, resolve_clients
from .extract_orm import function_refs, model_entity
from .scrub import scrub_extract
from .models import (CallSite, Endpoint, Function, RepoExtract, Schema, TestCase,
                     normalize_path)

HTTP_VERBS = {"get", "post", "put", "delete", "patch"}
CLIENT_ROOTS = {"requests", "httpx", "session", "client", "http", "api"}
TEST_CLIENT_NAMES = {"client", "test_client", "tc"}
# packages whose classes are frameworks, not generated API clients
NON_CLIENT_PACKAGES = {"fastapi", "flask", "starlette", "pydantic", "django", "requests", "httpx",
                       "sqlalchemy", "typing", "dataclasses", "collections", "pathlib", "os"}


def module_name(path: str) -> str:
    if path.endswith(".java"):
        return module_fqn(path)
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _is_test_file(path: str) -> bool:
    base = posixpath.basename(path)
    return base.startswith("test_") or base.endswith("_test.py") or "/tests/" in "/" + path


def _const_str(n) -> Optional[str]:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _unparse(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:  # pragma: no cover
        return "?"


class _FileVisitor:
    def __init__(self, repo: str, service: str, path: str, tree: ast.Module, out: RepoExtract,
                 strip_prefixes: tuple):
        self.repo, self.service, self.path, self.tree, self.out = repo, service, path, tree, out
        self.strip = strip_prefixes
        self.consts: dict[str, tuple[str, str]] = {}     # NAME -> (kind, value)
        self.imports: dict[str, str] = {}                # alias -> module (or module.attr)
        self.import_modules: list[str] = []
        self.router_prefix: dict[str, str] = {}
        self.client_vars: dict[str, tuple[str, str]] = {}  # unparsed target -> (pkg, class)
        self.is_test = _is_test_file(path)
        self.is_migration = False        # Alembic / Django migration modules declare schema changes, not data access

    # ---- module-level pre-pass -------------------------------------------------
    def prepass(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name.split(".")[0]] = a.name
                    self.import_modules.append(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.import_modules.append(node.module)
                for a in node.names:
                    self.imports[a.asname or a.name] = f"{node.module}.{a.name}"
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name):
                    c = self._const_ref(node.value)
                    if c:
                        self.consts[t.id] = c
                    if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                            and node.value.func.id == "APIRouter"):
                        for kw in node.value.keywords:
                            if kw.arg == "prefix" and _const_str(kw.value):
                                self.router_prefix[t.id] = _const_str(kw.value)
        # client variables: x = GeneratedClient(...) where the class is imported from a package
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.value, ast.Call):
                f = node.value.func
                if isinstance(f, ast.Name) and f.id in self.imports:
                    full = self.imports[f.id]
                    if "." in full:
                        pkg, cls = full.rsplit(".", 1)
                        if pkg.split(".")[0] not in NON_CLIENT_PACKAGES:
                            self.client_vars[_unparse(node.targets[0])] = (pkg.split(".")[0], cls)

    def _const_ref(self, v) -> Optional[tuple[str, str]]:
        s = _const_str(v)
        if s is not None:
            return ("literal", s)
        if isinstance(v, ast.Call):
            fn = _unparse(v.func)
            if fn in ("os.getenv", "os.environ.get", "environ.get", "getenv") and v.args:
                k = _const_str(v.args[0])
                if k:
                    return ("config_key", k)
        if isinstance(v, ast.Subscript) and _unparse(v.value) in ("os.environ", "environ"):
            k = _const_str(v.slice)
            if k:
                return ("config_key", k)
        if isinstance(v, ast.Attribute) and _unparse(v.value) in ("settings", "config", "cfg"):
            return ("config_key", v.attr)
        return None

    # ---- main walk ---------------------------------------------------------------
    def run(self):
        self.prepass()
        self.visit_body(self.tree.body, [], None)

    def visit_body(self, body, scope, cls):
        for node in body:
            if isinstance(node, ast.ClassDef):
                self.visit_class(node, scope)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit_function(node, scope)
            else:
                # module-level statements may contain call sites (rare); attribute to no function
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        self.maybe_callsite(sub, None)

    def visit_class(self, node: ast.ClassDef, scope):
        if not (self.is_test or self.is_migration):
            ent = model_entity(node, self.repo, self.service, self.path, self.imports, self.out)
            if ent is not None:
                self.out.entities.append(ent)
        bases = {_unparse(b).split(".")[-1] for b in node.bases}
        if "BaseModel" in bases:
            fields = {}
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    fields[st.target.id] = {"type": _unparse(st.annotation), "required": st.value is None}
            self.out.schemas.append(Schema(
                id=f"{self.service}:schema:{node.name}", service=self.service, repo=self.repo,
                name=node.name, fields=fields, file=self.path, line=node.lineno))
        self.visit_body([n for n in node.body if not isinstance(n, ast.AnnAssign)],
                        scope + [node.name], node.name)

    def visit_function(self, node, scope):
        qual = ".".join(scope + [node.name])
        fid = f"{self.repo}:{self.path}::{qual}"
        calls = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    calls.append(f.id)
                elif isinstance(f, ast.Attribute):
                    calls.append(f.attr)
        h = hashlib.sha1(ast.dump(node).encode()).hexdigest()[:16]
        if self.is_test and node.name.startswith("test"):
            self.add_test(node, fid, calls)
        else:
            self.out.functions.append(Function(id=fid, repo=self.repo, file=self.path, qualname=qual,
                                               line=node.lineno, hash=h, calls=sorted(set(calls))))
            self.maybe_endpoint(node, fid, qual)
            if not self.is_migration:
                self.out.data_refs += function_refs(node, fid, self.path, scope, self.out)
            for stmt in node.body:          # body only: decorators are route declarations, not calls
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Call):
                        self.maybe_callsite(sub, fid)
        # nested functions are attributed to the enclosing function (not separately extracted)

    # ---- endpoints ------------------------------------------------------------------
    def maybe_endpoint(self, node, fid, qual):
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            attr, owner = dec.func.attr, dec.func.value
            methods = None
            if attr in HTTP_VERBS:
                methods = [attr.upper()]
            elif attr == "route":
                methods = ["GET"]
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [(_const_str(e) or "").upper() for e in kw.value.elts]
            if not methods or not dec.args:
                continue
            path = _const_str(dec.args[0])
            if path is None:
                continue
            if isinstance(owner, ast.Name) and owner.id in self.router_prefix:
                path = self.router_prefix[owner.id] + path
            resp = None
            for kw in dec.keywords:
                if kw.arg == "response_model":
                    resp = _unparse(kw.value)
            reqs = []
            for a in node.args.args:
                if a.annotation is not None:
                    reqs.append(_unparse(a.annotation))
            for m in methods:
                np_ = normalize_path(path, self.strip)
                self.out.endpoints.append(Endpoint(
                    id=f"{self.service}:{m} {np_}", service=self.service, repo=self.repo, method=m,
                    path=path, norm_path=np_, handler=qual, handler_id=fid, file=self.path,
                    line=node.lineno, response_schema=resp, request_schemas=reqs))

    # ---- outbound call sites ----------------------------------------------------------
    def maybe_callsite(self, call: ast.Call, fid: Optional[str]):
        f = call.func
        if not isinstance(f, ast.Attribute):
            return
        owner = _unparse(f.value)
        # generated client call:  <client_var>.<operation>(...)
        if owner in self.client_vars:
            pkg, _cls = self.client_vars[owner]
            self.out.callsites.append(CallSite(
                id=f"{self.repo}:{self.path}:{call.lineno}", repo=self.repo, service=self.service,
                file=self.path, line=call.lineno, function_id=fid, method=None, norm_path=None,
                base_kind=None, base_value=None, client_pkg=pkg, client_attr=f.attr))
            return
        root = owner.split(".")[0].split("(")[0]
        if f.attr not in HTTP_VERBS and f.attr != "request":
            return
        if root not in CLIENT_ROOTS and not owner.endswith(("client", "session", "http")):
            return
        args = list(call.args)
        method = f.attr.upper() if f.attr != "request" else None
        if f.attr == "request" and args:
            m = _const_str(args[0])
            method = m.upper() if m else None
            args = args[1:]
        if not args:
            return
        path, bk, bv, reason = self.parse_url(args[0])
        self.out.callsites.append(CallSite(
            id=f"{self.repo}:{self.path}:{call.lineno}", repo=self.repo, service=self.service,
            file=self.path, line=call.lineno, function_id=fid, method=method,
            norm_path=normalize_path(path, self.strip) if path is not None else None,
            base_kind=bk, base_value=bv, reason=reason))

    def _base_of(self, n):
        if isinstance(n, ast.Name) and n.id in self.consts:
            return self.consts[n.id]
        if isinstance(n, ast.Attribute) and _unparse(n.value) in ("settings", "config", "cfg"):
            return ("config_key", n.attr)
        c = self._const_ref(n)
        return c

    def parse_url(self, n):
        """-> (path_template|None, base_kind, base_value, reason)"""
        # resolve a bare name to a module constant
        if isinstance(n, ast.Name) and n.id in self.consts and self.consts[n.id][0] == "literal":
            n = ast.Constant(self.consts[n.id][1])
        s = _const_str(n)
        if s is not None:
            if s.startswith(("http://", "https://")):
                u = urlsplit(s)
                return (u.path or "/", "host", u.hostname, None)
            return (s, None, None, "base URL not present in call")
        if isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                if isinstance(v, ast.Constant):
                    parts.append(("s", v.value))
                else:
                    parts.append(("v", v.value))  # FormattedValue.value
            return self._from_parts(parts)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            flat = []

            def walk(x):
                if isinstance(x, ast.BinOp) and isinstance(x.op, ast.Add):
                    walk(x.left); walk(x.right)
                elif _const_str(x) is not None:
                    flat.append(("s", _const_str(x)))
                else:
                    flat.append(("v", x))
            walk(n)
            return self._from_parts(flat)
        return (None, None, None, f"URL expression not statically resolvable: {_unparse(n)[:60]}")

    def _from_parts(self, parts):
        base_kind = base_val = None
        reason = None
        path = ""
        i = 0
        if parts and parts[0][0] == "v":
            c = self._base_of(parts[0][1])
            if c and c[0] == "literal" and c[1].startswith(("http://", "https://")):
                u = urlsplit(c[1]); base_kind, base_val = "host", u.hostname; path += u.path
            elif c:
                base_kind, base_val = c
            else:
                reason = f"base expression not resolvable: {_unparse(parts[0][1])[:40]}"
                return (None, None, None, reason)
            i = 1
        elif parts and parts[0][0] == "s" and parts[0][1].startswith(("http://", "https://")):
            # literal URL start, possibly with variables later
            lit = parts[0][1]
            u = urlsplit(lit)
            base_kind, base_val = "host", u.hostname
            path += lit[len(f"{u.scheme}://{u.netloc}"):]
            i = 1
        for kind, v in parts[i:]:
            path += v if kind == "s" else "{}"
        if not path.startswith("/"):
            return (None, base_kind, base_val, "path does not start with a literal segment")
        return (path, base_kind, base_val, None)

    # ---- tests ----------------------------------------------------------------------------
    def add_test(self, node, fid, calls):
        http = []
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in HTTP_VERBS and sub.args):
                owner = _unparse(sub.func.value)
                if owner in TEST_CLIENT_NAMES or owner.endswith("client"):
                    s = _const_str(sub.args[0])
                    if s is None and isinstance(sub.args[0], ast.JoinedStr):
                        p, _, _, _ = self.parse_url(sub.args[0])
                        s = p
                    if s and s.startswith("/"):
                        http.append([sub.func.attr.upper(), normalize_path(s, self.strip)])
        self.out.tests.append(TestCase(
            id=fid, repo=self.repo, file=self.path, name=node.name, line=node.lineno,
            imports=sorted(set(self.import_modules)), called=sorted(set(calls)), http_calls=http))


BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".jar", ".class", ".exe", ".dll",
              ".so", ".woff", ".woff2", ".ttf", ".pyc", ".bin", ".lock", ".svg")
TEXT_EXT = (".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".gitignore", ".xml", ".properties", ".gradle", ".sql")


APP_CONFIG_NAMES = ("application.yml", "application.yaml", "bootstrap.yml", "bootstrap.yaml", "application.properties",
                    "bootstrap.properties")
_SPRING_NAME = re.compile(r"^\s*spring\.application\.name\s*[=:]\s*([^\s#]+)", re.M)


def is_service_config(path: str) -> bool:
    """Spring configuration files that can name the service (main resources only, never test resources)."""
    return path.rsplit("/", 1)[-1] in APP_CONFIG_NAMES and "/src/test/" not in "/" + path


def spring_application_name(files: dict) -> Optional[str]:
    """spring.application.name: the name a Spring service registers under and is addressed by from other services."""
    for path in sorted((p for p in files if is_service_config(p)), key=lambda p: (len(p), p)):
        text = files[path]
        name = None
        if path.endswith((".yml", ".yaml")):
            try:
                for doc in yaml.safe_load_all(text):
                    if not isinstance(doc, dict):
                        continue
                    name = ((doc.get("spring") or {}).get("application") or {}).get("name") if isinstance(doc.get("spring"), dict) else None
                    name = name or doc.get("spring.application.name")
                    if name:
                        break
            except yaml.YAMLError:
                continue
        else:
            m = _SPRING_NAME.search(text)
            name = m.group(1) if m else None
        if isinstance(name, str) and name.strip() and "${" not in name:
            return name.strip()
    return None


def _flatten(doc, prefix: str, out: dict) -> None:
    if isinstance(doc, dict):
        for k, v in doc.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif doc is not None:
        out.setdefault(prefix, doc)


_SHARED = re.compile(r"(?:^|/)(?:shared|config-repo)/([A-Za-z0-9][A-Za-z0-9._-]*)\.(?:yml|yaml|properties)$")
_CONTEXT_KEYS = ("server.servlet.context-path", "server.context-path", "server.servlet.contextPath")


def is_shared_config(path: str) -> bool:
    """A Spring Cloud Config repository's per-service file (shared/<service>.yml): it can set another service's context path."""
    return bool(_SHARED.search(path)) and "/src/test/" not in "/" + path


def _context_of(props: dict) -> Optional[str]:
    for k in _CONTEXT_KEYS:
        v = props.get(k)
        if isinstance(v, str) and re.fullmatch(r"/[A-Za-z0-9._~/-]*", v.strip()) and v.strip() != "/":
            return v.strip().rstrip("/")
    return None


def context_path(files: dict) -> Optional[str]:
    """server.servlet.context-path of this service's own main configuration: every route is served under it."""
    return _context_of(spring_properties({p: t for p, t in files.items() if is_service_config(p)}))


def external_context_paths(files: dict) -> dict:
    """service -> context path set for it in a configuration repository (Spring Cloud Config 'shared' files)."""
    out = {}
    for p, text in files.items():
        m = _SHARED.search(p)
        if not m or not is_shared_config(p):
            continue
        cp = _context_of(_props_of(p, text))
        if cp:
            out[m.group(1)] = cp
    return out


def _props_of(path: str, text: str) -> dict:
    props: dict = {}
    if path.endswith((".yml", ".yaml")):
        try:
            for doc in yaml.safe_load_all(text):
                _flatten(doc, "", props)
        except yaml.YAMLError:
            return {}
    else:
        for line in text.splitlines():
            if "=" in line and not line.lstrip().startswith(("#", "!")):
                k, v = line.split("=", 1)
                props.setdefault(k.strip(), v.strip())
    return props


def spring_properties(files: dict) -> dict:
    """Flattened Spring Boot main configuration (application/bootstrap .yml/.properties); the shortest path wins."""
    props: dict = {}
    for path in sorted((p for p in files if is_service_config(p)), key=lambda p: (len(p), p)):
        text = files[path]
        if path.endswith((".yml", ".yaml")):
            try:
                for doc in yaml.safe_load_all(text):
                    _flatten(doc, "", props)
            except yaml.YAMLError:
                continue
        else:
            for line in text.splitlines():
                m = re.match(r"^\s*([\w.\-\[\]]+)\s*[=:]\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith(("#", "!")):
                    props.setdefault(m.group(1), m.group(2))
    return props


_EMBEDDED_DB = re.compile(r"^jdbc:(h2|hsqldb|derby|sqlite)\b", re.I)
_JDBC = re.compile(r"^jdbc:(\w+):(?:thin:)?@?(?://)?([^/;:?]+)(?::(\d+))?(?:[/:;](.*))?$", re.I)


def jdbc_database_id(url: str) -> Optional[str]:
    """Identity of the database a JDBC URL names (driver://host:port/name), or None for embedded / unresolvable URLs."""
    url = str(url).strip()
    if not url.startswith("jdbc:") or "${" in url or _EMBEDDED_DB.match(url):
        return None
    m = _JDBC.match(url)
    if not m:
        return None
    driver, host, port, rest = m.group(1).lower(), m.group(2).lower(), m.group(3) or "", m.group(4) or ""
    dbm = re.search(r"databasename=([^;&]+)", rest, re.I)
    name = dbm.group(1) if dbm else re.split(r"[?;&]", rest)[0].lstrip("/")
    return f"{driver}://{host}{':' + port if port else ''}/{name.lower()}" if name else None


def read_service_map(files: dict) -> dict:
    """Service identity + service map (R4) come from the owner-maintained tia-services.yaml; a Spring service without one
    is named by its spring.application.name. The database a service maps tables in is the declared ``database:`` or, for a
    Spring service, the one named by a literal spring.datasource.url."""
    smap: dict = {}
    for name in ("tia-services.yaml", "tia-services.yml"):
        if name in files:
            try:
                smap = yaml.safe_load(files[name]) or {}
            except yaml.YAMLError:
                smap = {}
            if not isinstance(smap, dict):
                smap = {}
    if not smap.get("service"):
        spring = spring_application_name(files)
        if spring:
            smap = {**smap, "service": spring}
    props = spring_properties(files)
    if smap.get("database"):
        smap = {**smap, "database": str(smap["database"]).strip().lower(), "database_source": "declared"}
    else:
        db = jdbc_database_id(props["spring.datasource.url"]) if "spring.datasource.url" in props else None
        if db:
            smap = {**smap, "database": db, "database_source": "datasource_url"}
    naming = [str(v) for k, v in props.items() if k.startswith("spring.jpa.hibernate.naming")]
    if any("SpringPhysicalNamingStrategy" not in v and "CamelCaseToUnderscoresNamingStrategy" not in v
           and "SpringImplicitNamingStrategy" not in v and "ImplicitNamingStrategyJpaCompliantImpl" not in v for v in naming):
        smap = {**smap, "jpa_custom_naming": True}
    return smap


SOURCE_EXT = (".py", ".java")


def is_extractable(path: str) -> bool:
    """Files whose facts are extracted per file and cached: Python and Java source, SQL and Liquibase migrations."""
    return path.endswith(SOURCE_EXT) or is_data_file(path)


def extract_source(repo: str, service: str, path: str, text: str, strip_prefixes: tuple = ()) -> RepoExtract:
    """Facts from one source or migration file in any supported language."""
    if path.endswith(".java"):
        part = extract_java_file(repo, service, path, text, strip_prefixes)
    elif path.endswith(".py"):
        part = extract_file(repo, service, path, text, strip_prefixes)
    else:
        part = extract_data_file(repo, service, path, text)
    part.secrets_withheld = scrub_extract(part)          # secrets in source never reach the cache, the graph or a report
    return part


def extract_file(repo: str, service: str, path: str, text: str, strip_prefixes: tuple = ()) -> RepoExtract:
    """Facts from one Python file, as a partial RepoExtract. Depends only on (repo, service, path, text, strip)."""
    out = RepoExtract(repo=repo, service=service, commit="", extractor_version=EXTRACTOR_VERSION)
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as e:
        out.files_failed.append({"file": path, "error": f"SyntaxError line {e.lineno}"})
        return out
    out.files_analysed = 1
    tool = migration_tool(tree)
    v = _FileVisitor(repo, service, path, tree, out, strip_prefixes)
    v.is_migration = tool is not None
    v.run()
    if tool:
        mig = (alembic_migration if tool == "alembic" else django_migration)(repo, service, path, tree)
        if mig is not None:
            out.migrations.append(mig)
    return out


def migration_tool(tree: ast.Module) -> Optional[str]:
    """'alembic' / 'django' when the module imports the migration framework, else None."""
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            names = [a.name for a in n.names]
            if n.module.startswith("alembic") and ("op" in names or n.module.startswith("alembic.op")):
                return "alembic"
            if n.module == "django.db" and "migrations" in names or n.module.startswith("django.db.migrations"):
                return "django"
        elif isinstance(n, ast.Import) and any(a.name.startswith("alembic") for a in n.names):
            return "alembic"
    return None


def assemble(repo: str, commit: str, tree_paths: list, smap: dict, service: str, per_file: dict,
             small_files: dict, strip: tuple = ()) -> RepoExtract:
    """Merge per-file facts into the repository extract. The same function serves full and incremental extraction,
    so an incremental result equals a full rescan by construction (AC-021).

    per_file:    path -> partial RepoExtract for every .py file (parsed or failed)
    small_files: path -> text for the files read in full anyway (contracts/*.json)
    """
    out = RepoExtract(repo=repo, service=service, commit=commit, extractor_version=EXTRACTOR_VERSION, service_map=smap,
                      strip_prefixes=list(strip))
    for path in sorted(tree_paths):
        if is_extractable(path):
            part = per_file[path]
            out.files_analysed += part.files_analysed
            out.files_failed += part.files_failed
            out.endpoints += part.endpoints
            out.schemas += part.schemas
            out.callsites += part.callsites
            out.functions += part.functions
            out.tests += part.tests
            out.clients += part.clients
            out.typed_calls += part.typed_calls
            out.unsupported_clients += part.unsupported_clients
            out.entities += part.entities
            out.migrations += part.migrations
            out.data_unknowns += part.data_unknowns
            out.repositories += part.repositories
            out.data_refs += part.data_refs
            out.secrets_withheld += part.secrets_withheld
        elif path.startswith("contracts/") and path.endswith(".json"):
            try:
                c = json.loads(small_files[path])
                c["_file"] = path
                out.contracts.append(c)
            except (json.JSONDecodeError, KeyError, TypeError):
                out.files_failed.append({"file": path, "error": "invalid JSON contract"})
        elif not path.endswith(TEXT_EXT + BINARY_EXT):
            out.non_python_files += 1
    resolve_data(out, smap)                    # ORM mappings, repositories and migrations need facts from several files
    resolve_clients(out, smap)                 # Java: Feign / shared-client calls need declarations from other files
    keep_reachable_schemas(out)
    # keep only declared schemas as endpoint request schemas
    names = {s.name for s in out.schemas}
    for e in out.endpoints:
        e.request_schemas = sorted({r for r in e.request_schemas if r in names})
        if e.response_schema:
            # response_model=List[X] / list[X] -> X
            for nm in names:
                if nm == e.response_schema or f"[{nm}]" in e.response_schema:
                    e.response_schema = nm
                    break
            else:
                e.response_schema = e.response_schema if e.response_schema in names else None
    # a Spring service with server.servlet.context-path serves every route under it: that is the path its consumers call
    out.context_path = context_path(small_files)
    if out.context_path:
        for e in out.endpoints:
            if e.file.endswith(".java"):
                e.path = out.context_path + ("" if e.path.startswith("/") else "/") + e.path
                e.norm_path = normalize_path(e.path, strip)
                e.id = f"{e.service}:{e.method} {e.norm_path}"
        for t in out.tests:                                   # MockMvc paths are relative to the servlet: put them under the same context
            if t.file.endswith(".java"):
                t.http_calls = [[v, p if p.startswith(normalize_path(out.context_path, strip)) else normalize_path(out.context_path + p, strip)]
                                for v, p in t.http_calls]
    out.external_context_paths = external_context_paths(small_files)
    out.endpoints.sort(key=lambda e: e.id)
    out.schemas.sort(key=lambda s: s.id)
    out.callsites.sort(key=lambda c: c.id)
    out.functions.sort(key=lambda f: f.id)
    out.tests.sort(key=lambda t: t.id)
    return out


def extract_repo(repo: str, commit: str, files: dict[str, str], strip_prefixes: tuple = ()) -> RepoExtract:
    """Extract all entities for one repository at one revision (full rescan)."""
    smap = read_service_map(files)
    service = smap.get("service", repo)
    per_file = {p: extract_source(repo, service, p, files[p], strip_prefixes) for p in files if is_extractable(p)}
    return assemble(repo, commit, list(files), smap, service, per_file,
                    {p: t for p, t in files.items() if (p.startswith("contracts/") and p.endswith(".json")) or is_service_config(p) or is_shared_config(p)},
                    strip_prefixes)
