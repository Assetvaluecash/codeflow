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

    def get_file_extracts(self, keys: list) -> dict:
        """Per-file facts already extracted for these (blob-identified) keys."""
        if not keys:
            return {}
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select key, data from file_extracts where key = any(%s)", (list(keys),)).fetchall()
        return {k: RepoExtract.from_dict(d) for k, d in rows}

    def put_file_extracts(self, items: dict, overwrite: bool = False):
        if not items:
            return
        action = "do update set data = excluded.data" if overwrite else "do nothing"
        with self.db.tx(self.tenant_id) as c:
            for k, ex in items.items():
                c.execute("insert into file_extracts (tenant_id, key, data) values (%s, %s, %s) "
                          f"on conflict (tenant_id, key) {action}", (self.tenant_id, k, Jsonb(ex.to_dict())))

    # ---- persisted graph versions (immutable) and the current head --------------------------------------------
    def get_graph(self, graph_id: str) -> Optional[dict]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select data from graph_versions where id = %s", (graph_id,)).fetchone()
        return r[0] if r else None

    def put_graph(self, graph_id: str, inputs: dict, stats: dict, data: dict, overwrite: bool = False):
        action = "do update set stats = excluded.stats, data = excluded.data" if overwrite else "do nothing"
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into graph_versions (tenant_id, id, inputs, stats, data) values (%s, %s, %s, %s, %s) "
                      f"on conflict (tenant_id, id) {action}",
                      (self.tenant_id, graph_id, Jsonb(inputs), Jsonb(stats), Jsonb(data)))

    def list_graphs(self, limit: int = 50) -> list:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select id, created_at, inputs, stats from graph_versions order by created_at desc, id limit %s",
                             (limit,)).fetchall()
        return [{"id": i, "created_at": t.isoformat(), "inputs": inp, "stats": st} for i, t, inp, st in rows]

    def get_head(self, scope: str) -> Optional[dict]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select graph_id, previous_id, updated_at from graph_heads where scope = %s", (scope,)).fetchone()
        return {"graph_id": r[0], "previous_id": r[1], "updated_at": r[2].isoformat()} if r else None

    def set_head(self, scope: str, graph_id: str) -> bool:
        """Point the scope at a graph version; returns True if the head moved."""
        with self.db.tx(self.tenant_id) as c:
            cur = c.execute("select graph_id from graph_heads where scope = %s for update", (scope,)).fetchone()
            if cur and cur[0] == graph_id:
                return False
            c.execute("insert into graph_heads (tenant_id, scope, graph_id, previous_id) values (%s, %s, %s, %s) "
                      "on conflict (tenant_id, scope) do update set graph_id = excluded.graph_id, "
                      "previous_id = graph_heads.graph_id, updated_at = now()",
                      (self.tenant_id, scope, graph_id, cur[0] if cur else None))
        return True

    # ---- relationship history ---------------------------------------------------------------------------------
    def list_relationships(self, state: Optional[str] = None) -> list:
        with self.db.tx(self.tenant_id) as c:
            if state:
                rows = c.execute("select data from relationships where state = %s order by key", (state,)).fetchall()
            else:
                rows = c.execute("select data from relationships order by key").fetchall()
        return [r[0] for r in rows]

    def put_relationships(self, items: list):
        if not items:
            return
        with self.db.tx(self.tenant_id) as c:
            for d in items:
                c.execute("insert into relationships (tenant_id, key, state, data) values (%s, %s, %s, %s) "
                          "on conflict (tenant_id, key) do update set state = excluded.state, data = excluded.data, "
                          "updated_at = now()", (self.tenant_id, d["key"], d["state"], Jsonb(d)))

    def delete_relationships(self, keys: list):
        if keys:
            with self.db.tx(self.tenant_id) as c:
                c.execute("delete from relationships where key = any(%s)", (list(keys),))

    # ---- evidence snapshots (immutable) -------------------------------------------------------------------------
    def put_snapshot(self, snapshot_id: str, data: dict):
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into evidence_snapshots (tenant_id, id, data) values (%s, %s, %s) "
                      "on conflict (tenant_id, id) do nothing", (self.tenant_id, snapshot_id, Jsonb(data)))

    def get_snapshot(self, snapshot_id: str) -> Optional[dict]:
        with self.db.tx(self.tenant_id) as c:
            r = c.execute("select data, created_at from evidence_snapshots where id = %s", (snapshot_id,)).fetchone()
        return {**r[0], "created_at": r[1].isoformat()} if r else None

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
