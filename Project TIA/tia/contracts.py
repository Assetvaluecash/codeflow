"""Basic REST contract comparison (PRD FR-018 P0 list).

Detected:  removed endpoint, changed method/path, removed response field, field type change,
           request field becoming required (including new required request fields).
Anything else (status codes, auth, enum narrowing, headers) is reported as
"compatibility not evaluated" - never as backward compatible.
"""
from __future__ import annotations

from .models import RepoExtract

NOT_EVALUATED = ["status codes", "authentication/authorisation", "enum narrowing", "headers"]


def diff_contracts(base: RepoExtract, head: RepoExtract) -> list[dict]:
    changes: list[dict] = []

    # ---- endpoints -------------------------------------------------------------------------------
    head_by_key = {(e.method, e.norm_path): e for e in head.endpoints}
    head_by_handler = {e.handler: e for e in head.endpoints}
    for e in base.endpoints:
        if (e.method, e.norm_path) in head_by_key:
            continue
        moved = head_by_handler.get(e.handler)
        if moved:
            changes.append({"kind": "endpoint_method_or_path_changed", "breaking": True, "element": e.id,
                            "seeds": [e.id], "file": e.file, "line": e.line,
                            "detail": f"{e.method} {e.path} -> {moved.method} {moved.path}"})
        else:
            changes.append({"kind": "endpoint_removed", "breaking": True, "element": e.id, "seeds": [e.id],
                            "file": e.file, "line": e.line, "detail": f"{e.method} {e.path} removed"})

    # ---- schemas -------------------------------------------------------------------------------------
    head_schemas = {s.name: s for s in head.schemas}
    for s in base.schemas:
        resp_eps = [e for e in base.endpoints if e.response_schema == s.name]
        req_eps = [e for e in base.endpoints if s.name in e.request_schemas]
        used = bool(resp_eps or req_eps)
        hs = head_schemas.get(s.name)
        if hs is None:
            changes.append({"kind": "schema_removed", "breaking": used, "element": s.id, "seeds": [s.id],
                            "file": s.file, "line": s.line, "detail": f"schema {s.name} removed",
                            "used_by_endpoint": used})
            continue
        for fname, bf in s.fields.items():
            hf = hs.fields.get(fname)
            if hf is None:
                changes.append({"kind": "schema_field_removed", "breaking": bool(resp_eps), "element": s.id,
                                "seeds": [s.id], "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname} removed", "used_by_endpoint": used})
            elif hf["type"] != bf["type"]:
                changes.append({"kind": "schema_field_type_changed", "breaking": used, "element": s.id,
                                "seeds": [s.id], "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname}: {bf['type']} -> {hf['type']}",
                                "used_by_endpoint": used})
            elif hf["required"] and not bf["required"] and req_eps:
                changes.append({"kind": "request_field_became_required", "breaking": True, "element": s.id,
                                "seeds": [s.id], "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname} is now required", "used_by_endpoint": used})
        for fname, hf in hs.fields.items():
            if fname not in s.fields:
                new_required_req = hf["required"] and bool(req_eps)
                changes.append({"kind": "request_field_became_required" if new_required_req else "schema_field_added",
                                "breaking": new_required_req, "element": s.id, "seeds": [s.id],
                                "file": s.file, "line": s.line,
                                "detail": f"{s.name}.{fname} added ({'required' if hf['required'] else 'optional'})",
                                "used_by_endpoint": used})
    changes.sort(key=lambda c: (c["element"], c["kind"], c["detail"]))
    return changes
