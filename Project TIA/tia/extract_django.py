"""Django and Django REST framework routes (PRD FR-004: "Python: FastAPI / Flask / Django REST (routes)").

Django declares routes away from the code that handles them: `urlpatterns` in a urls module lists `path()` / `re_path()` / `url()`
entries that point at views in other modules, `include()` nests whole url modules under a prefix, and a DRF router turns one
`router.register("orders", OrderViewSet)` into a collection route and a detail route whose HTTP methods depend on the viewset's
class (ModelViewSet, mixins, generic views) and its `@action`s. So extraction runs in two steps, like ORM mappings:

  file_facts()   per file and cached with it: the url modules, routers, view functions and view classes the file declares, with every
                 name resolved through the file's own imports (relative imports included) to a dotted module path;
  resolve()      at assembly, with every file's facts: walk the url tree from the root url module (ROOT_URLCONF, or every url module
                 nothing includes), join the prefixes, expand routers, and emit one Endpoint per route and method.

A route whose view, include or regular expression cannot be resolved becomes a route unknown in the report, never a silent gap.
Inherited viewset actions (ModelViewSet.list ...) have no function in the repository; they are handled by a function that stands for
the view class's own configuration (queryset, serializer_class, permission_classes), so changing that configuration reaches them.
"""
from __future__ import annotations

import ast
import hashlib
import posixpath
import re
from typing import Optional

from .models import Endpoint, Function, Schema, normalize_path

DJANGO_ROOTS = ("django", "rest_framework")
URL_FUNCS = {"path", "re_path", "url"}
ROUTERS = {"DefaultRouter", "SimpleRouter", "ExtendedSimpleRouter", "NestedSimpleRouter"}
ACTION_ROUTE = {"list": ("GET", False), "create": ("POST", False), "retrieve": ("GET", True), "update": ("PUT", True),
                "partial_update": ("PATCH", True), "destroy": ("DELETE", True)}
VIEWSET_ACTIONS = {"ModelViewSet": ["list", "create", "retrieve", "update", "partial_update", "destroy"],
                   "ReadOnlyModelViewSet": ["list", "retrieve"], "GenericViewSet": [], "ViewSet": [], "ViewSetMixin": [],
                   "ListModelMixin": ["list"], "CreateModelMixin": ["create"], "RetrieveModelMixin": ["retrieve"],
                   "UpdateModelMixin": ["update", "partial_update"], "DestroyModelMixin": ["destroy"]}
GENERIC_METHODS = {"ListAPIView": ["GET"], "CreateAPIView": ["POST"], "RetrieveAPIView": ["GET"], "DestroyAPIView": ["DELETE"],
                   "UpdateAPIView": ["PUT", "PATCH"], "ListCreateAPIView": ["GET", "POST"], "RetrieveUpdateAPIView": ["GET", "PUT", "PATCH"],
                   "RetrieveDestroyAPIView": ["GET", "DELETE"], "RetrieveUpdateDestroyAPIView": ["GET", "PUT", "PATCH", "DELETE"]}
PLAIN_VIEWS = {"APIView", "View", "GenericAPIView", "TemplateView", "RedirectView", "FormView"}
HTTP = ("get", "post", "put", "patch", "delete")
OPEN_PERMISSIONS = {"AllowAny"}


def module_name(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    p = p[: -len("/__init__")] if p.endswith("/__init__") else p
    return p.replace("/", ".")


def _s(n) -> Optional[str]:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _u(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:  # pragma: no cover
        return "?"


def _uses_django(tree: ast.Module) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in DJANGO_ROOTS:
            return True
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] in DJANGO_ROOTS for a in n.names):
            return True
    return False


class _Names:
    """Resolves a name used in one file to a dotted path, through that file's imports (absolute and relative)."""

    def __init__(self, tree: ast.Module, mod: str, is_pkg: bool):
        pkg = mod if is_pkg else mod.rpartition(".")[0]
        self.alias: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[: len(parts) - (n.level - 1)] if n.level > 1 else parts
                    base = ".".join([*parts, *([n.module] if n.module else [])])
                for a in n.names:
                    self.alias[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
        self.mod = mod

    def ref(self, n) -> Optional[str]:
        """`views.order_detail` -> 'app.views.order_detail'; a name defined in this file -> '<this module>.<name>'."""
        dotted = _u(n)
        if not re.fullmatch(r"[A-Za-z_][\w.]*", dotted):
            return None
        head, _, rest = dotted.partition(".")
        if head in self.alias:
            return self.alias[head] + ("." + rest if rest else "")
        return f"{self.mod}.{dotted}"


def _methods_checked(fn) -> list:
    """HTTP methods a plain Django view compares request.method with."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare) and _u(n.left).endswith("request.method"):
            for c in n.comparators:
                vals = c.elts if isinstance(c, (ast.Tuple, ast.List, ast.Set)) else [c]
                out.update(v.upper() for v in (_s(x) for x in vals) if v and v.lower() in HTTP)
    return sorted(out)


def _str_list(n) -> list:
    return [v for v in (_s(x) for x in n.elts) if v] if isinstance(n, (ast.List, ast.Tuple)) else []


def _names_list(n) -> list:
    return [_u(x).split(".")[-1] for x in n.elts] if isinstance(n, (ast.List, ast.Tuple)) else []


def _pattern(call: ast.Call, names: _Names, lists: set) -> Optional[dict]:
    fname = _u(call.func).split(".")[-1]
    if fname not in URL_FUNCS or len(call.args) < 2:
        return None
    route = _s(call.args[0])
    p = {"route": route, "regex": fname != "path", "line": call.lineno}
    if route is None:
        p["unresolved"] = f"the route of {fname}() is not a string literal"
        return p
    v = call.args[1]
    if isinstance(v, ast.Call) and _u(v.func).split(".")[-1] == "include" and v.args:
        a = v.args[0]
        if isinstance(a, ast.Tuple) and a.elts:
            a = a.elts[0]
        if _s(a):
            p["include"] = _s(a)
        elif isinstance(a, ast.Attribute) and a.attr == "urls" and isinstance(a.value, ast.Name):
            p["include_router"] = a.value.id
        elif isinstance(a, ast.Name) and a.id in lists:
            p["include_list"] = a.id
        else:
            p["unresolved"] = f"include({_u(a)[:80]}) cannot be followed"
    elif isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "as_view":
        p["view"] = names.ref(v.func.value)
        if v.args and isinstance(v.args[0], ast.Dict):
            p["actions"] = {(_s(k) or "").upper(): _s(val) for k, val in zip(v.args[0].keys, v.args[0].values) if _s(k) and _s(val)}
    else:
        p["view"] = names.ref(v)
    if "view" in p and not p["view"]:
        p["unresolved"] = f"the view {_u(v)[:80]} cannot be resolved"
    return p


SERIALIZER_BASES = {"Serializer", "ModelSerializer", "HyperlinkedModelSerializer", "ListSerializer"}
FIELD_TYPES = {"CharField": "str", "EmailField": "str", "SlugField": "str", "URLField": "str", "UUIDField": "UUID", "RegexField": "str",
               "IPAddressField": "str", "ChoiceField": "str", "IntegerField": "int", "FloatField": "float", "DecimalField": "Decimal",
               "BooleanField": "bool", "NullBooleanField": "bool", "DateTimeField": "datetime", "DateField": "date", "TimeField": "time",
               "DurationField": "timedelta", "JSONField": "dict", "DictField": "dict", "ListField": "list", "MultipleChoiceField": "list",
               "FileField": "file", "ImageField": "file", "PrimaryKeyRelatedField": "int", "SlugRelatedField": "str",
               "StringRelatedField": "str", "HyperlinkedRelatedField": "str", "HyperlinkedIdentityField": "str",
               "SerializerMethodField": "any", "ReadOnlyField": "any", "HiddenField": "any"}


def _serializer(n: ast.ClassDef, repo: str, service: str, path: str) -> Optional[tuple]:
    """A DRF serializer as a schema: declared fields with their types and whether a request must send them; Meta.fields names that
    come from the model are listed with type 'model'."""
    bases = [_u(b).split(".")[-1] for b in n.bases]
    if not any(b in SERIALIZER_BASES or b.endswith("Serializer") for b in bases):
        return None
    fields = {}
    for st in n.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Call):
            kind = _u(st.value.func).split(".")[-1]
            if kind not in FIELD_TYPES and not kind.endswith("Serializer"):
                continue
            kw = {k.arg: k.value for k in st.value.keywords if k.arg}
            flag = lambda k: isinstance(kw.get(k), ast.Constant) and kw[k].value is True
            required = (kw["required"].value is True) if isinstance(kw.get("required"), ast.Constant) else not (flag("read_only") or "default" in kw)
            f = {"type": FIELD_TYPES.get(kind, kind), "required": bool(required)}
            if flag("many") or kind in ("ListField", "MultipleChoiceField"):
                f["multi"] = True
            if flag("allow_null"):
                f["nullable"] = True
            if flag("read_only"):
                f["read_only"] = True
            cons = {k: v.value for k, v in kw.items() if k in ("max_length", "min_length", "max_value", "min_value") and isinstance(v, ast.Constant)}
            if cons:
                f["constraints"] = cons
            fields[st.targets[0].id] = f
        elif isinstance(st, ast.ClassDef) and st.name == "Meta":
            for m in st.body:
                if isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "fields" for t in m.targets):
                    for name in _str_list(m.value):
                        fields.setdefault(name, {"type": "model", "required": False})
    project_base = next((b for b in bases if b not in SERIALIZER_BASES and b.endswith("Serializer")), None)
    schema = Schema(id=f"{service}:schema:{n.name}", service=service, repo=repo, name=n.name, fields=fields, file=path, line=n.lineno,
                    extends=project_base)
    return schema, {"kind": "serializer", "name": n.name, "extends": project_base, "file": path, "line": n.lineno}


def file_facts(repo: str, path: str, tree: ast.Module, is_test: bool, service: str = "") -> tuple[list, list, list]:
    """-> (facts, functions standing for view-class configuration, serializer schemas). Empty for a file that does not use Django."""
    if is_test or not _uses_django(tree):
        return [], [], []
    mod = module_name(path)
    names = _Names(tree, mod, path.endswith("__init__.py"))
    facts, fns, schemas = [], [], []
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            got = _serializer(n, repo, service or repo, path)
            if got:
                schemas.append(got[0])
                facts.append(got[1])
    routers, lists, patterns = {}, {}, []
    list_names = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
                  and isinstance(n.value, (ast.List, ast.Tuple))}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            tgt, val = n.targets[0].id, n.value
            if isinstance(val, ast.Call) and _u(val.func).split(".")[-1] in ROUTERS:
                trailing = next((k.value.value for k in val.keywords if k.arg == "trailing_slash" and isinstance(k.value, ast.Constant)), True)
                routers[tgt] = {"trailing_slash": bool(trailing), "registrations": []}
            elif tgt == "ROOT_URLCONF" and _s(val):
                facts.append({"kind": "settings", "root_urlconf": _s(val), "file": path})
            elif isinstance(val, (ast.List, ast.Tuple)):
                pats = [p for p in (_pattern(e, names, list_names) for e in val.elts if isinstance(e, ast.Call)) if p]
                if tgt == "urlpatterns":
                    patterns += pats
                else:
                    lists[tgt] = pats
            elif tgt == "urlpatterns" and isinstance(val, ast.Attribute) and val.attr == "urls" and isinstance(val.value, ast.Name):
                patterns.append({"route": "", "regex": False, "include_router": val.value.id, "line": n.lineno})
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == "urlpatterns":
            val = n.value
            if isinstance(val, (ast.List, ast.Tuple)):
                patterns += [p for p in (_pattern(e, names, list_names) for e in val.elts if isinstance(e, ast.Call)) if p]
            elif isinstance(val, ast.Attribute) and val.attr == "urls" and isinstance(val.value, ast.Name):
                patterns.append({"route": "", "regex": False, "include_router": val.value.id, "line": n.lineno})
    for n in ast.walk(tree):                          # router.register("orders", OrderViewSet, basename=...)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "register"
                and isinstance(n.func.value, ast.Name) and n.func.value.id in routers and len(n.args) >= 2):
            routers[n.func.value.id]["registrations"].append(
                {"prefix": _s(n.args[0]), "view": names.ref(n.args[1]), "line": n.lineno, "expr": _u(n.args[1])[:80]})
    if patterns or routers or lists:
        facts.append({"kind": "urlconf", "module": mod, "file": path, "patterns": patterns, "lists": lists, "routers": routers})
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decs = {(_u(d.func) if isinstance(d, ast.Call) else _u(d)).split(".")[-1]: d for d in n.decorator_list}
            fid = f"{repo}:{path}::{n.name}"
            if "api_view" in decs:
                d = decs["api_view"]
                methods = [m.upper() for m in _str_list(d.args[0])] if isinstance(d, ast.Call) and d.args else ["GET"]
                perms = _names_list(decs["permission_classes"].args[0]) if isinstance(decs.get("permission_classes"), ast.Call) and decs["permission_classes"].args else []
                facts.append({"kind": "view_fn", "ref": f"{mod}.{n.name}", "name": n.name, "function_id": fid, "file": path, "line": n.lineno,
                              "methods": methods or ["GET"], "inferred": False, "auth": [p for p in perms if p not in OPEN_PERMISSIONS]})
            elif n.args.args and n.args.args[0].arg == "request":
                # a plain Django view answers every method unless a decorator restricts it; checking request.method only adds branches
                restricted = None
                if isinstance(decs.get("require_http_methods"), ast.Call) and decs["require_http_methods"].args:
                    restricted = [m.upper() for m in _str_list(decs["require_http_methods"].args[0])]
                for name, ms in (("require_GET", ["GET"]), ("require_POST", ["POST"]), ("require_safe", ["GET", "HEAD"])):
                    if name in decs:
                        restricted = ms
                methods = restricted or sorted({"GET", *_methods_checked(n)})
                auth = [k for k in decs if k in ("login_required", "permission_required", "staff_member_required")]
                facts.append({"kind": "view_fn", "ref": f"{mod}.{n.name}", "name": n.name, "function_id": fid, "file": path, "line": n.lineno,
                              "methods": [m for m in methods if m != "HEAD"], "inferred": restricted is None, "auth": auth})
        elif isinstance(n, ast.ClassDef):
            bases = [_u(b).split(".")[-1] for b in n.bases]
            defines_http = any(isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and (st.name in HTTP or st.name in ACTION_ROUTE)
                               for st in n.body)
            if not (defines_http or any(b in VIEWSET_ACTIONS or b in GENERIC_METHODS or b in PLAIN_VIEWS
                                        or b.endswith(("View", "ViewSet", "APIView", "Mixin")) for b in bases)):
                continue                                               # models, forms, serializers: not views
            methods, extra, perms, serializer, lookup = {}, [], [], None, "pk"
            for st in n.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[st.name] = {"function_id": f"{repo}:{path}::{n.name}.{st.name}", "line": st.lineno}
                    for d in st.decorator_list:
                        if isinstance(d, ast.Call) and _u(d.func).split(".")[-1] == "action":
                            kw = {k.arg: k.value for k in d.keywords}
                            detail = isinstance(kw.get("detail"), ast.Constant) and bool(kw["detail"].value)
                            ms = [m.upper() for m in _str_list(kw["methods"])] if "methods" in kw else ["GET"]
                            extra.append({"name": st.name, "detail": detail, "methods": ms, "url_path": _s(kw.get("url_path")) or st.name,
                                          "function_id": f"{repo}:{path}::{n.name}.{st.name}", "line": st.lineno})
                elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    k = st.targets[0].id
                    if k == "permission_classes":
                        perms = _names_list(st.value)
                    elif k == "serializer_class":
                        serializer = _u(st.value).split(".")[-1]
                    elif k in ("lookup_url_kwarg", "lookup_field") and _s(st.value) and (k == "lookup_url_kwarg" or lookup == "pk"):
                        lookup = _s(st.value)
            config = [st for st in n.body if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))]
            class_fid = f"{repo}:{path}::{n.name}"
            facts.append({"kind": "view_class", "ref": f"{mod}.{n.name}", "name": n.name, "bases": bases, "file": path, "line": n.lineno,
                          "methods": methods, "extra_actions": extra, "auth": [p for p in perms if p not in OPEN_PERMISSIONS],
                          "serializer": serializer, "lookup": lookup, "class_function_id": class_fid})
            h = hashlib.sha1(("|".join(bases) + ast.dump(ast.Module(body=config, type_ignores=[]))).encode()).hexdigest()[:16]
            fns.append(Function(id=class_fid, repo=repo, file=path, qualname=n.name, line=n.lineno, hash=h, calls=[],
                                body_hash=h, end_line=getattr(n, "end_lineno", None)))
    return facts, fns, schemas


# ---- assembly -----------------------------------------------------------------------------------------------------------------------------
_REGEX_GROUP = re.compile(r"\(\?P<(\w+)>[^)]*\)")


def _route(p: dict) -> Optional[str]:
    """A route as a path template: path() converters stay (<int:pk>); a regular expression is turned into one when it is simple."""
    r = p["route"]
    if not p.get("regex"):
        return r
    r = _REGEX_GROUP.sub(lambda m: "{" + m.group(1) + "}", r.lstrip("^").rstrip("$"))
    r = r.replace("\\.", ".").replace("\\-", "-").replace("\\/", "/")
    return None if re.search(r"[\\()\[\]*+?|]", r) else r


def _join(a: str, b: str) -> str:
    return a + b if not a or a.endswith("/") or not b else a + "/" + b


def resolve(out, strip: tuple = ()) -> None:
    """Turn every file's Django facts into endpoints of the repository extract (out.endpoints) and route unknowns."""
    facts = out.django_facts
    if not facts:
        return
    # serializer inheritance: a subclass carries its project base serializer's fields (its own win)
    sers = {f["name"]: f for f in facts if f["kind"] == "serializer"}
    by_schema = {s.name: s for s in out.schemas if s.name in sers}

    def ser_fields(name: str, seen: tuple = ()) -> dict:
        s = by_schema.get(name)
        if s is None or name in seen:
            return {}
        base = ser_fields(s.extends, seen + (name,)) if s.extends else {}
        # a name listed only in Meta.fields (type 'model') does not override a field the base serializer declares
        return {**base, **{k: v for k, v in s.fields.items() if not (v.get("type") == "model" and k in base)}}
    for s in by_schema.values():
        if s.extends:
            s.fields = ser_fields(s.name)
    urlconfs = {f["module"]: f for f in facts if f["kind"] == "urlconf"}
    views = {f["ref"]: f for f in facts if f["kind"] in ("view_fn", "view_class")}
    classes_by_name: dict = {}
    for f in facts:
        if f["kind"] == "view_class":
            classes_by_name.setdefault(f["name"], []).append(f)

    def find_module(target: str) -> Optional[str]:
        if target in urlconfs:
            return target
        hits = [m for m in urlconfs if m.endswith("." + target)]
        return hits[0] if len(hits) == 1 else None

    def find_view(ref: str) -> Optional[dict]:
        """Exact dotted path, else the one view whose path ends with it (an import that omits a source root such as src.)."""
        if ref in views:
            return views[ref]
        hits = [r for r in views if r.endswith("." + ref)]
        return views[hits[0]] if len(hits) == 1 else None

    def lineage(cls: dict, seen=None) -> list:
        """The class's framework bases, through the repository's own view classes (class MyBase(ModelViewSet); class X(MyBase))."""
        seen = seen or set()
        out_ = []
        for b in cls["bases"]:
            if b in VIEWSET_ACTIONS or b in GENERIC_METHODS or b in PLAIN_VIEWS:
                out_.append(b)
            elif b in classes_by_name and b not in seen and len(classes_by_name[b]) == 1:
                seen.add(b)
                out_ += lineage(classes_by_name[b][0], seen)
        return out_

    def inherited(cls: dict, attr: str, seen=None):
        """A view class's methods and extra actions merged with its repository base classes' (its own win); auth, serializer and
        lookup: its own, else the nearest base's."""
        seen = (seen or set()) | {cls["ref"]}
        parents = [classes_by_name[b][0] for b in cls["bases"] if b in classes_by_name and len(classes_by_name[b]) == 1
                   and classes_by_name[b][0]["ref"] not in seen]
        if attr == "methods":
            merged = {}
            for p in reversed(parents):
                merged.update(inherited(p, attr, seen))
            return {**merged, **cls["methods"]}
        if attr == "extra_actions":
            own = {x["name"] for x in cls["extra_actions"]}
            return [x for p in parents for x in inherited(p, attr, seen) if x["name"] not in own] + cls["extra_actions"]
        val = cls.get(attr)
        if attr == "lookup" and val == "pk":
            val = None
        for p in parents:
            if val:
                break
            val = inherited(p, attr, seen)
        return val

    eps: dict = {}
    unknowns: list = []

    def unknown(where: dict, reason: str):
        unknowns.append({"file": where.get("file"), "line": where.get("line"), "reason": reason})

    def emit(path: str, method: str, view: dict, handler: str, handler_id: str, line: int, shape=None, status=None):
        full = "/" + path.lstrip("/")
        np_ = normalize_path(full, strip)
        eid = f"{out.service}:{method} {np_}"
        auth = view.get("auth") or (inherited(view, "auth") if view["kind"] == "view_class" else []) or []
        ser = view.get("serializer") or (inherited(view, "serializer") if view["kind"] == "view_class" else None)
        eps.setdefault(eid, Endpoint(id=eid, service=out.service, repo=out.repo, method=method, path=full, norm_path=np_,
                                     handler=handler, handler_id=handler_id, file=view["file"], line=line, response_schema=ser,
                                     response_shape=shape, success_status=status, auth=sorted(set(auth))))

    def emit_view(path: str, view: dict, actions: Optional[dict], where: dict):
        if view["kind"] == "view_fn":
            for m in view["methods"]:
                emit(path, m, view, view["name"], view["function_id"], view["line"])
            return
        methods = inherited(view, "methods") or {}
        base = lineage(view)
        if actions:                                                    # ViewSet.as_view({"get": "list"})
            for m, act in actions.items():
                h = methods.get(act)
                emit(path, m, view, f"{view['name']}.{act}", h["function_id"] if h else view["class_function_id"],
                     h["line"] if h else view["line"], "list" if act == "list" else "object", 201 if act == "create" else None)
            return
        gm = sorted({m for b in base for m in GENERIC_METHODS.get(b, [])})
        defined = [m.upper() for m in HTTP if m in methods]
        if not gm and not defined:
            unknown(where, f"{view['name']} declares no HTTP method TIA can see")
        for m in sorted(set(gm) | set(defined)):
            h = methods.get(m.lower())
            emit(path, m, view, f"{view['name']}.{m.lower()}", h["function_id"] if h else view["class_function_id"],
                 h["line"] if h else view["line"], status=201 if m == "POST" and "CreateAPIView" in " ".join(base) else None)

    def expand_router(prefix: str, router: dict, uc: dict):
        slash = "/" if router["trailing_slash"] else ""
        for reg in router["registrations"]:
            view = find_view(reg["view"]) if reg["view"] else None
            if reg["prefix"] is None or view is None or view["kind"] != "view_class":
                unknown({"file": uc["file"], "line": reg["line"]}, f"router.register(..., {reg['expr']}) cannot be resolved to a viewset")
                continue
            methods = inherited(view, "methods") or {}
            acts = sorted({a for b in lineage(view) for a in VIEWSET_ACTIONS.get(b, [])} | {a for a in ACTION_ROUTE if a in methods})
            coll = _join(prefix, reg["prefix"].strip("/"))
            detail = coll + "/{" + (inherited(view, "lookup") or "pk") + "}"
            for act in acts:
                m, is_detail = ACTION_ROUTE[act]
                h = methods.get(act)
                emit((detail if is_detail else coll) + slash, m, view, f"{view['name']}.{act}",
                     h["function_id"] if h else view["class_function_id"], h["line"] if h else view["line"],
                     "list" if act == "list" else "object", 201 if act == "create" else None)
            for x in inherited(view, "extra_actions") or []:
                for m in x["methods"]:
                    emit((detail if x["detail"] else coll) + "/" + x["url_path"].strip("/") + slash, m, view,
                         f"{view['name']}.{x['name']}", x["function_id"], x["line"])
            if not acts and not (inherited(view, "extra_actions") or []):
                unknown({"file": uc["file"], "line": reg["line"]}, f"{view['name']} exposes no action TIA can see")

    def walk(uc: dict, pats: list, prefix: str, depth: int, seen: frozenset):
        if depth > 12:
            return
        for p in pats:
            where = {"file": uc["file"], "line": p.get("line")}
            if p.get("unresolved"):
                unknown(where, p["unresolved"])
                continue
            r = _route(p)
            if r is None:
                unknown(where, f"the regular expression {p['route']!r} is not a plain path")
                continue
            here = _join(prefix, r)
            if "include" in p:
                tm = find_module(p["include"])
                if tm is None:
                    unknown(where, f"include({p['include']!r}): no such url module in the repository")
                elif tm not in seen:
                    walk(urlconfs[tm], urlconfs[tm]["patterns"], here, depth + 1, seen | {tm})
            elif "include_router" in p:
                router = uc["routers"].get(p["include_router"])
                if router is None:
                    unknown(where, f"router {p['include_router']} is not declared in this module")
                else:
                    expand_router(here, router, uc)
            elif "include_list" in p:
                walk(uc, uc["lists"].get(p["include_list"], []), here, depth + 1, seen)
            else:
                view = find_view(p["view"])
                if view is None:
                    unknown(where, f"the view {p['view']} is not in the repository")
                else:
                    emit_view(here, view, p.get("actions"), where)

    roots = [s["root_urlconf"] for s in facts if s["kind"] == "settings"]
    root_mods = [m for m in (find_module(r) for r in roots) if m]
    if not root_mods:
        included = {find_module(p["include"]) for uc in urlconfs.values() for p in uc["patterns"] + [x for v in uc["lists"].values() for x in v]
                    if p.get("include")}
        root_mods = sorted(m for m in urlconfs if m not in included)
    for m in root_mods:
        walk(urlconfs[m], urlconfs[m]["patterns"], "", 0, frozenset({m}))
    have = {e.id for e in out.endpoints}
    out.endpoints += [e for k, e in sorted(eps.items()) if k not in have]
    out.route_unknowns += unknowns
