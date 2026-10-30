"""Python ORM adapter: SQLAlchemy (declarative, Mapped/mapped_column, Flask-SQLAlchemy), SQLModel and Django models.

Emits, per file, the entities (model -> table -> columns) and the model references made inside each function (a function
that queries ``Model.objects`` / ``session.query(Model)`` / ``select(Model)`` reads the entity; one that adds, saves, deletes,
updates or inserts writes it). A reference to a name that is not a mapped model is dropped when the repository extract is
assembled. Raw SQL executed through a session, connection or cursor cannot be analysed and is reported, never read as
"no data access". Repository code is parsed, never executed."""
from __future__ import annotations

import ast
import posixpath
from typing import Optional

from .extract_data import m2m_target, m2m_through, sa_type, snake_flask
from .models import Entity, RepoExtract

DJANGO_WRITE = {"create", "update", "delete", "bulk_create", "bulk_update", "get_or_create", "update_or_create", "save", "add",
                "remove", "clear", "set", "contribute_to_class"}
SA_SESSION_WRITE = {"add", "add_all", "merge", "delete", "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"}
SA_CORE_WRITE = {"insert", "update", "delete"}
RAW_SQL_METHODS = {"execute", "executemany", "exec_driver_sql", "raw", "executescript"}
DJANGO_FIELD_SUFFIX = "Field"
DJANGO_REL = {"ForeignKey", "OneToOneField", "ManyToManyField"}


def _last(n) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return n.attr
    if isinstance(n, ast.Call):
        return _last(n.func)
    if isinstance(n, ast.Subscript):
        return _last(n.value)
    return ""


def _unp(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:                                                   # pragma: no cover
        return "?"


def _cs(n) -> Optional[str]:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _bool(n) -> Optional[bool]:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, bool) else None


def _tp(s: str) -> str:
    return " ".join(s.replace(", ", ",").split()).lower()


def _optional(ann) -> bool:
    s = _unp(ann)
    return "Optional[" in s or "| None" in s or "None |" in s or "Union[" in s and "None" in s


def _inner_mapped(ann) -> str:
    """Mapped[Optional[int]] -> int."""
    s = _unp(ann)
    if s.startswith("Mapped[") and s.endswith("]"):
        s = s[7:-1]
    if s.startswith("Optional[") and s.endswith("]"):
        s = s[9:-1]
    return s.replace(" | None", "").replace("None | ", "")


def app_label(path: str) -> str:
    """Django app label: the package that holds models.py (or the models/ package)."""
    d = posixpath.dirname(path)
    if posixpath.basename(d) == "models":
        d = posixpath.dirname(d)
    return posixpath.basename(d) or "app"


# =====================================================================================================================
# Model classes
# =====================================================================================================================
def _sa_attr(att: str, value: ast.Call, ann) -> Optional[tuple]:
    kind = _last(value.func)
    if kind not in ("Column", "mapped_column"):
        return None
    name, ty = None, ""
    for a in value.args:
        s = _cs(a)
        if s is not None and name is None:
            name = s
        elif not isinstance(a, ast.Constant) and not ty and _last(a) not in ("ForeignKey", "Sequence", "Identity"):
            ty = _unp(a)
    if not ty and ann is not None:
        ty = _inner_mapped(ann)
    pk = _bool(_kw(value, "primary_key")) is True
    nullable = _bool(_kw(value, "nullable"))
    if nullable is None:
        nullable = False if pk else (_optional(ann) if ann is not None and _unp(ann).startswith("Mapped[") else True)
    return (name or att).lower(), {"field": att, "type": sa_type(ty) if ty else "", "nullable": nullable, "pk": pk}


def _django_attr(att: str, value: ast.Call, app: str, model: str):
    kind = _last(value.func)
    if kind in DJANGO_REL:
        if kind == "ManyToManyField":
            return None, (f"{app}_{model.lower()}_{att}", {c: {"field": att, **spec} for c, spec in
                                                          m2m_through("", model, m2m_target(value)).items()})
        col = _cs(_kw(value, "db_column")) or f"{att}_id"
        return (col.lower(), {"field": att, "type": "fk", "nullable": _bool(_kw(value, "null")) is True,
                              "pk": _bool(_kw(value, "primary_key")) is True}), None
    if not kind.endswith(DJANGO_FIELD_SUFFIX):
        return None, None
    ty = kind
    if _kw(value, "max_length") is not None:
        ty += f"({_unp(_kw(value, 'max_length'))})"
    pk = _bool(_kw(value, "primary_key")) is True or kind in ("AutoField", "BigAutoField", "SmallAutoField")
    col = _cs(_kw(value, "db_column")) or att
    return (col.lower(), {"field": att, "type": ty.lower(), "nullable": _bool(_kw(value, "null")) is True and not pk,
                          "pk": pk}), None


def _is_django_field(func, imports: dict) -> bool:
    """A field built by django.db.models (not a DRF serializer or form field that happens to be called CharField)."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return imports.get(func.value.id, "").startswith("django.db.models") or imports.get(func.value.id) == "django.db.models"
    if isinstance(func, ast.Name):
        return imports.get(func.id, "").startswith("django.db.models.")
    return False


def model_entity(node: ast.ClassDef, repo: str, service: str, path: str, imports: dict, out: RepoExtract) -> Optional[Entity]:
    """The ORM entity a class declares, or None. Appends what could not be analysed to out.data_unknowns."""
    mods = " ".join(imports.values())
    django = "django.db" in mods
    sqla = "sqlalchemy" in mods or "sqlmodel" in mods or "flask_sqlalchemy" in mods
    if not (django or sqla):
        return None
    bases = [_unp(b) for b in node.bases]
    base_last = [b.rsplit(".", 1)[-1] for b in bases]
    # ---- Django ------------------------------------------------------------------------------------------------------
    if django:
        cols, joins_tables, has_field = {}, [], False
        app = app_label(path)
        for st in node.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) \
                    and isinstance(st.value, ast.Call) and _is_django_field(st.value.func, imports):
                col, through = _django_attr(st.targets[0].id, st.value, app, node.name)
                if col:
                    cols[col[0]] = col[1]
                    has_field = True
                if through:
                    joins_tables.append({"table": through[0], "columns": through[1]})
                    has_field = True
        meta = next((s for s in node.body if isinstance(s, ast.ClassDef) and s.name == "Meta"), None)
        opts = {}
        for st in (meta.body if meta else []):
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                opts[st.targets[0].id] = st.value
        is_model = "Model" in base_last or has_field
        if is_model and not (sqla and "__tablename__" in {t.id for s in node.body if isinstance(s, ast.Assign)
                                                          for t in s.targets if isinstance(t, ast.Name)}):
            if _bool(opts.get("proxy")) is True:
                return None
            abstract = _bool(opts.get("abstract")) is True
            declared = _cs(opts.get("db_table")) if opts.get("db_table") is not None else None
            return Entity(id=f"{service}:entity:{node.name}", service=service, repo=repo, name=node.name,
                          table=(declared or f"{app}_{node.name.lower()}").lower(),
                          table_source="declared" if declared else "django_default", columns=cols, kind="django", file=path,
                          line=node.lineno, bases=[b for b in base_last if b not in ("Model", "object")], abstract=abstract,
                          app=app, join_tables=joins_tables,
                          notes=[] if declared else [f"table name derived from the app label '{app}'; a custom AppConfig label "
                                                     "would change it"])
    # ---- SQLAlchemy / SQLModel / Flask-SQLAlchemy ------------------------------------------------------------------------
    if sqla:
        tablename, dynamic, cols, has_col, abstract_flag = None, False, {}, False, False
        sqlmodel_table = any(k.arg == "table" and _bool(k.value) is True for k in node.keywords)       # class X(Base, table=True)
        sqlmodel_fields = sqlmodel_table or "SQLModel" in base_last      # a non-table SQLModel base contributes its fields
        for st in node.body:
            target = value = ann = None
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                target, value = st.targets[0].id, st.value
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                target, value, ann = st.target.id, st.value, st.annotation
            elif isinstance(st, ast.FunctionDef) and st.name == "__tablename__":
                dynamic = True
                continue
            if target is None:
                continue
            if target == "__tablename__":
                tablename = _cs(value)
                dynamic = dynamic or tablename is None
            elif target == "__abstract__":
                abstract_flag = _bool(value) is True
            elif target == "__table__":
                out.data_unknowns.append({"file": path, "line": st.lineno, "kind": "sqlalchemy_table_object",
                                          "reason": f"{node.name}: mapping to an explicit Table object is not analysed"})
            elif isinstance(value, ast.Call) and _last(value.func) in ("Column", "mapped_column"):
                got = _sa_attr(target, value, ann)
                if got:
                    cols[got[0]] = got[1]
                    has_col = True
            elif sqlmodel_fields and ann is not None and not (isinstance(value, ast.Call) and _last(value.func) == "Relationship") \
                    and not _unp(ann).startswith("ClassVar"):
                pk = isinstance(value, ast.Call) and _bool(_kw(value, "primary_key")) is True
                sa_col = _kw(value, "sa_column") if isinstance(value, ast.Call) else None
                name = target
                if isinstance(sa_col, ast.Call):
                    got = _sa_attr(target, sa_col, None)
                    if got:
                        cols[got[0]] = {**got[1], "field": target}
                        has_col = True
                        continue
                nullable = False if pk else _optional(ann)
                if isinstance(value, ast.Call) and _bool(_kw(value, "nullable")) is not None:
                    nullable = _bool(_kw(value, "nullable"))
                cols[name.lower()] = {"field": target, "type": _tp(_unp(ann)), "nullable": nullable, "pk": pk}
                has_col = True
        flask_model = any(b.endswith(".Model") or b == "Model" for b in bases)
        if tablename or dynamic or sqlmodel_table or flask_model and has_col:
            if dynamic:
                out.data_unknowns.append({"file": path, "line": node.lineno, "kind": "dynamic_tablename",
                                          "reason": f"{node.name}: __tablename__ is not a string literal"})
                return Entity(id=f"{service}:entity:{node.name}", service=service, repo=repo, name=node.name, table="",
                              table_source="unresolved", columns=cols, kind="sqlalchemy", file=path, line=node.lineno,
                              bases=[b for b in base_last if b not in ("Base", "Model", "SQLModel")])
            source = "declared"
            if not tablename:
                tablename = node.name.lower() if sqlmodel_table else snake_flask(node.name)
                source = "naming_strategy"
            return Entity(id=f"{service}:entity:{node.name}", service=service, repo=repo, name=node.name,
                          table=tablename.lower(), table_source=source, columns=cols, kind="sqlalchemy", file=path,
                          line=node.lineno, bases=[b for b in base_last if b not in ("Base", "Model", "SQLModel", "DeclarativeBase")])
        if has_col and (abstract_flag or not tablename):
            return Entity(id=f"{service}:entity:{node.name}", service=service, repo=repo, name=node.name, table="",
                          table_source="declared", columns=cols, kind="sqlalchemy", file=path, line=node.lineno,
                          bases=[b for b in base_last if b not in ("Base", "Model", "SQLModel", "DeclarativeBase")], abstract=True)
    return None


# =====================================================================================================================
# Model references inside functions
# =====================================================================================================================
def _model_name(n) -> Optional[str]:
    """Model named by an expression: X, pkg.X, X.column, X(...)."""
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        if isinstance(n.value, ast.Name) and n.value.id[:1].islower() and n.attr[:1].isupper():
            return n.attr                                      # models.Order
        if isinstance(n.value, (ast.Name, ast.Attribute)):
            inner = _model_name(n.value)
            if inner and inner[:1].isupper():
                return inner                                   # Order.status
        return None
    if isinstance(n, ast.Call):
        return _model_name(n.func) if isinstance(n.func, ast.Name) else None
    return None


def _objects_model(expr) -> Optional[str]:
    """Model whose ``.objects`` manager a call chain starts from: X.objects.filter(...).order_by(...)."""
    while True:
        if isinstance(expr, ast.Call):
            expr = expr.func
        elif isinstance(expr, ast.Attribute):
            if expr.attr == "objects":
                return _model_name(expr.value)
            expr = expr.value
        else:
            return None


def _query_model(expr) -> Optional[str]:
    """Model of the nearest session.query(X) / select(X) / insert(X) at the root of a chain."""
    while isinstance(expr, (ast.Call, ast.Attribute)):
        if isinstance(expr, ast.Call):
            if _last(expr.func) in ("query", "select") and expr.args:
                return _model_name(expr.args[0])
            expr = expr.func
        else:
            expr = expr.value
    return None


def function_refs(fn, fid: str, path: str, scope: list, out: RepoExtract) -> list:
    """Model references made by one function: [{function_id, model, op, file, line, via}]."""
    refs: list = []
    selfname = scope[-1] if scope else None
    local: dict = {}
    if selfname:
        local["self"] = local["cls"] = selfname
    for a in list(fn.args.args) + list(fn.args.kwonlyargs):
        if a.annotation is not None:
            m = _model_name(a.annotation)
            if m and m[:1].isupper():
                local[a.arg] = m
    for st in fn.body:
        for n in ast.walk(st):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                m = _objects_model(n.value) or _query_model(n.value)
                if m is None and isinstance(n.value, ast.Call):
                    m = _model_name(n.value.func) if isinstance(n.value.func, ast.Name) else None
                if m:
                    local[n.targets[0].id] = local.get(m, m)

    def add(model, op, line, via):
        if model:
            refs.append({"function_id": fid, "model": local.get(model, model) if model in ("self", "cls") else model,
                         "op": op, "file": path, "line": line, "via": via})
    for st in fn.body:
        for n in ast.walk(st):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            attr = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            # ---- Django manager / queryset ------------------------------------------------------------------------------
            m = _objects_model(f.value) if isinstance(f, ast.Attribute) else None
            if m:
                model = local.get(m, m) if m in ("self", "cls") else m
                add(model, "write" if attr in DJANGO_WRITE else "read", n.lineno, f"{model}.objects.{attr}")
            elif isinstance(f, ast.Attribute) and attr in ("save", "delete") and isinstance(f.value, (ast.Name, ast.Call)):
                v = f.value.id if isinstance(f.value, ast.Name) else None
                model = local.get(v) if v else (_model_name(f.value.func) if isinstance(f.value.func, ast.Name) else None)
                if model:
                    add(model, "write", n.lineno, f"{model}.{attr}")
            elif isinstance(f, ast.Attribute) and attr == "refresh_from_db" and isinstance(f.value, ast.Name) and f.value.id in local:
                add(local[f.value.id], "read", n.lineno, f"{local[f.value.id]}.refresh_from_db")
            # ---- SQLAlchemy ----------------------------------------------------------------------------------------------
            if attr in ("query", "select") and n.args:
                for a in (n.args if attr == "select" else n.args[:1]):
                    add(_model_name(a), "read", n.lineno, f"{attr}")
            elif attr in SA_CORE_WRITE and n.args and not (isinstance(f, ast.Attribute) and _objects_model(f.value)):
                if isinstance(f, ast.Name) or (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                                               and f.value.id in ("sa", "sqlalchemy", "sql")):
                    add(_model_name(n.args[0]), "write", n.lineno, attr)
                elif attr in ("update", "delete") and isinstance(f, ast.Attribute) and _query_model(f.value):
                    add(_query_model(f.value), "write", n.lineno, f"query.{attr}")
            if isinstance(f, ast.Attribute) and attr in SA_SESSION_WRITE and n.args:
                targets = n.args[0].elts if isinstance(n.args[0], (ast.List, ast.Tuple)) else [n.args[0]]
                for t in targets:
                    mm = local.get(t.id) if isinstance(t, ast.Name) else (_model_name(t.func) if isinstance(t, ast.Call) and isinstance(t.func, ast.Name) else None)
                    if mm:
                        add(mm, "write", n.lineno, f"session.{attr}")
            elif isinstance(f, ast.Attribute) and attr == "get" and len(n.args) >= 1 and isinstance(n.args[0], (ast.Name, ast.Attribute)) \
                    and _model_name(n.args[0]) and _model_name(n.args[0])[:1].isupper() and len(n.args) + len(n.keywords) >= 2:
                add(_model_name(n.args[0]), "read", n.lineno, "session.get")
            elif isinstance(f, ast.Attribute) and attr in ("update", "delete") and _query_model(f.value):
                add(_query_model(f.value), "write", n.lineno, f"query.{attr}")
            # ---- raw SQL ----------------------------------------------------------------------------------------------------
            if isinstance(f, ast.Attribute) and attr in RAW_SQL_METHODS and n.args:
                a0 = n.args[0]
                if isinstance(a0, (ast.Constant, ast.JoinedStr, ast.BinOp)) or (isinstance(a0, ast.Call) and _last(a0.func) == "text"):
                    out.data_unknowns.append({"file": path, "line": n.lineno, "kind": "raw_sql",
                                              "reason": f"raw SQL executed in {fid.split('::')[-1]} is not analysed (FR-041)"})
    return refs
