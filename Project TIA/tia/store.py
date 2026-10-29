"""Tenant-scoped persistence on PostgreSQL: evidence cache, analyses, repository registry, audit chain.

Every method runs in a transaction bound to this store's tenant; row-level security (not this code)
is what prevents cross-tenant access.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Jsonb

from .db import Database
from .models import RepoExtract

GENESIS = "GENESIS"


def _chain_hash(prev: str, seq: int, ts: str, event: str, payload: str, key: Optional[bytes] = None) -> str:
    msg = f"{prev}|{seq}|{ts}|{event}|{payload}".encode("utf-8")
    # With a key held outside the database, someone who can edit rows cannot also recompute a valid chain.
    return hmac.new(key, msg, hashlib.sha256).hexdigest() if key else hashlib.sha256(msg).hexdigest()


class Store:
    def __init__(self, db: Database, tenant_id: str, audit_key: Optional[bytes] = None):
        self.db = db
        self.tenant_id = str(tenant_id)
        self.audit_key = audit_key

    # ---- evidence cache (incremental analysis: unchanged revisions are not re-parsed) -----------------
    @staticmethod
    def _key(repo: str, commit: str, version: str) -> str:
        return f"{repo}@{commit}#{version}"

    def get_extract(self, repo: str, commit: str, version: str) -> Optional[RepoExtract]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select data from extractions where key = %s", (self._key(repo, commit, version),)).fetchone()
        return RepoExtract.from_dict(r[0]) if r else None

    def put_extract(self, ex: RepoExtract):
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into extractions (tenant_id, key, data) values (%s, %s, %s) "
                      "on conflict (tenant_id, key) do update set data = excluded.data",
                      (self.tenant_id, self._key(ex.repo, ex.commit, ex.extractor_version), Jsonb(ex.to_dict())))

    # ---- immutable analysis results ------------------------------------------------------------------------
    def put_analysis(self, report: dict):
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into analyses (tenant_id, id, data) values (%s, %s, %s) "
                      "on conflict (tenant_id, id) do update set data = excluded.data, created_at = now()",
                      (self.tenant_id, report["analysis_id"], Jsonb(report)))

    def get_analysis(self, analysis_id: str) -> Optional[dict]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select data from analyses where id = %s", (analysis_id,)).fetchone()
        return r[0] if r else None

    # ---- repository registry -----------------------------------------------------------------------------
    def put_repo(self, repo):
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into repositories (tenant_id, name, data) values (%s, %s, %s) "
                      "on conflict (tenant_id, name) do update set data = excluded.data",
                      (self.tenant_id, repo.name, Jsonb(repo.public())))

    def get_repo(self, name: str):
        from .repos import Repo
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select data from repositories where name = %s", (name,)).fetchone()
        return Repo(**r[0]) if r else None

    def list_repos(self) -> list:
        from .repos import Repo
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select data from repositories order by name").fetchall()
        return [Repo(**r[0]) for r in rows]

    def delete_repo(self, name: str):
        with self.db.tx(self.tenant_id) as c:
            c.execute("delete from repositories where name = %s", (name,))

    # ---- audit: append-only, hash-chained per tenant ----------------------------------------------------------
    def audit(self, event: str, **fields):
        payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.db.tx(self.tenant_id) as c:
            c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (self.tenant_id,))
            last = c.execute("select seq, hash from audit_log order by seq desc limit 1").fetchone()
            seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
            c.execute("insert into audit_log (tenant_id, seq, ts, event, payload, prev_hash, hash) values (%s,%s,%s,%s,%s,%s,%s)",
                      (self.tenant_id, seq, ts, event, payload, prev, _chain_hash(prev, seq, ts, event, payload, self.audit_key)))

    def audit_events(self, limit: int = 100) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select seq, ts, event, payload from audit_log order by seq desc limit %s", (limit,)).fetchall()
        return [{"seq": s, "ts": t, "event": e, **json.loads(p)} for s, t, e, p in rows]

    def verify_audit(self) -> dict:
        """Recompute the whole chain. ok=False names the first sequence number that does not verify."""
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select seq, ts, event, payload, prev_hash, hash from audit_log order by seq").fetchall()
        prev, expect = GENESIS, 1
        for seq, ts, event, payload, ph, h in rows:
            if seq != expect or ph != prev or h != _chain_hash(prev, seq, ts, event, payload, self.audit_key):
                return {"ok": False, "entries": len(rows), "first_bad_seq": seq}
            prev, expect = h, expect + 1
        return {"ok": True, "entries": len(rows), "first_bad_seq": None}
