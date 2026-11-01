"""PostgreSQL access: connection pool with tenant-bound transactions, migrations, tenant registry.

Isolation model (TECHNICAL-FRAMEWORK 9.1): every tenant-owned table has tenant_id and FORCED row-level
security. The runtime connects as `tia_app` (not an owner, no BYPASSRLS) and every transaction first runs
set_config('tia.tenant_id', ..., true); a session without a tenant sees no rows. Migrations and the tenant
registry use a separate admin DSN that the request path never holds.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import psycopg
import psycopg.conninfo
from psycopg import sql
from psycopg_pool import ConnectionPool

# Tenant-owned tables the runtime role may read (the tenant registry tables are admin-only). tests/test_tenants.py checks this list
# against the catalog, so a migration that adds a tenant table cannot be forgotten here.
RUNTIME_TABLES = ("analyses", "audit_log", "auth_login_states", "auth_sessions", "evidence_snapshots", "extractions", "file_extracts",
                  "graph_heads", "graph_versions", "jobs", "relationships", "repo_access", "repo_grants", "repositories", "saml_assertions_seen",
                  "scm_permissions", "scm_sync_status", "service_tokens", "tenant_config_versions", "tenant_idps", "tenant_keys", "tenant_secrets",
                  "tenant_users")
APP_ROLE = "tia_app"
WORKER_ROLE = "tia_worker"
MIGRATIONS = Path(__file__).parent / "migrations"


class Database:
    """Runtime access as the non-privileged application role."""

    def __init__(self, app_dsn: str, max_size: int = 10):
        self.pool = ConnectionPool(app_dsn, min_size=1, max_size=max_size, open=True, timeout=30)

    @contextmanager
    def tx(self, tenant_id):
        """One transaction bound to one tenant. Everything inside is filtered/guarded by RLS."""
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("select set_config('tia.tenant_id', %s, true)", (str(uuid.UUID(str(tenant_id))),))
                yield conn

    @contextmanager
    def plain(self):
        """A transaction bound to NO tenant: it sees no rows. Only for the narrow SECURITY DEFINER lookup functions that find the
        tenant a caller belongs to (identity-provider routing, sign-in options)."""
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("select set_config('tia.tenant_id', '', true)")
                yield conn

    def close(self):
        self.pool.close()


def migrate(admin_dsn: str, app_password: str, worker_password: Optional[str] = None) -> list[str]:
    """Create/refresh the app and worker roles and apply pending SQL migrations. Returns versions applied now."""
    applied_now: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        for role, pw in ((APP_ROLE, app_password), (WORKER_ROLE, worker_password or app_password)):
            exists = c.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
            stmt = "alter role {} with login password {}" if exists else \
                "create role {} login password {} nosuperuser nobypassrls nocreatedb nocreaterole"
            c.execute(sql.SQL(stmt).format(sql.Identifier(role), sql.Literal(pw)))
        c.execute(sql.SQL("grant {} to {}").format(sql.Identifier(APP_ROLE), sql.Identifier(WORKER_ROLE)))
        c.execute("create table if not exists schema_migrations (version text primary key, applied_at timestamptz not null default now())")
        done = {r[0] for r in c.execute("select version from schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem in done:
                continue
            with c.transaction():
                c.execute(f.read_text(encoding="utf-8"))
                c.execute("insert into schema_migrations (version) values (%s)", (f.stem,))
            applied_now.append(f.stem)
    return applied_now


class TenantAdmin:
    """Tenant registry (admin credentials only; never used on the request path)."""

    def __init__(self, admin_dsn: str):
        self.dsn = admin_dsn

    def ensure(self, name: str) -> str:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            r = c.execute("insert into tenants (name) values (%s) on conflict (name) do update set name = excluded.name "
                          "returning id, status", (name,)).fetchone()
        if r[1] in ("offboarding", "offboarded"):
            raise ValueError(f"tenant '{name}' is {r[1]}; names of offboarded tenants are never reused")
        return str(r[0])

    def get(self, name: str):
        with psycopg.connect(self.dsn) as c:
            r = c.execute("select id, status from tenants where name = %s", (name,)).fetchone()
        return {"id": str(r[0]), "status": r[1]} if r else None

    def list(self) -> list[dict]:
        with psycopg.connect(self.dsn) as c:
            return [{"id": str(i), "name": n, "status": s} for i, n, s in
                    c.execute("select id, name, status from tenants order by name")]


def app_dsn_from_admin(admin_dsn: str, password: str, role: str = APP_ROLE) -> str:
    """Derive a runtime DSN (tia_app, or tia_worker) from an admin DSN - for development and tests."""
    info = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    info.update(user=role, password=password)
    return psycopg.conninfo.make_conninfo(**info)
