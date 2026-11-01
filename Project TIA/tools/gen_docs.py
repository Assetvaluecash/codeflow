"""Generates docs/API.md from the running application's own route table and OpenAPI schema (so the reference cannot drift from the code).

    python tools/gen_docs.py            # rewrite docs/API.md
    python tools/gen_docs.py --check    # exit 1 if docs/API.md is out of date (the test suite does this)

The administrator and user guides are written by hand; tests/test_docs.py checks that every environment variable, command and link they
mention exists, and that the configuration examples in them are accepted by the real validators.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "API.md"

INTRO = """# TIA HTTP API reference

*Generated from the application's route table and OpenAPI schema by `tools/gen_docs.py`; do not edit by hand (a test fails when it is out of date).*

All paths are relative to the server's address. Request and response bodies are JSON unless stated.

## Authentication

With `TIA_AUTH_REQUIRED=true` every route except the ones marked **public** needs one of:

| Credential | How it is sent | Who it is for |
|---|---|---|
| Service-account token | `Authorization: Bearer tia_<tenant>_<id>_<secret>` | CI jobs, scripts. Has roles and an optional repository scope; created by an administrator; shown once. |
| OpenID Connect token | `Authorization: Bearer <JWT>` from the tenant's identity provider | Programs acting for a signed-in person. Issuer and audience select the tenant. |
| Browser session | `tia_session` cookie set by `/auth/login` (OIDC or SAML) | The web interface. State-changing calls must also send the `X-TIA-CSRF` value from `GET /auth/me`. |

The credential names the **tenant**: every request runs against that tenant's data only. Without `TIA_AUTH_REQUIRED` (development) everyone is an administrator on one tenant.

Roles: `viewer`, `engineer`, `release_manager`, `auditor`, `administrator` (additive). Each route below lists the permission it needs and the roles that hold it.
A repository the caller may not read answers `404`, as if it did not exist.

Common answers: `401` no/invalid credential (`code`: `authentication_required`, `invalid_token`, `no_session`, `session_expired`, `session_revoked`), `403` not permitted
(`forbidden`, `csrf`, `user_disabled`, `no_role`, `tenant_suspended`, `storage_quota_exceeded`), `404` unknown or not readable, `410` tenant offboarded, `422` invalid body
(configuration endpoints list every problem), `424` a GitHub App installation problem, `429` too many pending sign-ins, `503` a required service is not configured.
Every response carries `X-Request-ID` and `X-Trace-Id`.

"""


def build_app():
    from tia.api import create_app

    class Stub:
        """create_app only builds objects at start-up; no database call is made until a request arrives."""

        def tx(self, *a, **k):
            raise RuntimeError("documentation build: no database")
        plain = tx

        def close(self):
            pass

    from tia.authapi import AuthSettings
    return create_app(Stub(), "00000000-0000-0000-0000-000000000000", tempfile.mkdtemp(prefix="tia-docs-"), allow_local=False,
                      auth=AuthSettings(required=True, public_url="https://tia.example.com"))


def schema_of(openapi: dict, ref_or_schema: dict, depth: int = 0) -> list[tuple[str, str, bool, str]]:
    """(field, type, required, description) rows for a request body."""
    s = ref_or_schema
    if "$ref" in s:
        s = openapi["components"]["schemas"][s["$ref"].rsplit("/", 1)[-1]]
    if s.get("type") == "object" and "properties" not in s:
        return [("(object)", "object", True, "free-form JSON object")]
    req = set(s.get("required", []))
    rows = []
    for name, p in (s.get("properties") or {}).items():
        t = p.get("type")
        if "anyOf" in p:
            t = " | ".join(x.get("type") or x.get("$ref", "").rsplit("/", 1)[-1] or "?" for x in p["anyOf"] if x.get("type") != "null") + (" (optional)" if any(x.get("type") == "null" for x in p["anyOf"]) else "")
        elif "$ref" in p:
            t = p["$ref"].rsplit("/", 1)[-1]
        elif t == "array":
            it = p.get("items", {})
            t = f"array of {it.get('type') or it.get('$ref', '').rsplit('/', 1)[-1] or 'any'}"
        rows.append((name, t or "any", name in req, (p.get("description") or "").replace("\n", " ")))
    return rows


def render() -> str:
    from tia import rbac
    app = build_app()
    oa = app.openapi()
    docs = {}
    for r in app.routes:
        if getattr(r, "methods", None) and getattr(r, "include_in_schema", True) is not False:
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                docs[(r.path, m)] = r
    public_extra = [(p, m) for (m, p) in rbac.ROUTES if (p, m) not in docs]           # routes hidden from the schema (the web pages, sign-in, health)
    for r in app.routes:
        if getattr(r, "methods", None) and getattr(r, "include_in_schema", True) is False:
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                docs.setdefault((r.path, m), r)

    def group(path: str) -> str:
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return "Web interface"
        if parts[0] == "auth":
            return "Sign-in and session (/auth)"
        if parts[0] in ("healthz", "readyz", "metrics"):
            return "Health and metrics"
        if parts[0] == "analyses":
            return "Web interface"
        if parts[:2] == ["api", "v1"] and len(parts) > 2:
            seg = parts[2]
            if seg == "admin":
                return "Administration (/api/v1/admin)"
            return {"impact-analyses": "Impact analyses", "repositories": "Repositories", "uploads": "Uploads", "graph": "Code graph", "tenant": "Tenant and configuration",
                    "jobs": "Jobs", "audit": "Audit", "webhooks": "Webhooks", "evidence-snapshots": "Evidence snapshots", "me": "Who am I", "health": "Health and metrics",
                    "config": "Tenant and configuration"}.get(seg, "Other")
        return "Other"

    sections: dict[str, list] = {}
    for (path, method), route in sorted(docs.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        sections.setdefault(group(path), []).append((path, method, route))
    out = [INTRO]
    for title in sorted(sections, key=lambda t: ("Web interface" in t, t)):
        out.append(f"## {title}\n")
        for path, method, route in sections[title]:
            perm = rbac.classify(method, path)
            who = "public" if perm == rbac.PUBLIC else "any signed-in principal" if perm == rbac.AUTHENTICATED else \
                f"`{perm}` ({', '.join(f'`{x}`' for x in sorted(rbac.PERMISSIONS[perm], key=rbac.ROLES.index))})" if perm else "not classified (refused)"
            doc = (route.endpoint.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ") if hasattr(route, "endpoint") else ""
            doc = " ".join(doc.split())
            out.append(f"### `{method} {path}`\n")
            out.append(f"**Needs:** {who}.  " + (doc if doc else "") + "\n")
            op = oa.get("paths", {}).get(path, {}).get(method.lower())
            if op:
                params = [p for p in op.get("parameters", []) if p.get("in") in ("query", "path")]
                if params:
                    out.append("| Parameter | In | Type | Required |\n|---|---|---|---|")
                    for p in params:
                        t = (p.get("schema") or {}).get("type") or ("|".join(x.get("type", "?") for x in (p.get("schema") or {}).get("anyOf", [])) or "any")
                        out.append(f"| `{p['name']}` | {p['in']} | {t} | {'yes' if p.get('required') else 'no'} |")
                    out.append("")
                body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
                if body:
                    rows = schema_of(oa, body)
                    out.append("Request body:\n\n| Field | Type | Required | Notes |\n|---|---|---|---|")
                    out += [f"| `{n}` | {t} | {'yes' if rq else 'no'} | {d} |" for n, t, rq, d in rows]
                    out.append("")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="fail if docs/API.md is not up to date")
    a = ap.parse_args(argv)
    text = render()
    if a.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print("docs/API.md is out of date: run  python tools/gen_docs.py", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
