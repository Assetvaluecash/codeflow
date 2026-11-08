"""SaaS operations per tenant: usage metering and plan limits (PRD PL-014), data residency (PL-015, AC-041).

* Plan, region and customer key reference are set by the OPERATOR in the tenant registry (`python -m tia.tenants plan-set / region-set /
  key-set`), never by a tenant. The request path reads its own tenant's settings through tia_tenant_settings(), which resolves the tenant
  from the transaction setting, so a process cannot learn another tenant's plan.
* Usage is metered from the tenant's own rows: repositories, active users, analyses, evidence records and jobs in a month. A plan limit
  that is reached refuses the action that would exceed it (HTTP 402 with code plan_limit) and is audited; nothing already stored is touched.
  `python -m tia.tenants usage` exports the month for billing (JSON or CSV).
* Residency: a deployment serves one region (TIA_REGION). A tenant pinned to another region is refused before any of its data is read or
  written (HTTP 421 misdirected, code wrong_region); its rows live only in its region's database, which only that region's deployment
  holds credentials for. An unpinned tenant is served anywhere (a single-region installation needs no setting).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

PLAN_KEYS = ("max_repositories", "max_users", "max_analyses_per_month", "max_evidence_records_per_month")
REGIONS = ("eu", "us", "apac", "uk", "ca", "au", "in", "jp", "ch", "sa")


class PlanError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code, self.code, self.detail = status_code, code, detail


def this_region() -> Optional[str]:
    r = (os.environ.get("TIA_REGION") or "").strip().lower()
    return r or None


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("a plan is an object of limits")
    out = {}
    for k, v in plan.items():
        if k not in PLAN_KEYS:
            raise ValueError(f"unknown plan limit '{k}' (one of {', '.join(PLAN_KEYS)})")
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            raise ValueError(f"{k} must be a whole number >= 0, or null for unlimited")
        out[k] = v
    return out


def month_bounds(month: Optional[str] = None) -> tuple:
    now = datetime.now(timezone.utc)
    y, m = (int(month[:4]), int(month[5:7])) if month else (now.year, now.month)
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return start, end


def usage(conn, tenant_id: str, month: Optional[str] = None) -> dict:
    """One tenant's metered usage: totals now and activity in the month. Filtered by tenant explicitly, so it is right on a tenant-bound
    connection (row-level security) and on the operator's connection alike."""
    start, end = month_bounds(month)
    t = str(tenant_id)
    one = lambda q, *a: conn.execute(q, (t, *a)).fetchone()[0]
    return {"month": start.strftime("%Y-%m"),
            "repositories": one("select count(*) from repositories where tenant_id = %s"),
            "users": one("select count(*) from tenant_users where tenant_id = %s and not disabled"),
            "users_active_in_month": one("select count(*) from tenant_users where tenant_id = %s and last_login >= %s and last_login < %s", start, end),
            "analyses_in_month": one("select count(*) from analyses where tenant_id = %s and created_at >= %s and created_at < %s", start, end),
            "jobs_in_month": one("select count(*) from jobs where tenant_id = %s and created_at >= %s and created_at < %s", start, end),
            "evidence_records_in_month": one("select count(*) from evidence_records where tenant_id = %s and ingested_at >= %s and ingested_at < %s",
                                             start, end)}


class TenantPlan:
    """The tenant's plan, region and key reference (cached briefly) and the checks made before an action that would add usage."""

    def __init__(self, db, tenant_id: str, ttl: float = 10.0):
        self.db, self.tenant_id, self.ttl = db, str(tenant_id), ttl
        self._cache: Optional[tuple] = None
        self._lock = threading.Lock()

    def settings(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if self._cache and now - self._cache[0] < self.ttl:
                return self._cache[1]
        with self.db.tx(self.tenant_id) as c:
            row = c.execute("select plan, region, key_ref from tia_tenant_settings()").fetchone()
        s = {"plan": (row[0] if row else None) or {}, "region": row[1] if row else None, "key_ref": row[2] if row else None}
        with self._lock:
            self._cache = (now, s)
        return s

    def check_region(self) -> None:
        here, pinned = this_region(), self.settings()["region"]
        if pinned and here and pinned != here:
            raise PlanError(421, "wrong_region", f"this organisation's data is held in region '{pinned}'; use that region's address")
        if pinned and not here:
            raise PlanError(421, "wrong_region", f"this organisation is pinned to region '{pinned}' but this deployment has no region (TIA_REGION)")

    def check(self, what: str, adding: int = 1) -> None:
        """Refuses an action that would exceed the plan. what: repositories | users | analyses | evidence."""
        plan = self.settings()["plan"]
        key = {"repositories": "max_repositories", "users": "max_users", "analyses": "max_analyses_per_month",
               "evidence": "max_evidence_records_per_month"}[what]
        limit = plan.get(key)
        if limit is None:
            return
        with self.db.tx(self.tenant_id) as c:
            u = usage(c, self.tenant_id)
        have = {"max_repositories": u["repositories"], "max_users": u["users"], "max_analyses_per_month": u["analyses_in_month"],
                "max_evidence_records_per_month": u["evidence_records_in_month"]}[key]
        if have + adding > limit:
            raise PlanError(402, "plan_limit", f"the plan allows {limit} {key.removeprefix('max_').replace('_', ' ')} and {have} are used; "
                                               "ask your provider to change the plan")

    def invalidate(self):
        with self._lock:
            self._cache = None
