"""REST contract comparison (PRD FR-018 basic and full).

Basic (always breaking): removed endpoint, changed method/path, removed response field, field type change, request field becoming
required (including new required request fields).

Full, each classified by a compatibility rule the tenant can configure (compatibility.rules in the tenant configuration):
  request_enum_value_removed    an allowed value of a request field or parameter is removed (enum narrowing)
  response_enum_value_added     a response field may carry a value consumers have not seen (enum widening)
  request_constraint_tightened  a request field or parameter accepts less than before (shorter max length, higher minimum, new pattern)
  request_param_required        a query/header parameter becomes required, or a new required one is added
  response_shape_changed        a response changes between a single object and a list
  success_status_changed        the declared success status changes (201 -> 200)
  auth_added                    a route that had no authentication/authorisation requirement gets one
  auth_changed                  the authentication/authorisation requirement of a route changes
  auth_removed                  a route loses its requirement (not breaking for consumers; flagged for security review)
Anything else (headers, error response bodies) is reported as "compatibility not evaluated" - never as backward compatible.
"""
from __future__ import annotations

from .models import RepoExtract

NOT_EVALUATED = ["headers", "error response bodies"]
RULES = {"request_enum_value_removed": "breaking", "response_enum_value_added": "breaking", "request_constraint_tightened": "breaking",
         "request_param_required": "breaking", "response_shape_changed": "breaking", "success_status_changed": "breaking",
         "auth_added": "breaking", "auth_changed": "breaking", "auth_removed": "compatible"}


def _allowed(f: dict, enums: dict):
    a = (f.get("constraints") or {}).get("allowed")
    if a:
        return [str(x) for x in a]
    t = (f.get("type") or "").replace("Optional[", "").replace("List[", "").rstrip("]").split(".")[-1]
    return [str(x) for x in enums[t]] if t in enums else None


def _tightened(b: dict, h: dict) -> list:
    """How a field's constraints accept less than before."""
    bc, hc, out = b.get("constraints") or {}, h.get("constraints") or {}, []
    for k, cmp in (("max_length", lambda x, y: y < x), ("max_items", lambda x, y: y < x), ("maximum", lambda x, y: y < x),
                   ("exclusive_maximum", lambda x, y: y < x), ("min_length", lambda x, y: y > x), ("min_items", lambda x, y: y > x),
                   ("minimum", lambda x, y: y > x), ("exclusive_minimum", lambda x, y: y > x)):
        if k in hc and (k not in bc or cmp(bc[k], hc[k])):
            out.append(f"{k.replace('_', ' ')} {bc.get(k, 'none')} -> {hc[k]}")
    if hc.get("pattern") and hc.get("pattern") != bc.get("pattern"):
        out.append(f"pattern {bc.get('pattern') or 'none'} -> {hc['pattern']}")
    if hc.get("format") and hc.get("format") != bc.get("format"):
        out.append(f"format {bc.get('format') or 'none'} -> {hc['format']}")
    return out


def diff_contracts(base: RepoExtract, head: RepoExtract, rules: dict = None) -> list[dict]:
    rules = {**RULES, **(rules or {})}
    changes: list[dict] = []

    def add(kind, rule, element, seeds, file, line, detail, **extra):
        changes.append({"kind": kind, "breaking": rules.get(rule, "breaking") == "breaking" if rule else True, "rule": rule,
                        "element": element, "seeds": seeds, "file": file, "line": line, "detail": detail, **extra})

    # ---- endpoints -------------------------------------------------------------------------------
    head_by_key = {(e.method, e.norm_path): e for e in head.endpoints}
    head_by_handler = {e.handler: e for e in head.endpoints}
    for e in base.endpoints:
        h = head_by_key.get((e.method, e.norm_path))
        if h is None:
            moved = head_by_handler.get(e.handler)
            if moved:
                add("endpoint_method_or_path_changed", None, e.id, [e.id], e.file, e.line, f"{e.method} {e.path} -> {moved.method} {moved.path}")
            else:
                add("endpoint_removed", None, e.id, [e.id], e.file, e.line, f"{e.method} {e.path} removed")
            continue
        if e.response_shape and h.response_shape and e.response_shape != h.response_shape:
            add("response_shape_changed", "response_shape_changed", e.id, [e.id], h.file, h.line,
                f"{e.method} {e.path}: response {e.response_shape} -> {h.response_shape}")
        if e.success_status and h.success_status and e.success_status != h.success_status:
            add("success_status_changed", "success_status_changed", e.id, [e.id], h.file, h.line,
                f"{e.method} {e.path}: success status {e.success_status} -> {h.success_status}")
        ba, ha = sorted(e.auth or []), sorted(h.auth or [])
        if ba != ha:
            rule = "auth_added" if not ba else "auth_removed" if not ha else "auth_changed"
            add(rule, rule, e.id, [e.id], h.file, h.line, f"{e.method} {e.path}: authorisation {', '.join(ba) or 'none'} -> {', '.join(ha) or 'none'}",
                **({"security_review": True} if rule == "auth_removed" else {}))
        bp, hp = {p["name"]: p for p in e.params}, {p["name"]: p for p in h.params}
        for n, p in hp.items():
            if p["in"] == "path":
                continue
            old = bp.get(n)
            if p.get("required") and (old is None or not old.get("required")):
                add("request_param_required", "request_param_required", e.id, [e.id], h.file, h.line,
                    f"{e.method} {e.path}: {p['in']} parameter '{n}' " + ("added (required)" if old is None else "is now required"))
            if old is not None:
                bal, hal = _allowed(old, base.enums), _allowed(p, head.enums)
                if bal and hal and set(bal) - set(hal):
                    add("request_enum_value_removed", "request_enum_value_removed", e.id, [e.id], h.file, h.line,
                        f"{e.method} {e.path}: parameter '{n}' no longer accepts {', '.join(sorted(set(bal) - set(hal)))}")
                t = _tightened(old, p)
                if t:
                    add("request_constraint_tightened", "request_constraint_tightened", e.id, [e.id], h.file, h.line,
                        f"{e.method} {e.path}: parameter '{n}' {'; '.join(t)}")

    # ---- schemas -------------------------------------------------------------------------------------
    head_schemas = {s.name: s for s in head.schemas}
    for s in base.schemas:
        resp_eps = [e for e in base.endpoints if e.response_schema == s.name]
        req_eps = [e for e in base.endpoints if s.name in e.request_schemas]
        used = bool(resp_eps or req_eps)
        hs = head_schemas.get(s.name)
        if hs is None:
            changes.append({"kind": "schema_removed", "breaking": used, "rule": None, "element": s.id, "seeds": [s.id],
                            "file": s.file, "line": s.line, "detail": f"schema {s.name} removed", "used_by_endpoint": used})
            continue
        for fname, bf in s.fields.items():
            hf = hs.fields.get(fname)
            if hf is None:
                changes.append({"kind": "schema_field_removed", "breaking": bool(resp_eps), "rule": None, "element": s.id,
                                "seeds": [s.id], "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname} removed", "used_by_endpoint": used})
                continue
            if hf["type"] != bf["type"]:
                changes.append({"kind": "schema_field_type_changed", "breaking": used, "rule": None, "element": s.id,
                                "seeds": [s.id], "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname}: {bf['type']} -> {hf['type']}", "used_by_endpoint": used})
                continue
            if hf["required"] and not bf["required"] and req_eps:
                changes.append({"kind": "request_field_became_required", "breaking": True, "rule": None, "element": s.id,
                                "seeds": [s.id], "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname} is now required", "used_by_endpoint": used})
            bal, hal = _allowed(bf, base.enums), _allowed(hf, head.enums)
            if req_eps and bal and hal and set(bal) - set(hal):
                add("request_enum_value_removed", "request_enum_value_removed", s.id, [s.id], s.file, s.line,
                    f"{s.name}.{fname} no longer accepts {', '.join(sorted(set(bal) - set(hal)))}", used_by_endpoint=used)
            if resp_eps and bal and hal and set(hal) - set(bal):
                add("response_enum_value_added", "response_enum_value_added", s.id, [s.id], s.file, s.line,
                    f"{s.name}.{fname} may now be {', '.join(sorted(set(hal) - set(bal)))}", used_by_endpoint=used)
            t = _tightened(bf, hf) if req_eps else []
            if t:
                add("request_constraint_tightened", "request_constraint_tightened", s.id, [s.id], s.file, s.line,
                    f"{s.name}.{fname}: {'; '.join(t)}", used_by_endpoint=used)
        for fname, hf in hs.fields.items():
            if fname not in s.fields:
                new_required_req = hf["required"] and bool(req_eps)
                changes.append({"kind": "request_field_became_required" if new_required_req else "schema_field_added",
                                "breaking": new_required_req, "rule": None, "element": s.id, "seeds": [s.id],
                                "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname} added ({'required' if hf['required'] else 'optional'})",
                                "used_by_endpoint": used})
    changes.sort(key=lambda c: (c["element"], c["kind"], c["detail"]))
    return changes
