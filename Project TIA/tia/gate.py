"""Request-path enforcement of the tenant lifecycle (UC1-20): is this tenant allowed to act, and is there storage room?

The runtime role cannot read the tenant registry. It asks the database for its OWN status and quota through tia_tenant_state(),
which resolves the tenant from the transaction setting, so a process can never learn another tenant's state. Answers are cached
briefly (a suspension takes effect within `ttl` seconds). Storage usage is approximate and says so: database row sizes (no indexes)
plus the bytes of the workspace on disk.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .db import RUNTIME_TABLES, Database
from .tenants import dir_size


class GateError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code, self.code, self.detail = status_code, code, detail


class TenantGate:
    def __init__(self, db: Database, tenant_id: str, workspace: str, default_quota: Optional[int] = None, ttl: float = 5.0,
                 usage_ttl: float = 30.0):
        self.db, self.tenant_id, self.workspace = db, tenant_id, workspace
        self.default_quota, self.ttl, self.usage_ttl = default_quota, ttl, usage_ttl
        self._state: Optional[tuple] = None
        self._usage: Optional[tuple] = None
        self._lock = threading.Lock()

    # ---- state ---------------------------------------------------------------------------------------------------
    def state(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if self._state and now - self._state[0] < self.ttl:
                return self._state[1]
        with self.db.tx(self.tenant_id) as conn:
            row = conn.execute("select status, storage_quota_bytes from tia_tenant_state()").fetchone()
        s = {"status": row[0] if row else "unknown", "quota_bytes": row[1] if row and row[1] is not None else self.default_quota}
        with self._lock:
            self._state = (now, s)
        return s

    def check_active(self) -> None:
        st = self.state()["status"]
        if st == "active":
            return
        if st in ("offboarding", "offboarded"):
            raise GateError(410, "tenant_offboarded", "this tenant has been offboarded")
        if st == "suspended":
            raise GateError(403, "tenant_suspended", "this tenant is suspended; contact your administrator")
        raise GateError(403, "tenant_unknown", "this tenant is not provisioned")

    # ---- storage ---------------------------------------------------------------------------------------------------
    def usage(self, fresh: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            if not fresh and self._usage and now - self._usage[0] < self.usage_ttl:
                return self._usage[1]
        tables = {}
        with self.db.tx(self.tenant_id) as conn:
            for t in RUNTIME_TABLES:
                rows, size = conn.execute(f'select count(*), coalesce(sum(pg_column_size(x.*)), 0)::bigint from "{t}" x').fetchone()
                tables[t] = {"rows": rows, "bytes": size}
        db_bytes = sum(v["bytes"] for v in tables.values())
        ws_bytes = dir_size(self.workspace)
        u = {"database": tables, "database_bytes": db_bytes, "workspace_bytes": ws_bytes, "total_bytes": db_bytes + ws_bytes}
        with self._lock:
            self._usage = (now, u)
        return u

    def invalidate(self) -> None:
        with self._lock:
            self._usage = None

    def check_room(self, adding: int = 0) -> None:
        quota = self.state()["quota_bytes"]
        if quota is None:
            return
        used = self.usage()["total_bytes"]
        if used + max(adding, 0) > quota:
            raise GateError(403, "storage_quota_exceeded",
                            f"storage quota exceeded: {used} of {quota} bytes used" + (f", {adding} more requested" if adding else ""))

    def summary(self) -> dict:
        st, u = self.state(), self.usage(fresh=True)
        q = st["quota_bytes"]
        return {"status": st["status"], "storage_quota_bytes": q, "storage_used_bytes": u["total_bytes"],
                "storage_used_fraction": round(u["total_bytes"] / q, 4) if q else None,
                "database_bytes": u["database_bytes"], "workspace_bytes": u["workspace_bytes"], "approximate": True}
