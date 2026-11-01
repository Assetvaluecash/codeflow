"""Tenant lifecycle (UC1-20, PL-013, AC-039): provision, suspend / resume, SCM installation mapping, storage quota, offboarding with
verified deletion.

Runs with ADMIN credentials only (python -m tia.tenants ...); the request path never holds them. Everything it does is recorded in
tenant_events - metadata only, no tenant content - which is kept after offboarding as evidence that the deletion happened.

Offboarding is deliberate and verifiable:
  * the operator must type the tenant's name, and the tenant is blocked (status 'offboarding') before anything is deleted;
  * every table that has a tenant_id column is discovered from the catalog and emptied (a future table cannot be forgotten);
  * the deletion is then independently verified (all such tables re-counted, workspace directories checked) and only a clean
    verification moves the tenant to 'offboarded'; otherwise it stays 'offboarding' and can be re-run;
  * the result is a hashed deletion certificate stored on the registry tombstone. A name is never reused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
PROVIDERS = ("github", "ado")
COLS = ("id, name, display_name, plan, status, storage_quota_bytes, suspended_at, suspended_reason, created_at, offboarded_at")


class TenantError(Exception):
    """A lifecycle operation that cannot or must not be done; the message says why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r) -> dict:
    keys = [c.strip() for c in COLS.split(",")]
    d = dict(zip(keys, r))
    d["id"] = str(d["id"])
    for k in ("suspended_at", "created_at", "offboarded_at"):
        d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
    return d


def tenant_tables(c) -> list:
    """Every base table in the schema that has a tenant_id column: the tenant's data. Discovered, never hard-coded."""
    return [r[0] for r in c.execute(
        "select c.table_name from information_schema.columns c join information_schema.tables t "
        "  on t.table_schema = c.table_schema and t.table_name = c.table_name "
        "where c.table_schema = 'public' and c.column_name = 'tenant_id' and t.table_type = 'BASE TABLE' order by 1")]


def dir_size(path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _writable(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)               # git object files are read-only on Windows
    func(path)


def remove_workspace(path: str) -> dict:
    """Delete one workspace directory, refusing anything that looks like a mistake (a drive root, the home directory, a parent of
    the working directory, or a path too shallow to be a data directory)."""
    p = Path(path).resolve()
    if len(p.parts) < 3 or p == Path.home() or p.anchor == str(p):
        raise TenantError(f"refusing to delete '{p}': not a tenant workspace directory")
    cwd = Path.cwd().resolve()
    if p == cwd or p in cwd.parents:
        raise TenantError(f"refusing to delete '{p}': it contains the current working directory")
    existed = p.exists()
    before = dir_size(p) if existed else 0
    if existed:
        shutil.rmtree(p, onerror=_writable)
    return {"path": str(p), "bytes_before": before, "existed": existed, "exists_after": p.exists()}


class Tenants:
    def __init__(self, admin_dsn: str):
        self.dsn = admin_dsn

    def _connect(self):
        return psycopg.connect(self.dsn)

    @staticmethod
    def _resolve(c, ref: str) -> dict:
        try:
            r = c.execute(f"select {COLS} from tenants where id = %s", (str(uuid.UUID(str(ref))),)).fetchone()
        except ValueError:
            r = c.execute(f"select {COLS} from tenants where name = %s", (ref,)).fetchone()
        if not r:
            raise TenantError(f"unknown tenant '{ref}'")
        return _row(r)

    @staticmethod
    def _event(c, tenant_id: str, event: str, **detail) -> None:
        c.execute("insert into tenant_events (subject_tenant, event, detail) values (%s, %s, %s)", (tenant_id, event, Jsonb(detail)))

    # ---- provisioning --------------------------------------------------------------------------------------------
    def create(self, name: str, display_name: Optional[str] = None, plan: str = "standard",
               storage_quota_bytes: Optional[int] = None) -> dict:
        if not NAME_RE.match(name or ""):
            raise TenantError("tenant name must be 1-63 characters: letters, digits, '.', '_' or '-', starting with a letter or digit")
        if storage_quota_bytes is not None and storage_quota_bytes < 0:
            raise TenantError("storage quota cannot be negative")
        with self._connect() as c:
            if c.execute("select 1 from tenants where name = %s", (name,)).fetchone():
                raise TenantError(f"tenant '{name}' already exists (names are never reused, including offboarded tenants)")
            r = c.execute(f"insert into tenants (name, display_name, plan, storage_quota_bytes) values (%s, %s, %s, %s) returning {COLS}",
                          (name, display_name, plan, storage_quota_bytes)).fetchone()
            t = _row(r)
            self._event(c, t["id"], "tenant_created", name=name, plan=plan, storage_quota_bytes=storage_quota_bytes)
        return t

    def get(self, ref: str) -> dict:
        with self._connect() as c:
            return self._resolve(c, ref)

    def list(self) -> list:
        with self._connect() as c:
            return [_row(r) for r in c.execute(f"select {COLS} from tenants order by name")]

    def events(self, ref: str) -> list:
        with self._connect() as c:
            t = self._resolve(c, ref)
            return [{"id": i, "ts": ts.isoformat(timespec="seconds"), "event": e, "detail": d} for i, ts, e, d in c.execute(
                "select id, ts, event, detail from tenant_events where subject_tenant = %s order by id", (t["id"],))]

    # ---- suspension ----------------------------------------------------------------------------------------------
    def suspend(self, ref: str, reason: str) -> dict:
        if not (reason or "").strip():
            raise TenantError("a reason is required to suspend a tenant")
        with self._connect() as c:
            t = self._resolve(c, ref)
            if t["status"] != "active":
                raise TenantError(f"tenant '{t['name']}' is {t['status']}; only an active tenant can be suspended")
            c.execute("update tenants set status = 'suspended', suspended_at = now(), suspended_reason = %s where id = %s", (reason.strip(), t["id"]))
            self._event(c, t["id"], "tenant_suspended", reason=reason.strip())
            return self._resolve(c, t["id"])

    def resume(self, ref: str) -> dict:
        with self._connect() as c:
            t = self._resolve(c, ref)
            if t["status"] != "suspended":
                raise TenantError(f"tenant '{t['name']}' is {t['status']}; only a suspended tenant can be resumed")
            c.execute("update tenants set status = 'active', suspended_at = null, suspended_reason = null where id = %s", (t["id"],))
            self._event(c, t["id"], "tenant_resumed")
            return self._resolve(c, t["id"])

    # ---- storage quota ---------------------------------------------------------------------------------------------
    def set_quota(self, ref: str, storage_quota_bytes: Optional[int]) -> dict:
        if storage_quota_bytes is not None and storage_quota_bytes < 0:
            raise TenantError("storage quota cannot be negative")
        with self._connect() as c:
            t = self._resolve(c, ref)
            if t["status"] in ("offboarding", "offboarded"):
                raise TenantError(f"tenant '{t['name']}' is {t['status']}")
            c.execute("update tenants set storage_quota_bytes = %s where id = %s", (storage_quota_bytes, t["id"]))
            self._event(c, t["id"], "quota_changed", old=t["storage_quota_bytes"], new=storage_quota_bytes)
            return self._resolve(c, t["id"])

    def usage(self, ref: str, workspace_dirs=()) -> dict:
        """What the tenant stores: database row bytes per table (row sizes, excluding indexes) and workspace bytes on disk."""
        with self._connect() as c:
            t = self._resolve(c, ref)
            c.execute("select set_config('tia.tenant_id', %s, false)", (t["id"],))
            tables = {}
            for name in tenant_tables(c):
                rows, size = c.execute(f'select count(*), coalesce(sum(pg_column_size(x.*)), 0)::bigint from "{name}" x '
                                       "where tenant_id = %s", (t["id"],)).fetchone()
                tables[name] = {"rows": rows, "bytes": size}
        ws = {str(p): (dir_size(p) if Path(p).exists() else 0) for p in workspace_dirs}
        db_bytes, ws_bytes = sum(v["bytes"] for v in tables.values()), sum(ws.values())
        quota = t["storage_quota_bytes"]
        return {"tenant": t["name"], "database": tables, "database_bytes": db_bytes, "workspace": ws, "workspace_bytes": ws_bytes,
                "total_bytes": db_bytes + ws_bytes, "quota_bytes": quota,
                "used_fraction": round((db_bytes + ws_bytes) / quota, 4) if quota else None}

    # ---- SCM installation mapping ------------------------------------------------------------------------------------
    def map_installation(self, ref: str, provider: str, installation_id: str, account: Optional[str] = None) -> dict:
        if provider not in PROVIDERS:
            raise TenantError(f"provider must be one of {', '.join(PROVIDERS)}")
        if not str(installation_id).strip():
            raise TenantError("installation id is required")
        installation_id = str(installation_id).strip()
        with self._connect() as c:
            t = self._resolve(c, ref)
            if t["status"] in ("offboarding", "offboarded"):
                raise TenantError(f"tenant '{t['name']}' is {t['status']}")
            c.execute("insert into tenant_installations (provider, installation_id, tenant_id, account) values (%s, %s, %s, %s) "
                      "on conflict (provider, installation_id) do nothing", (provider, installation_id, t["id"], account))
            owner = c.execute("select tenant_id from tenant_installations where provider = %s and installation_id = %s",
                              (provider, installation_id)).fetchone()[0]
            if str(owner) != t["id"]:
                other = c.execute("select name from tenants where id = %s", (owner,)).fetchone()[0]
                raise TenantError(f"{provider} installation {installation_id} already belongs to tenant '{other}'")
            self._event(c, t["id"], "installation_mapped", provider=provider, installation_id=installation_id, account=account)
        return {"tenant": t["name"], "provider": provider, "installation_id": installation_id, "account": account}

    def unmap_installation(self, ref: str, provider: str, installation_id: str) -> None:
        with self._connect() as c:
            t = self._resolve(c, ref)
            n = c.execute("delete from tenant_installations where provider = %s and installation_id = %s and tenant_id = %s",
                          (provider, str(installation_id), t["id"])).rowcount
            if not n:
                raise TenantError(f"{provider} installation {installation_id} is not mapped to tenant '{t['name']}'")
            self._event(c, t["id"], "installation_unmapped", provider=provider, installation_id=str(installation_id))

    def installations(self, ref: str) -> list:
        with self._connect() as c:
            t = self._resolve(c, ref)
            return [{"provider": p, "installation_id": i, "account": a} for p, i, a in c.execute(
                "select provider, installation_id, account from tenant_installations where tenant_id = %s order by 1, 2", (t["id"],))]

    def tenant_for_installation(self, provider: str, installation_id: str) -> Optional[dict]:
        """The tenant an SCM installation belongs to, with its status so the receiver can refuse suspended/offboarded ones."""
        with self._connect() as c:
            r = c.execute("select t.id, t.name, t.status from tenant_installations i join tenants t on t.id = i.tenant_id "
                          "where i.provider = %s and i.installation_id = %s", (provider, str(installation_id))).fetchone()
        return {"id": str(r[0]), "name": r[1], "status": r[2]} if r else None

    # ---- configuration (the same versioned store the tenant API uses; for provisioning and infrastructure-as-code) --------------------
    def config_show(self, ref: str) -> dict:
        from . import tenantconfig
        with self._connect() as c:
            t = self._resolve(c, ref)
            cfg = tenantconfig.latest(c, t["id"])
        return {"tenant": t["name"], "id": cfg.id, "version": cfg.version, "config": cfg.to_dict()}

    def config_set(self, ref: str, raw: dict, comment: Optional[str] = None, actor: Optional[str] = None) -> dict:
        from . import tenantconfig
        with self._connect() as c:
            t = self._resolve(c, ref)
            if t["status"] in ("offboarding", "offboarded"):
                raise TenantError(f"tenant '{t['name']}' is {t['status']}")
            try:
                r = tenantconfig.put_version(c, t["id"], raw, comment, actor or "admin-cli")
            except tenantconfig.ConfigError as e:
                raise TenantError("invalid configuration: " + "; ".join(e.problems))
            if r["created"]:
                self._event(c, t["id"], "config_changed", version=r["version"], actor=actor or "admin-cli", changes=[x["path"] for x in r["changes"]])
        return r

    # ---- identity: the first identity provider and a break-glass token are provisioned here (the API needs a signed-in administrator) --------
    def _identity(self, ref: str):
        from .db import Database
        from .identity import IdentityStore
        with self._connect() as c:
            t = self._resolve(c, ref)
        if t["status"] != "active":
            raise TenantError(f"tenant '{t['name']}' is {t['status']}")
        if getattr(self, "_db", None) is None:
            self._db = Database(self.dsn, max_size=2)
        return IdentityStore(self._db, t["id"]), t

    def idp_set(self, ref: str, raw: dict, actor: Optional[str] = None) -> dict:
        """Create or update (by name) an identity provider: {name, protocol, config, enabled, client_secret_ref}."""
        from .identity import AuthError, IdentityConfigError
        ids, t = self._identity(ref)
        try:
            cur = ids.idp_by_name(raw.get("name", ""))
            row = ids.put_idp(raw.get("name", ""), raw.get("protocol", "oidc"), raw.get("config", {}), raw.get("client_secret_ref"),
                              raw.get("enabled", True), actor=actor or "admin-cli", idp_id=cur["id"] if cur else None)
        except IdentityConfigError as e:
            raise TenantError("invalid identity provider: " + "; ".join(e.problems))
        except AuthError as e:
            raise TenantError(f"identity provider could not be saved ({e.code})")
        with self._connect() as c:
            self._event(c, t["id"], "idp_set", idp=row["name"], protocol=row["protocol"], actor=actor or "admin-cli")
        return row

    def idp_list(self, ref: str) -> list:
        return self._identity(ref)[0].list_idps()

    def token_create(self, ref: str, name: str, roles: list, repos="*", days: Optional[int] = 90, allow_administrator: bool = False,
                     actor: Optional[str] = None) -> dict:
        from .identity import IdentityConfigError
        ids, t = self._identity(ref)
        try:
            tok = ids.create_token(name, roles, repos, days, created_by=actor or "admin-cli", allow_administrator=allow_administrator)
        except IdentityConfigError as e:
            raise TenantError("invalid token: " + "; ".join(e.problems))
        with self._connect() as c:
            self._event(c, t["id"], "service_token_created", name=name, roles=roles, actor=actor or "admin-cli")
        return tok

    def token_list(self, ref: str) -> list:
        return self._identity(ref)[0].list_tokens()

    def token_revoke(self, ref: str, token_id: str) -> dict:
        ids, t = self._identity(ref)
        ok = ids.revoke_token(token_id)
        if not ok:
            raise TenantError("unknown or already revoked token")
        with self._connect() as c:
            self._event(c, t["id"], "service_token_revoked", token=token_id)
        return {"revoked": token_id}

    # ---- offboarding -------------------------------------------------------------------------------------------------
    def verify_deletion(self, ref: str, workspace_dirs=()) -> dict:
        """Independent check: no row of the tenant remains in any table, and the workspace directories are gone."""
        with self._connect() as c:
            t = self._resolve(c, ref)
            c.execute("select set_config('tia.tenant_id', %s, false)", (t["id"],))
            remaining = {}
            for name in tenant_tables(c):
                n = c.execute(f'select count(*) from "{name}" where tenant_id = %s', (t["id"],)).fetchone()[0]
                if n:
                    remaining[name] = n
        left = [str(Path(p).resolve()) for p in workspace_dirs if Path(p).exists()]
        return {"clean": not remaining and not left, "remaining_rows": remaining, "remaining_paths": left}

    def offboard(self, ref: str, confirm_name: str, workspace_dirs=()) -> dict:
        with self._connect() as c:
            t = self._resolve(c, ref)
            if confirm_name != t["name"]:
                raise TenantError("the confirmation does not match the tenant name; nothing was deleted")
            if t["status"] == "offboarded":
                cert = c.execute("select deletion_certificate from tenants where id = %s", (t["id"],)).fetchone()[0]
                return cert                                         # idempotent: the certificate already issued
            requested = c.execute("select ts from tenant_events where subject_tenant = %s and event = 'offboarding_started' "
                                  "order by id desc limit 1", (t["id"],)).fetchone()
            if t["status"] != "offboarding":
                c.execute("update tenants set status = 'offboarding' where id = %s", (t["id"],))
                self._event(c, t["id"], "offboarding_started", previous_status=t["status"], workspace=[str(Path(p).resolve()) for p in workspace_dirs])
                requested = c.execute("select ts from tenant_events where subject_tenant = %s and event = 'offboarding_started' "
                                      "order by id desc limit 1", (t["id"],)).fetchone()
            c.commit()                                              # the tenant is blocked from here on, whatever happens next
            c.execute("select set_config('tia.tenant_id', %s, false)", (t["id"],))
            tables = {}
            for name in tenant_tables(c):
                before = c.execute(f'select count(*) from "{name}" where tenant_id = %s', (t["id"],)).fetchone()[0]
                tables[name] = {"rows_before": before}
            head = c.execute("select seq, hash from audit_log where tenant_id = %s order by seq desc limit 1", (t["id"],)).fetchone()
            for name in tables:
                tables[name]["rows_deleted"] = c.execute(f'delete from "{name}" where tenant_id = %s', (t["id"],)).rowcount
            c.commit()
            workspace = [remove_workspace(p) for p in workspace_dirs]
            check = self.verify_deletion(t["id"], workspace_dirs)
            if not check["clean"]:
                raise TenantError(f"deletion could not be verified (still present: {check}); the tenant stays 'offboarding' and the "
                                  "operation can be re-run")
            for name in tables:
                tables[name]["rows_remaining"] = 0
            cert = {"version": 1, "tenant_id": t["id"], "tenant_name": t["name"],
                    "requested_at": requested[0].isoformat(timespec="seconds") if requested else None, "completed_at": _now(),
                    "tables": tables, "workspace": workspace,
                    "audit_chain": {"entries": head[0] if head else 0, "head_hash": head[1] if head else None},
                    "retained": {"tenants": "registry tombstone: id, name, status and this certificate",
                                 "tenant_events": "lifecycle metadata only (no tenant content)"}}
            cert["certificate_hash"] = hashlib.sha256(json.dumps(cert, sort_keys=True).encode()).hexdigest()
            c.execute("update tenants set status = 'offboarded', offboarded_at = now(), deletion_certificate = %s, "
                      "suspended_reason = null where id = %s", (Jsonb(cert), t["id"]))
            self._event(c, t["id"], "offboarded", certificate_hash=cert["certificate_hash"],
                        rows_deleted=sum(v["rows_deleted"] for v in tables.values()))
            c.commit()
        return cert


def certificate_valid(cert: dict) -> bool:
    body = {k: v for k, v in cert.items() if k != "certificate_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() == cert.get("certificate_hash")


# ---- command line --------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tia.tenants", description="TIA tenant lifecycle (admin credentials).")
    ap.add_argument("--admin-dsn", default=os.environ.get("TIA_ADMIN_DATABASE_URL"), help="admin DSN (default: $TIA_ADMIN_DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create"); p.add_argument("name"); p.add_argument("--display-name"); p.add_argument("--plan", default="standard")
    p.add_argument("--quota-bytes", type=int)
    sub.add_parser("list")
    for cmd in ("show", "events", "resume", "installations"):
        sub.add_parser(cmd).add_argument("tenant")
    p = sub.add_parser("suspend"); p.add_argument("tenant"); p.add_argument("--reason", required=True)
    p = sub.add_parser("quota"); p.add_argument("tenant"); p.add_argument("bytes", help="bytes, or 'none' for unlimited")
    p = sub.add_parser("usage"); p.add_argument("tenant"); p.add_argument("--workspace", action="append", default=[])
    for cmd in ("map-installation", "unmap-installation"):
        p = sub.add_parser(cmd); p.add_argument("tenant"); p.add_argument("provider", choices=PROVIDERS); p.add_argument("installation_id")
        if cmd == "map-installation":
            p.add_argument("--account")
    p = sub.add_parser("offboard", help="irreversibly delete the tenant's data and issue a deletion certificate")
    p.add_argument("tenant"); p.add_argument("--confirm", required=True, help="the tenant's name, typed in full")
    p.add_argument("--workspace", action="append", default=[], help="a workspace directory to delete (repeatable)")
    sub.add_parser("config-show").add_argument("tenant")
    p = sub.add_parser("config-set", help="save a new configuration version from a JSON file")
    p.add_argument("tenant"); p.add_argument("--file", required=True); p.add_argument("--comment"); p.add_argument("--actor")
    p = sub.add_parser("idp-set", help="create or update an identity provider (OIDC or SAML) from a JSON file")
    p.add_argument("tenant"); p.add_argument("--file", required=True); p.add_argument("--actor")
    sub.add_parser("idp-list").add_argument("tenant")
    p = sub.add_parser("token-create", help="create a service-account token (shown once)")
    p.add_argument("tenant"); p.add_argument("--name", required=True); p.add_argument("--roles", required=True, help="comma-separated")
    p.add_argument("--repos", default="*", help='"*" or comma-separated repository names'); p.add_argument("--days", type=int, default=90)
    p.add_argument("--allow-administrator", action="store_true", help="break-glass: allow the administrator role on a token")
    p.add_argument("--actor")
    sub.add_parser("token-list").add_argument("tenant")
    p = sub.add_parser("token-revoke"); p.add_argument("tenant"); p.add_argument("token_id")
    p = sub.add_parser("verify-deletion"); p.add_argument("tenant"); p.add_argument("--workspace", action="append", default=[])
    a = ap.parse_args(argv)
    if not a.admin_dsn:
        print("an admin DSN is required (--admin-dsn or TIA_ADMIN_DATABASE_URL)", file=sys.stderr)
        return 2
    svc = Tenants(a.admin_dsn)
    try:
        out = {
            "create": lambda: svc.create(a.name, a.display_name, a.plan, a.quota_bytes), "list": svc.list,
            "show": lambda: svc.get(a.tenant), "events": lambda: svc.events(a.tenant), "resume": lambda: svc.resume(a.tenant),
            "installations": lambda: svc.installations(a.tenant), "suspend": lambda: svc.suspend(a.tenant, a.reason),
            "quota": lambda: svc.set_quota(a.tenant, None if a.bytes.lower() == "none" else int(a.bytes)),
            "usage": lambda: svc.usage(a.tenant, a.workspace),
            "map-installation": lambda: svc.map_installation(a.tenant, a.provider, a.installation_id, a.account),
            "unmap-installation": lambda: svc.unmap_installation(a.tenant, a.provider, a.installation_id) or {"unmapped": True},
            "offboard": lambda: svc.offboard(a.tenant, a.confirm, a.workspace),
            "config-show": lambda: svc.config_show(a.tenant),
            "config-set": lambda: svc.config_set(a.tenant, json.load(open(a.file, encoding="utf-8")), a.comment, a.actor),
            "idp-set": lambda: svc.idp_set(a.tenant, json.load(open(a.file, encoding="utf-8")), a.actor),
            "idp-list": lambda: svc.idp_list(a.tenant),
            "token-create": lambda: svc.token_create(a.tenant, a.name, [r.strip() for r in a.roles.split(",") if r.strip()],
                                                      "*" if a.repos == "*" else [r.strip() for r in a.repos.split(",") if r.strip()], a.days,
                                                      a.allow_administrator, a.actor),
            "token-list": lambda: svc.token_list(a.tenant), "token-revoke": lambda: svc.token_revoke(a.tenant, a.token_id),
            "verify-deletion": lambda: svc.verify_deletion(a.tenant, a.workspace)}[a.cmd]()
    except (TenantError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
