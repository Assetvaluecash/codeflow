"""Tenant-scoped persistence on PostgreSQL: evidence cache, analyses, repository registry, audit chain.

Every method runs in a transaction bound to this store's tenant; row-level security (not this code)
is what prevents cross-tenant access.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pickle
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Jsonb

from . import tenantconfig
from .db import Database
from .identity import current_principal
from .models import RepoExtract
from .scrub import scrub_json

GENESIS = "GENESIS"


def _chain_hash(prev: str, seq: int, ts: str, event: str, payload: str, key: Optional[bytes] = None) -> str:
    msg = f"{prev}|{seq}|{ts}|{event}|{payload}".encode("utf-8")
    # With a key held outside the database, someone who can edit rows cannot also recompute a valid chain.
    return hmac.new(key, msg, hashlib.sha256).hexdigest() if key else hashlib.sha256(msg).hexdigest()


class _RowCache:
    """Decoded copies of large rows that are read far more often than written: repository extracts and graph versions (a worker running
    a burst of analyses reads the same 500 extracts for every job). Each use is validated against the row's current version (PostgreSQL
    xmin) in the caller's tenant-bound transaction, so a rewritten row (a full rescan, a rebuilt graph) is never served stale, and an entry
    is only ever filled from a row-level-security-checked read of the same tenant, whose id is part of the key. Entries are kept serialized,
    so every reader gets new objects it may change freely. Bounded by TIA_CACHE_MB (default 256; 0 turns it off); least recently used goes."""

    def __init__(self, max_bytes: int):
        self.max, self.used, self._d, self._lock = max_bytes, 0, OrderedDict(), threading.Lock()

    def get(self, key, version):
        if self.max <= 0:
            return None
        with self._lock:
            e = self._d.get(key)
            if e is None or e[0] != version:
                return None
            self._d.move_to_end(key)
            blob = e[1]
        return pickle.loads(blob)

    def put(self, key, version, obj):
        if self.max <= 0:
            return
        blob = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        if len(blob) > self.max // 4:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old:
                self.used -= len(old[1])
            self._d[key] = (version, blob)
            self.used += len(blob)
            while self.used > self.max and self._d:
                _, (_, b) = self._d.popitem(last=False)
                self.used -= len(b)

    def clear(self):
        with self._lock:
            self._d.clear()
            self.used = 0


def _cache_bytes() -> int:
    try:
        return max(0, int(os.environ.get("TIA_CACHE_MB", "256"))) * 1024 * 1024
    except ValueError:
        return 256 * 1024 * 1024


ROW_CACHE = _RowCache(_cache_bytes())


class Store:
    def __init__(self, db: Database, tenant_id: str, audit_key: Optional[bytes] = None):
        self.db = db
        self.tenant_id = str(tenant_id)
        self.audit_key = audit_key

    # ---- evidence cache (incremental analysis: unchanged revisions are not re-parsed) -----------------
    @staticmethod
    def _key(repo: str, commit: str, version: str, strip: tuple = (), exclude: tuple = ()) -> str:
        """An extract depends on the gateway prefixes and exclude globs it was made with: a different configuration is a different cache entry."""
        return (f"{repo}@{commit}#{version}" + (f"|strip={','.join(strip)}" if strip else "")
                + (f"|excl={','.join(exclude)}" if exclude else ""))

    def _cached_row(self, table: str, key: str, kind: str):
        """The `data` of one row by key, through ROW_CACHE: a version check costs one small query; the large JSON is transferred and decoded
        only when this process has not seen this version of the row."""
        ck = (self.tenant_id, kind, key)
        with self.db.tx(self.tenant_id) as c:
            v = c.execute(f"select xmin::text from {table} where {'key' if table == 'extractions' else 'id'} = %s", (key,)).fetchone()
            if not v:
                return None
            hit = ROW_CACHE.get(ck, v[0])
            if hit is not None:
                return hit
            r = c.execute(f"select data, xmin::text from {table} where {'key' if table == 'extractions' else 'id'} = %s", (key,)).fetchone()
        if not r:
            return None
        ROW_CACHE.put(ck, r[1], r[0])
        return r[0]

    def get_extract(self, repo: str, commit: str, version: str, strip: tuple = (), exclude: tuple = ()) -> Optional[RepoExtract]:
        d = self._cached_row("extractions", self._key(repo, commit, version, tuple(strip), tuple(exclude)), "extract")
        return RepoExtract.from_dict(d) if d else None

    def put_extract(self, ex: RepoExtract):
        with self.db.tx(self.tenant_id) as c:
            c.execute("insert into extractions (tenant_id, key, data) values (%s, %s, %s) "
                      "on conflict (tenant_id, key) do update set data = excluded.data",
                      (self.tenant_id, self._key(ex.repo, ex.commit, ex.extractor_version, tuple(ex.strip_prefixes),
                                                 tuple(ex.exclude_patterns or ())), Jsonb(ex.to_dict())))

    # ---- tenant configuration (versioned, insert-only) -------------------------------------------------------------
    def effective_config(self):
        with self.db.tx(self.tenant_id) as c:
            return tenantconfig.latest(c, self.tenant_id)

    def put_config(self, raw: dict, comment=None, actor=None) -> dict:
        with self.db.tx(self.tenant_id) as c:
            return tenantconfig.put_version(c, self.tenant_id, raw, comment, actor)

    def get_config_version(self, version: int):
        with self.db.tx(self.tenant_id) as c:
            return tenantconfig.get_version(c, self.tenant_id, version)

    def list_config_versions(self, limit: int = 50) -> list:
        with self.db.tx(self.tenant_id) as c:
            return tenantconfig.list_versions(c, self.tenant_id, limit)

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
        return self._cached_row("graph_versions", graph_id, "graph")

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

    def find_analyses(self, repository: str, pull_request: Optional[int] = None, revision: Optional[str] = None, limit: int = 1) -> list[dict]:
        """The newest reports of a repository, optionally of one pull request or one proposed revision (a full sha or a prefix of 7+)."""
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute(
                "select data from analyses where tenant_id = %s and data->'change'->>'repository' = %s "
                "and (%s::int is null or (data->'change'->>'pull_request')::int = %s::int) "
                "and (%s::text is null or data->'change'->>'proposed_revision' like %s) order by created_at desc limit %s",
                (self.tenant_id, repository, pull_request, pull_request, revision, (revision or "") + "%", limit)).fetchall()
        return [r[0] for r in rows]

    def list_analyses(self, since: Optional[str] = None, until: Optional[str] = None, limit: int = 20000) -> list[dict]:
        """Stored reports in a period, oldest first (trends, calibration)."""
        where, args = [], []
        if since:
            where.append("created_at >= %s")
            args.append(since)
        if until:
            where.append("created_at < %s")
            args.append(until)
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select data from analyses" + (" where " + " and ".join(where) if where else "") + " order by created_at limit %s",
                             (*args, limit)).fetchall()
        return [r[0] for r in rows]

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
            for t in ("repo_access", "repo_grants", "scm_permissions", "scm_sync_status"):
                c.execute(f"delete from {t} where tenant_id = %s and repo = %s", (self.tenant_id, name))

    # ---- audit: append-only, hash-chained per tenant ----------------------------------------------------------
    def audit(self, event: str, **fields):
        who = current_principal.get()
        if who is not None and who.kind != "system" and "actor" not in fields:
            fields["actor"] = who.actor                                    # who did it: the authenticated principal of the request
        fields, _ = scrub_json(fields, generic_everywhere=True)         # an audit record never carries a secret
        payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.db.tx(self.tenant_id) as c:
            c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (self.tenant_id,))
            last = c.execute("select seq, hash from audit_log where tenant_id = %s order by seq desc limit 1", (self.tenant_id,)).fetchone()
            if last is None:                                               # every entry purged by retention: continue from the anchor
                last = c.execute("select seq, hash from audit_anchors where tenant_id = %s", (self.tenant_id,)).fetchone()
            seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
            c.execute("insert into audit_log (tenant_id, seq, ts, event, payload, prev_hash, hash) values (%s,%s,%s,%s,%s,%s,%s)",
                      (self.tenant_id, seq, ts, event, payload, prev, _chain_hash(prev, seq, ts, event, payload, self.audit_key)))

    def audit_events(self, limit: int = 100) -> list[dict]:
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select seq, ts, event, payload from audit_log where tenant_id = %s order by seq desc limit %s", (self.tenant_id, limit)).fetchall()
        return [{"seq": s, "ts": t, "event": e, **json.loads(p)} for s, t, e, p in rows]

    def verify_audit(self) -> dict:
        """Recompute the whole chain. ok=False names the first sequence number that does not verify."""
        with self.db.tx(self.tenant_id) as c:
            rows = c.execute("select seq, ts, event, payload, prev_hash, hash from audit_log where tenant_id = %s order by seq", (self.tenant_id,)).fetchall()
            anchor = c.execute("select seq, hash, purged_before from audit_anchors where tenant_id = %s", (self.tenant_id,)).fetchone()
        # entries removed by retention (tia.retention) leave an anchor: the chain continues from the last removed entry's hash
        prev, expect = (anchor[1], anchor[0] + 1) if anchor else (GENESIS, 1)
        for seq, ts, event, payload, ph, h in rows:
            if seq != expect or ph != prev or h != _chain_hash(prev, seq, ts, event, payload, self.audit_key):
                return {"ok": False, "entries": len(rows), "first_bad_seq": seq, "anchor": _anchor(anchor)}
            prev, expect = h, expect + 1
        return {"ok": True, "entries": len(rows), "first_bad_seq": None, "anchor": _anchor(anchor)}


def _anchor(a) -> Optional[dict]:
    return {"seq": a[0], "purged_before": a[2]} if a else None
