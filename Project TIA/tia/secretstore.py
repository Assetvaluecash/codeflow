"""Per-tenant secret storage with envelope encryption (PL-011; the key hierarchy of PL-016 without customer-managed keys).

    key-encryption key (KEK)   in a secrets manager / KMS (credentials.KeyWrapper); never stored by TIA
      wraps -> per-tenant data-encryption key (DEK), versioned, stored only wrapped (tenant_keys)
        encrypts -> each secret, AES-256-GCM, nonce || ciphertext (tenant_secrets)

The tenant id, the secret name and the key version are bound into every ciphertext as authenticated data, so a row copied to another
tenant or under another name fails its integrity check instead of decrypting. Values are returned only by get(); listing returns
names. Plaintext is never logged or audited (audit records carry the name and the action).

Rotation: rotate() makes a new DEK version, re-encrypts every secret under it in one transaction and deletes the old DEK (so a stale
copy of the old ciphertext is unreadable). rewrap_keys() re-wraps the DEKs under the current KEK after the KEK was rotated.

python -m tia.secretstore  administers it from the command line; values are read from standard input, never from arguments.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .credentials import KeyWrapper, LocalKeyWrapper, SecretError, keywrapper_from_env

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
DEK_CACHE_SECONDS = 300.0


def _aad_key(tenant_id: str, version: int) -> bytes:
    return f"tia:dek:{tenant_id}:{version}".encode()


def _aad_secret(tenant_id: str, name: str, version: int) -> bytes:
    return f"tia:secret:{tenant_id}:{name}:{version}".encode()


class SecretStore:
    def __init__(self, db, tenant_id: str, wrapper: KeyWrapper, audit=None):
        """db: Database (tenant-bound transactions). audit: optional callable(event, **fields) - receives names, never values."""
        self.db, self.tenant_id, self.wrapper, self._audit = db, str(tenant_id), wrapper, audit
        self._deks: dict = {}
        self._lock = threading.Lock()

    def _log(self, event: str, **fields):
        if self._audit:
            self._audit(event, **fields)

    # ---- keys ----------------------------------------------------------------------------------------------------
    def _load_dek(self, conn, version: int) -> bytes:
        now = time.monotonic()
        with self._lock:
            hit = self._deks.get(version)
            if hit and now - hit[0] < DEK_CACHE_SECONDS:
                return hit[1]
        row = conn.execute("select wrapped_dek from tenant_keys where tenant_id = %s and version = %s", (self.tenant_id, version)).fetchone()
        if not row:
            raise SecretError(f"data key version {version} of this tenant does not exist")
        dek = self.wrapper.unwrap(row[0], _aad_key(self.tenant_id, version))
        with self._lock:
            self._deks[version] = (now, dek)
        return dek

    def _current(self, conn) -> tuple:
        """(version, dek) of the newest data key; creates version 1 on first use."""
        row = conn.execute("select max(version) from tenant_keys where tenant_id = %s", (self.tenant_id,)).fetchone()
        if row and row[0] is not None:
            return row[0], self._load_dek(conn, row[0])
        dek = os.urandom(32)
        conn.execute("insert into tenant_keys (tenant_id, version, wrapped_dek, kek_id) values (%s, 1, %s, %s) on conflict do nothing",
                     (self.tenant_id, self.wrapper.wrap(dek, _aad_key(self.tenant_id, 1)), self.wrapper.id))
        v = conn.execute("select max(version) from tenant_keys where tenant_id = %s", (self.tenant_id,)).fetchone()[0]
        return v, self._load_dek(conn, v)                       # re-read: another process may have won the race

    # ---- secrets ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _check_name(name: str) -> str:
        if not NAME_RE.match(name or ""):
            raise SecretError("a secret name is 1-128 characters: lower-case letters, digits, '.', '_', '-' or '/', starting with a letter or digit")
        return name

    def put(self, name: str, value) -> None:
        self._check_name(name)
        data = value.encode() if isinstance(value, str) else bytes(value)
        if not data:
            raise SecretError("a secret cannot be empty")
        with self.db.tx(self.tenant_id) as conn:
            version, dek = self._current(conn)
            nonce = os.urandom(12)
            blob = nonce + AESGCM(dek).encrypt(nonce, data, _aad_secret(self.tenant_id, name, version))
            conn.execute("insert into tenant_secrets (tenant_id, name, key_version, ciphertext) values (%s, %s, %s, %s) "
                         "on conflict (tenant_id, name) do update set key_version = excluded.key_version, "
                         "ciphertext = excluded.ciphertext, updated_at = now()", (self.tenant_id, name, version, blob))
        self._log("secret_stored", name=name)

    def get(self, name: str) -> Optional[bytes]:
        with self.db.tx(self.tenant_id) as conn:
            row = conn.execute("select key_version, ciphertext from tenant_secrets where tenant_id = %s and name = %s", (self.tenant_id, name)).fetchone()
            if not row:
                return None
            version, blob = row[0], bytes(row[1])
            dek = self._load_dek(conn, version)
        try:
            return AESGCM(dek).decrypt(blob[:12], blob[12:], _aad_secret(self.tenant_id, name, version))
        except Exception:
            raise SecretError(f"secret '{name}' failed its integrity check")

    def get_text(self, name: str) -> Optional[str]:
        v = self.get(name)
        return v.decode() if v is not None else None

    def delete(self, name: str) -> bool:
        with self.db.tx(self.tenant_id) as conn:
            n = conn.execute("delete from tenant_secrets where tenant_id = %s and name = %s", (self.tenant_id, name)).rowcount
        if n:
            self._log("secret_deleted", name=name)
        return bool(n)

    def names(self) -> list:
        with self.db.tx(self.tenant_id) as conn:
            return [r[0] for r in conn.execute("select name from tenant_secrets where tenant_id = %s order by name", (self.tenant_id,))]

    # ---- rotation ----------------------------------------------------------------------------------------------------
    def rotate(self) -> dict:
        """New data key; every secret re-encrypted under it atomically; old data keys deleted."""
        with self.db.tx(self.tenant_id) as conn:
            conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 7))", (self.tenant_id,))
            old, old_dek = self._current(conn)
            version = old + 1
            dek = os.urandom(32)
            conn.execute("insert into tenant_keys (tenant_id, version, wrapped_dek, kek_id) values (%s, %s, %s, %s)",
                         (self.tenant_id, version, self.wrapper.wrap(dek, _aad_key(self.tenant_id, version)), self.wrapper.id))
            n = 0
            for name, kv, blob in conn.execute("select name, key_version, ciphertext from tenant_secrets where tenant_id = %s for update", (self.tenant_id,)).fetchall():
                blob = bytes(blob)
                try:
                    plain = AESGCM(self._load_dek(conn, kv)).decrypt(blob[:12], blob[12:], _aad_secret(self.tenant_id, name, kv))
                except Exception:
                    raise SecretError(f"secret '{name}' failed its integrity check; rotation aborted, nothing changed")
                nonce = os.urandom(12)
                conn.execute("update tenant_secrets set key_version = %s, ciphertext = %s, updated_at = now() where tenant_id = %s and name = %s",
                             (version, nonce + AESGCM(dek).encrypt(nonce, plain, _aad_secret(self.tenant_id, name, version)), self.tenant_id, name))
                n += 1
            conn.execute("delete from tenant_keys where tenant_id = %s and version < %s", (self.tenant_id, version))
        with self._lock:
            self._deks = {}
        self._log("secret_keys_rotated", version=version, secrets=n)
        return {"key_version": version, "secrets_reencrypted": n}

    def rewrap_keys(self) -> int:
        """Re-wrap this tenant's data keys under the current key-encryption key; returns how many were re-wrapped."""
        done = 0
        with self.db.tx(self.tenant_id) as conn:
            for version, wrapped, kek in conn.execute("select version, wrapped_dek, kek_id from tenant_keys where tenant_id = %s for update", (self.tenant_id,)).fetchall():
                if kek == self.wrapper.id:
                    continue
                dek = self.wrapper.unwrap(wrapped, _aad_key(self.tenant_id, version))
                conn.execute("update tenant_keys set wrapped_dek = %s, kek_id = %s where tenant_id = %s and version = %s",
                             (self.wrapper.wrap(dek, _aad_key(self.tenant_id, version)), self.wrapper.id, self.tenant_id, version))
                done += 1
        if done:
            self._log("secret_keys_rewrapped", count=done)
        return done


def for_tenant(db, tenant_id: str, audit=None) -> Optional[SecretStore]:
    """The runtime's secret store, or None when no key-encryption key is configured."""
    w = keywrapper_from_env()
    return SecretStore(db, tenant_id, w, audit) if w else None


# ---- command line ---------------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    import json

    from .db import Database
    from .tenants import TenantError, Tenants
    ap = argparse.ArgumentParser(prog="python -m tia.secretstore", description="Per-tenant secrets (admin credentials). "
                                 "Values are read from standard input.")
    ap.add_argument("--admin-dsn", default=os.environ.get("TIA_ADMIN_DATABASE_URL"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate-master-key", help="print a new 256-bit master key (base64)")
    for c in ("put", "list", "delete", "rotate", "rewrap"):
        p = sub.add_parser(c)
        p.add_argument("tenant")
        if c in ("put", "delete"):
            p.add_argument("name")
    a = ap.parse_args(argv)
    if a.cmd == "generate-master-key":
        print(LocalKeyWrapper.generate())
        return 0
    if not a.admin_dsn:
        print("an admin DSN is required (--admin-dsn or TIA_ADMIN_DATABASE_URL)", file=sys.stderr)
        return 2
    try:
        wrapper = keywrapper_from_env()
        if wrapper is None:
            print("no key-encryption key is configured (TIA_MASTER_KEY, or TIA_KEK_PROVIDER=vault-transit)", file=sys.stderr)
            return 2
        t = Tenants(a.admin_dsn).get(a.tenant)
        if t["status"] in ("offboarding", "offboarded"):
            raise TenantError(f"tenant '{t['name']}' is {t['status']}")
        db = Database(a.admin_dsn, max_size=2)
        try:
            st = SecretStore(db, t["id"], wrapper)
            if a.cmd == "put":
                st.put(a.name, sys.stdin.read().rstrip("\r\n"))
                out = {"stored": a.name}
            elif a.cmd == "list":
                out = {"names": st.names()}
            elif a.cmd == "delete":
                out = {"deleted": st.delete(a.name)}
            elif a.cmd == "rotate":
                out = st.rotate()
            else:
                out = {"rewrapped": st.rewrap_keys()}
        finally:
            db.close()
    except (SecretError, TenantError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
