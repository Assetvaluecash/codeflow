"""Data retention (PRD 11.4) and audit reporting.

Customers configure, per tenant (versioned like every setting):
    "retention": {"graph_days": 90, "analysis_days": 730, "runtime_days": 180, "incident_days": 1095, "audit_days": 2555}
(null = keep forever, the default). The operator's scheduled job applies it (python -m tia.retention enforce), with the admin role, because
the runtime role can never delete audit records:
  graph      cached extracts and graph versions older than the limit (the current graph head is never removed)
  analysis   analysis reports older than the limit, and evidence snapshots no remaining report refers to
  runtime    runtime observations, deployments, CI results and coverage ingested before the limit
  incident   incidents, incident links and verdicts older than the limit
  audit      audit entries older than the limit: the last removed entry's sequence number and hash are kept as an ANCHOR, so the remaining
             chain still verifies, and the purge itself is the first audit entry written afterwards
Every run reports what it removed, per tenant and category.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg

KEYS = ("graph_days", "analysis_days", "runtime_days", "incident_days", "audit_days")
MINIMUM = {"graph_days": 1, "analysis_days": 30, "runtime_days": 1, "incident_days": 30, "audit_days": 365}
RUNTIME_KINDS = ("runtime_edge", "deployment", "test_run", "coverage")
INCIDENT_KINDS = ("incident", "incident_link", "verdict")


def validate(raw, problems: list) -> Optional[dict]:
    if not isinstance(raw, dict):
        problems.append("retention must be an object of *_days -> number of days (or null to keep forever)")
        return None
    out = {}
    for k, v in raw.items():
        if k not in KEYS:
            problems.append(f"unknown setting 'retention.{k}' (one of {', '.join(KEYS)})")
        elif v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < MINIMUM[k] or v > 36500):
            problems.append(f"retention.{k} must be a whole number of days from {MINIMUM[k]} to 36500, or null")
        else:
            out[k] = v
    return out


def enforce_tenant(c, tenant_id: str, retention: dict, now: Optional[datetime] = None) -> dict:
    """Applies one tenant's retention on an admin connection (in the caller's transaction). -> {category: rows removed}."""
    now = now or datetime.now(timezone.utc)
    cut = {k: now - timedelta(days=v) for k, v in (retention or {}).items() if v}
    out = {}
    t = str(tenant_id)
    if "graph_days" in cut:
        heads = [r[0] for r in c.execute("select graph_id from graph_heads where tenant_id = %s", (t,)).fetchall()]
        out["graph"] = (c.execute("delete from file_extracts where tenant_id = %s and created_at < %s", (t, cut["graph_days"])).rowcount
                        + c.execute("delete from extractions where tenant_id = %s and created_at < %s", (t, cut["graph_days"])).rowcount
                        + c.execute("delete from graph_versions where tenant_id = %s and created_at < %s and not (id = any(%s))",
                                    (t, cut["graph_days"], heads)).rowcount)
    if "analysis_days" in cut:
        n = c.execute("delete from analyses where tenant_id = %s and created_at < %s", (t, cut["analysis_days"])).rowcount
        n += c.execute("delete from evidence_snapshots s where s.tenant_id = %s and s.created_at < %s and not exists "
                       "(select 1 from analyses a where a.tenant_id = s.tenant_id and a.data->>'evidence_snapshot_id' = s.id)",
                       (t, cut["analysis_days"])).rowcount
        out["analysis"] = n
    if "runtime_days" in cut:
        out["runtime"] = c.execute("delete from evidence_records where tenant_id = %s and kind = any(%s) and ingested_at < %s",
                                   (t, list(RUNTIME_KINDS), cut["runtime_days"])).rowcount
    if "incident_days" in cut:
        out["incident"] = c.execute("delete from evidence_records where tenant_id = %s and kind = any(%s) and ingested_at < %s",
                                    (t, list(INCIDENT_KINDS), cut["incident_days"])).rowcount
    if "audit_days" in cut:
        iso = cut["audit_days"].isoformat(timespec="seconds")
        last = c.execute("select seq, hash from audit_log where tenant_id = %s and ts < %s order by seq desc limit 1", (t, iso)).fetchone()
        if last:
            c.execute("insert into audit_anchors (tenant_id, seq, hash, purged_before) values (%s, %s, %s, %s) "
                      "on conflict (tenant_id) do update set seq = excluded.seq, hash = excluded.hash, purged_before = excluded.purged_before, "
                      "created_at = now()", (t, last[0], last[1], iso))
            out["audit"] = c.execute("delete from audit_log where tenant_id = %s and seq <= %s", (t, last[0])).rowcount
        else:
            out["audit"] = 0
    return out


def enforce(admin_dsn: str, tenant: Optional[str] = None, now: Optional[datetime] = None, audit_key: Optional[bytes] = None) -> list:
    """Every active tenant (or one) -> [{tenant, removed}]. The purge is audited in each tenant's own chain."""
    from . import tenantconfig
    from .db import Database
    from .store import Store
    results = []
    with psycopg.connect(admin_dsn) as c:
        rows = c.execute("select id, name from tenants where status = 'active'" + (" and (id::text = %s or name = %s)" if tenant else ""),
                         (tenant, tenant) if tenant else ()).fetchall()
        for tid, name in rows:
            c.execute("select set_config('tia.tenant_id', %s, true)", (str(tid),))
            cfg = tenantconfig.latest(c, str(tid))
            ret = cfg.retention
            removed = enforce_tenant(c, str(tid), ret, now) if any(ret.values()) else {}
            c.commit()
            results.append({"tenant": name, "tenant_id": str(tid), "retention": ret, "removed": removed})
    if any(r["removed"] for r in results):
        db = Database(admin_dsn, max_size=2)
        try:
            for r in results:
                if r["removed"]:
                    Store(db, r["tenant_id"], audit_key).audit("retention_enforced", removed=r["removed"], retention=r["retention"])
        finally:
            db.close()
    return results


def audit_report(c, tenant_id: str, start: str, end: str) -> dict:
    """Audit reporting: who did what in a period, by event and by actor (from the tenant's own chain)."""
    rows = c.execute("select event, payload from audit_log where tenant_id = %s and ts >= %s and ts < %s order by seq",
                     (str(tenant_id), start, end)).fetchall()
    by_event, by_actor = {}, {}
    for event, payload in rows:
        by_event[event] = by_event.get(event, 0) + 1
        try:
            actor = (json.loads(payload) if isinstance(payload, str) else payload or {}).get("actor") or "system"
        except (ValueError, AttributeError):
            actor = "system"
        by_actor[actor] = by_actor.get(actor, 0) + 1
    return {"from": start, "to": end, "entries": len(rows), "by_event": dict(sorted(by_event.items())), "by_actor": dict(sorted(by_actor.items()))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tia.retention", description="apply the tenants' retention settings (run on a schedule)")
    from .credentials import env_secret
    ap.add_argument("--admin-dsn", default=env_secret("TIA_ADMIN_DATABASE_URL"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enforce")
    p.add_argument("--tenant", help="one tenant (name or id); default: every active tenant")
    a = ap.parse_args(argv)
    if not a.admin_dsn:
        print("an admin DSN is required (--admin-dsn or TIA_ADMIN_DATABASE_URL)", file=sys.stderr)
        return 2
    key = env_secret("TIA_AUDIT_KEY")
    print(json.dumps(enforce(a.admin_dsn, a.tenant, audit_key=key.encode() if key else None), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
