"""Roles, permissions and the route table (PL-002). Deny by default: a route that is not listed here is refused to everyone, and
tests/test_auth.py fails if the API registers a route this table does not classify.

Roles (PRD PL-002): viewer, engineer, release_manager, auditor, administrator. A person may hold several; permissions are the union.
"""
from __future__ import annotations

ROLES = ("viewer", "engineer", "release_manager", "auditor", "administrator")

PERMISSIONS = {
    "read": {"viewer", "engineer", "release_manager", "auditor", "administrator"},        # reports, graph, evidence, repositories, jobs, config
    "analyze": {"engineer", "release_manager", "administrator"},                            # run/reproduce/cancel analyses, publish to the PR
    "repo_write": {"engineer", "release_manager", "administrator"},                         # register, sync, discover, upload, rescan
    "repo_delete": {"administrator"},
    "config_write": {"release_manager", "administrator"},                                    # severity, materiality, limits, criticality, audience
    "audit_read": {"auditor", "administrator"},
    "access_sync": {"release_manager", "administrator"},                                     # refresh the mirror of the source-code host's permissions
    "admin": {"administrator"},                                                              # identity providers, users, tokens, sessions, access
}

PUBLIC = "public"                    # no credentials needed (health, the sign-in flow, the static shell, signed webhooks)
AUTHENTICATED = "authenticated"     # any signed-in principal, whatever its roles

# (METHOD, route template as FastAPI registers it) -> permission | PUBLIC | AUTHENTICATED
ROUTES = {
    ("GET", "/"): PUBLIC, ("GET", "/healthz"): PUBLIC, ("GET", "/readyz"): PUBLIC, ("GET", "/api/v1/health"): PUBLIC,
    ("GET", "/metrics"): PUBLIC,                                           # has its own token / loopback rule
    ("POST", "/api/v1/webhooks/github"): PUBLIC,                           # authenticated by HMAC signature
    ("POST", "/api/v1/webhooks/ado"): PUBLIC,                              # authenticated by the service hook's basic-auth credentials
    ("GET", "/auth/options"): PUBLIC, ("GET", "/auth/login"): PUBLIC, ("GET", "/auth/callback"): PUBLIC,
    ("POST", "/auth/saml/acs"): PUBLIC, ("GET", "/auth/saml/metadata"): PUBLIC,
    ("GET", "/auth/me"): PUBLIC,                                           # answers {authenticated: false} instead of 401, for the UI shell
    ("POST", "/auth/logout"): AUTHENTICATED,
    ("GET", "/api/v1/me"): AUTHENTICATED,
    ("POST", "/query/tia"): "read",                                        # Knowledge Fabric V2 agents: a finished report as a QueryResponse
    ("GET", "/api/v1/config"): PUBLIC,
    ("GET", "/api/v1/tenant"): "read",
    # tenant configuration
    ("GET", "/api/v1/tenant/config"): "read", ("GET", "/api/v1/tenant/config/versions"): "read",
    ("GET", "/api/v1/tenant/config/versions/{n}"): "read", ("GET", "/api/v1/tenant/config/diff"): "read",
    ("POST", "/api/v1/tenant/config/validate"): "read",
    ("PUT", "/api/v1/tenant/config"): "config_write", ("POST", "/api/v1/tenant/config/versions/{n}/restore"): "config_write",
    # repositories and uploads
    ("GET", "/api/v1/repositories"): "read", ("GET", "/api/v1/repositories/{name}/revisions"): "read",
    ("POST", "/api/v1/repositories"): "repo_write", ("POST", "/api/v1/repositories/discover"): "repo_write",
    ("POST", "/api/v1/repositories/{name}/sync"): "repo_write", ("PUT", "/api/v1/repositories/{name}/upload"): "repo_write",
    ("DELETE", "/api/v1/repositories/{name}"): "repo_delete",
    ("POST", "/api/v1/uploads"): "repo_write", ("POST", "/api/v1/uploads/{uid}/files"): "repo_write",
    ("PUT", "/api/v1/uploads/{uid}/archive"): "repo_write", ("POST", "/api/v1/uploads/{uid}/commit"): "repo_write",
    ("DELETE", "/api/v1/uploads/{uid}"): "repo_write",
    # audit
    ("GET", "/api/v1/audit"): "audit_read", ("GET", "/api/v1/audit/verify"): "audit_read",
    # graph
    ("GET", "/api/v1/graph"): "read", ("GET", "/api/v1/graph/versions"): "read", ("GET", "/api/v1/graph/versions/{gid}"): "read",
    ("GET", "/api/v1/graph/relationships"): "read", ("GET", "/api/v1/graph/stats"): "read", ("POST", "/api/v1/graph/rescan"): "repo_write",
    # analyses
    ("POST", "/api/v1/impact-analyses"): "analyze", ("GET", "/api/v1/jobs/{job_id}"): "read", ("DELETE", "/api/v1/jobs/{job_id}"): "analyze",
    ("GET", "/api/v1/impact-analyses/{aid}"): "read", ("GET", "/analyses/{aid}"): "read",
    ("GET", "/api/v1/impact-analyses/{aid}/graph"): "read", ("GET", "/api/v1/impact-analyses/{aid}/comment"): "read",
    ("GET", "/api/v1/impact-analyses/{aid}/evidence"): "read", ("GET", "/api/v1/impact-analyses/{aid}/paths"): "read",
    ("GET", "/api/v1/impact-analyses/{aid}/unknowns"): "read", ("GET", "/api/v1/evidence-snapshots/{sid}"): "read",
    ("POST", "/api/v1/impact-analyses/{aid}/publish"): "analyze", ("POST", "/api/v1/impact-analyses/{aid}/reproduce"): "analyze",
    # administration
    ("GET", "/api/v1/admin/idps"): "admin", ("POST", "/api/v1/admin/idps"): "admin", ("PUT", "/api/v1/admin/idps/{iid}"): "admin",
    ("DELETE", "/api/v1/admin/idps/{iid}"): "admin",
    ("GET", "/api/v1/admin/users"): "admin", ("PUT", "/api/v1/admin/users/{uid}"): "admin",
    ("GET", "/api/v1/admin/tokens"): "admin", ("POST", "/api/v1/admin/tokens"): "admin", ("DELETE", "/api/v1/admin/tokens/{tid}"): "admin",
    ("GET", "/api/v1/admin/sessions"): "admin", ("DELETE", "/api/v1/admin/sessions/{sid}"): "admin",
    ("GET", "/api/v1/admin/github/installations"): "admin", ("POST", "/api/v1/admin/github/installations"): "admin",
    ("DELETE", "/api/v1/admin/github/installations/{iid}"): "admin",
    ("GET", "/api/v1/admin/repo-access"): "admin", ("PUT", "/api/v1/admin/repo-access/{name}"): "admin",
    ("POST", "/api/v1/admin/repo-access/sync"): "access_sync",
}

# Static assets and generated docs are not API routes; they are served without credentials (they contain no tenant data).
STATIC_PREFIXES = ("/vendor/",)
UNLISTED_OK = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def roles_for(permission: str) -> set:
    return PERMISSIONS.get(permission, set())


def allows(roles, permission: str) -> bool:
    return bool(set(roles) & roles_for(permission))


def classify(method: str, template: str):
    """The permission a route needs; None means unlisted, which is refused."""
    return ROUTES.get((method.upper(), template))


def valid_roles(roles) -> list:
    out = []
    for r in roles:
        if r not in ROLES:
            raise ValueError(f"unknown role '{r}'; roles are {', '.join(ROLES)}")
        if r not in out:
            out.append(r)
    return out
