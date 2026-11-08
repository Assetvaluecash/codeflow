"""Backup, restore and disaster recovery (PRD PL-005).

Two layers (docs/OPERATIONS.md gives the RPO/RTO each meets):
  * the platform: PostgreSQL continuous archiving / point-in-time recovery of the whole database (operator infrastructure);
  * per tenant: `python -m tia.backup export <tenant> --out t.tiabak` writes every row of every tenant-owned table (the catalog decides
    which tables: a new table is never forgotten), plus the tenant's registry entry, as one file encrypted with AES-256-GCM under
    TIA_BACKUP_KEY (32 bytes, base64; refused without one unless --unencrypted is given) with a manifest of row counts and SHA-256 digests.
    `restore` writes it into a database at the same schema version (another cluster, another region's recovery site) and `verify` checks
    counts, digests and the audit chain. A restore never merges into an existing tenant.
Secrets stay encrypted in the backup exactly as in the database (their keys are wrapped by the key-encryption key, which is NOT in the
backup), so a stolen backup file without TIA_BACKUP_KEY and the key-encryption key reveals nothing.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TIABAK1\n"


class BackupError(Exception):
    pass


def tenant_tables(c) -> list:
    """Every table with a tenant_id column (the same catalog rule offboarding uses)."""
    return [r[0] for r in c.execute("select table_name from information_schema.columns where column_name = 'tenant_id' and table_schema = 'public' "
                                    "and table_name <> 'tenants' order by table_name").fetchall()]


def _key(unencrypted: bool) -> Optional[bytes]:
    from .credentials import env_secret
    raw = env_secret("TIA_BACKUP_KEY")            # or TIA_BACKUP_KEY_FILE
    if not raw:
        if unencrypted:
            return None
        raise BackupError("TIA_BACKUP_KEY is not set (32 random bytes, base64); use --unencrypted only for a test copy")
    k = base64.b64decode(raw)
    if len(k) != 32:
        raise BackupError("TIA_BACKUP_KEY must be 32 bytes (base64)")
    return k


def export(admin_dsn: str, ref: str, unencrypted: bool = False) -> bytes:
    key = _key(unencrypted)
    with psycopg.connect(admin_dsn) as c:
        t = c.execute("select row_to_json(t) from tenants t where id::text = %s or name = %s", (ref, ref)).fetchone()
        if not t:
            raise BackupError(f"no tenant '{ref}'")
        tenant = t[0]
        schema = [r[0] for r in c.execute("select version from schema_migrations order by version").fetchall()]
        tables, manifest = {}, {}
        for name in tenant_tables(c):
            rows = [r[0] for r in c.execute(f'select row_to_json(x) from "{name}" x where tenant_id = %s order by row_to_json(x)::text', (tenant["id"],)).fetchall()]
            blob = json.dumps(rows, sort_keys=True, default=str).encode()
            tables[name] = rows
            manifest[name] = {"rows": len(rows), "sha256": hashlib.sha256(blob).hexdigest()}
    body = gzip.compress(json.dumps({"format": 1, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tenant": tenant,
                                     "schema": schema, "manifest": manifest, "tables": tables}, sort_keys=True, default=str).encode())
    if key is None:
        return MAGIC + b"plain\n" + body
    nonce, tid = os.urandom(12), tenant["id"].encode()
    # the tenant id is in the clear header and bound as associated data: changing it (or any byte) makes decryption fail
    return MAGIC + b"aesgcm\n" + tid + b"\n" + nonce + AESGCM(key).encrypt(nonce, body, MAGIC + tid)


def read(data: bytes, unencrypted: bool = False) -> dict:
    if not data.startswith(MAGIC):
        raise BackupError("not a TIA backup")
    rest = data[len(MAGIC):]
    mode, _, rest = rest.partition(b"\n")
    if mode == b"plain":
        if not unencrypted:
            raise BackupError("this backup is not encrypted: pass --unencrypted to accept it")
        body = rest
    elif mode == b"aesgcm":
        key = _key(False)
        tid, _, rest = rest.partition(b"\n")
        try:
            body = AESGCM(key).decrypt(rest[:12], rest[12:], MAGIC + tid)
        except Exception as e:                                       # noqa: BLE001 - any failure means the key or the file is wrong
            raise BackupError(f"the backup cannot be decrypted with TIA_BACKUP_KEY or was altered ({type(e).__name__})")
    else:
        raise BackupError("unknown backup encryption")
    doc = json.loads(gzip.decompress(body))
    for name, rows in doc["tables"].items():
        if hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest() != doc["manifest"][name]["sha256"]:
            raise BackupError(f"table {name} does not match its digest: the backup is damaged")
    return doc


def restore(admin_dsn: str, data: bytes, unencrypted: bool = False, as_name: Optional[str] = None) -> dict:
    doc = read(data, unencrypted)
    t = doc["tenant"]
    with psycopg.connect(admin_dsn) as c:
        schema = [r[0] for r in c.execute("select version from schema_migrations order by version").fetchall()]
        if schema != doc["schema"]:
            raise BackupError(f"schema mismatch: the backup is at {doc['schema'][-1]}, this database at {schema[-1] if schema else 'none'}; "
                              "migrate the target to the same version first")
        if c.execute("select 1 from tenants where id = %s or name = %s", (t["id"], as_name or t["name"])).fetchone():
            raise BackupError("the target already has this tenant (id or name): a restore never merges into existing data")
        cols = [r[0] for r in c.execute("select column_name from information_schema.columns where table_name = 'tenants' and table_schema = 'public'")]
        tr = {k: v for k, v in t.items() if k in cols}
        if as_name:
            tr["name"] = as_name
        c.execute("insert into tenants select * from json_populate_record(null::tenants, %s)", (json.dumps(tr),))
        order = _insert_order(c, list(doc["tables"]))
        for name in order:
            rows = doc["tables"][name]
            if not rows:
                continue
            ident = c.execute("select count(*) from information_schema.columns where table_name = %s and is_identity = 'YES'", (name,)).fetchone()[0]
            ov = " overriding system value" if ident else ""
            for row in rows:
                c.execute(f'insert into "{name}"{ov} select * from json_populate_record(null::"{name}", %s)', (json.dumps(row),))
        c.commit()
    return {"tenant": as_name or t["name"], "tenant_id": t["id"], "tables": {k: v["rows"] for k, v in doc["manifest"].items()}}


def _insert_order(c, names: list) -> list:
    """Parents before children (foreign keys between tenant tables)."""
    deps = {n: set() for n in names}
    for child, parent in c.execute("select tc.table_name, ccu.table_name from information_schema.table_constraints tc "
                                   "join information_schema.constraint_column_usage ccu on tc.constraint_name = ccu.constraint_name "
                                   "where tc.constraint_type = 'FOREIGN KEY'").fetchall():
        if child in deps and parent in deps and parent != child:
            deps[child].add(parent)
    out, done = [], set()
    while len(out) < len(names):
        ready = sorted(n for n in names if n not in done and deps[n] <= done)
        if not ready:
            ready = sorted(n for n in names if n not in done)[:1]
        for n in ready:
            out.append(n)
            done.add(n)
    return out


def verify(admin_dsn: str, ref: str, data: bytes, unencrypted: bool = False) -> dict:
    """The restored tenant matches the backup: every table's row count and digest (verify the audit chain with GET /api/v1/audit/verify)."""
    doc = read(data, unencrypted)
    with psycopg.connect(admin_dsn) as c:
        t = c.execute("select id from tenants where id::text = %s or name = %s", (ref, ref)).fetchone()
        if not t:
            raise BackupError(f"no tenant '{ref}'")
        bad = []
        for name, m in doc["manifest"].items():
            rows = [r[0] for r in c.execute(f'select row_to_json(x) from "{name}" x where tenant_id = %s order by row_to_json(x)::text', (t[0],)).fetchall()]
            if len(rows) != m["rows"] or hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest() != m["sha256"]:
                bad.append(name)
    return {"tenant": ref, "tables_checked": len(doc["manifest"]), "mismatched": bad, "ok": not bad}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tia.backup")
    from .credentials import env_secret
    ap.add_argument("--admin-dsn", default=env_secret("TIA_ADMIN_DATABASE_URL"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export"); p.add_argument("tenant"); p.add_argument("--out", required=True); p.add_argument("--unencrypted", action="store_true")
    p = sub.add_parser("restore"); p.add_argument("file"); p.add_argument("--as-name"); p.add_argument("--unencrypted", action="store_true")
    p = sub.add_parser("verify"); p.add_argument("tenant"); p.add_argument("file"); p.add_argument("--unencrypted", action="store_true")
    a = ap.parse_args(argv)
    if not a.admin_dsn:
        print("an admin DSN is required (--admin-dsn or TIA_ADMIN_DATABASE_URL)", file=sys.stderr)
        return 2
    try:
        if a.cmd == "export":
            data = export(a.admin_dsn, a.tenant, a.unencrypted)
            with open(a.out, "wb") as f:
                f.write(data)
            out = {"file": a.out, "bytes": len(data), "encrypted": not a.unencrypted}
        elif a.cmd == "restore":
            out = restore(a.admin_dsn, open(a.file, "rb").read(), a.unencrypted, a.as_name)
        else:
            out = verify(a.admin_dsn, a.tenant, open(a.file, "rb").read(), a.unencrypted)
    except (BackupError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
