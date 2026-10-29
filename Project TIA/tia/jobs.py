"""PostgreSQL job queue (UC1-03).

Producer side (API, tenant-bound):   enqueue / get / cancel / is_latest
Consumer side (worker role only):    claim / heartbeat / succeed / fail / reap_expired

Properties
- Idempotent: the same (tenant, key) is one job; re-submitting returns it (deduplicated=True).
- Newest wins: jobs sharing a group_key (e.g. one pull request) - older queued jobs become `superseded`,
  an older running job gets cancel_requested; is_latest() lets publishers refuse stale results.
- Fair: workers pick the job whose tenant currently has the fewest running jobs, then the tenant's earliest
  queued job first (round-robin across tenants); a per-tenant concurrency cap is enforced exactly (advisory lock).
- Reliable: at-least-once with leases; failed attempts retry with exponential backoff up to max_attempts;
  expired leases are re-queued by reap_expired().
Only sessions of the tia_worker role can see other tenants' jobs (RLS policy), so an API process cannot claim work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from psycopg.types.json import Jsonb

from .db import Database

COLS = "tenant_id, id, kind, key, group_key, group_seq, payload, attempts, max_attempts, cancel_requested"


@dataclass
class Job:
    tenant_id: str
    id: str
    kind: str
    key: str
    group_key: Optional[str]
    group_seq: int
    payload: dict
    attempts: int
    max_attempts: int
    cancel_requested: bool = False
    worker_id: Optional[str] = None

    @staticmethod
    def from_row(r, worker_id=None) -> "Job":
        return Job(str(r[0]), str(r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], worker_id)


class JobQueue:
    def __init__(self, db: Database):
        self.db = db

    # ---- producer ------------------------------------------------------------------------------------
    def enqueue(self, tenant_id: str, kind: str, payload: dict, key: str, group_key: Optional[str] = None,
                max_attempts: int = 3, delay_s: float = 0, force: bool = False) -> dict:
        with self.db.tx(tenant_id) as c:
            row = c.execute(
                "insert into jobs (tenant_id, kind, key, group_key, payload, max_attempts, run_after) "
                "values (%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s)) "
                "on conflict (tenant_id, key) do nothing returning id", (tenant_id, kind, key, group_key, Jsonb(payload),
                                                                         max_attempts, delay_s)).fetchone()
            if row is None:
                jid, status = c.execute("select id, status from jobs where key = %s", (key,)).fetchone()
                if not (force and status in ("failed", "cancelled", "superseded")):
                    return {"job_id": str(jid), "deduplicated": True, "status": status}
                c.execute("update jobs set status='queued', attempts=0, error=null, result=null, cancel_requested=false, "
                          "locked_by=null, locked_until=null, run_after=now(), group_seq=nextval('job_group_seq'), "
                          "payload=%s, group_key=%s, updated_at=now() where id=%s", (Jsonb(payload), group_key, jid))
            else:
                jid = row[0]
            if group_key:
                c.execute("update jobs set status='superseded', updated_at=now() "
                          "where group_key=%s and status='queued' and id<>%s", (group_key, jid))
                c.execute("update jobs set cancel_requested=true, updated_at=now() "
                          "where group_key=%s and status='running' and id<>%s", (group_key, jid))
            return {"job_id": str(jid), "deduplicated": False, "status": "queued"}

    def get(self, tenant_id: str, job_id: str) -> Optional[dict]:
        with self.db.tx(tenant_id) as c:
            r = c.execute("select id, kind, status, attempts, max_attempts, error, result, group_key, cancel_requested, "
                          "created_at, updated_at, run_after from jobs where id = %s", (job_id,)).fetchone()
        if not r:
            return None
        return {"id": str(r[0]), "kind": r[1], "status": r[2], "attempts": r[3], "max_attempts": r[4], "error": r[5],
                "result": r[6], "group_key": r[7], "cancel_requested": r[8], "created_at": r[9].isoformat(),
                "updated_at": r[10].isoformat(), "run_after": r[11].isoformat()}

    def cancel(self, tenant_id: str, job_id: str) -> Optional[str]:
        """Queued -> cancelled immediately; running -> cancel_requested. Returns the resulting state."""
        with self.db.tx(tenant_id) as c:
            r = c.execute("update jobs set status='cancelled', updated_at=now() where id=%s and status='queued' "
                          "returning status", (job_id,)).fetchone()
            if r:
                return "cancelled"
            r = c.execute("update jobs set cancel_requested=true, updated_at=now() where id=%s and status='running' "
                          "returning status", (job_id,)).fetchone()
            return "cancel_requested" if r else None

    def is_latest(self, tenant_id: str, job_id: str) -> bool:
        """True when no newer job exists in the same group (publishers must check this before writing to a PR)."""
        with self.db.tx(tenant_id) as c:
            r = c.execute("select group_key, group_seq from jobs where id = %s", (job_id,)).fetchone()
            if not r:
                return False
            if r[0] is None:
                return True
            newer = c.execute("select 1 from jobs where group_key=%s and group_seq > %s and status <> 'cancelled' limit 1",
                              (r[0], r[1])).fetchone()
        return newer is None

    # ---- consumer (tia_worker role) --------------------------------------------------------------------
    def claim(self, worker_id: str, lease_s: float = 300, per_tenant_cap: int = 4,
              kinds: Optional[list] = None) -> Optional[Job]:
        with self.db.pool.connection() as conn, conn.transaction():
            cands = conn.execute(
                "select tenant_id, id from ("
                "  select j.tenant_id, j.id, j.created_at, coalesce(r.n, 0) as running, "
                "         row_number() over (partition by j.tenant_id order by j.created_at) as rn "
                "  from jobs j left join (select tenant_id, count(*) n from jobs where status='running' group by 1) r "
                "       using (tenant_id) "
                "  where j.status='queued' and j.run_after <= now() and (%s::text[] is null or j.kind = any(%s))"
                ") x order by running, rn, created_at limit 30", (kinds, kinds)).fetchall()
            for tid, jid in cands:
                # serialise claims per tenant so the concurrency cap is exact
                if not conn.execute("select pg_try_advisory_xact_lock(hashtextextended(%s, 1))", (str(tid),)).fetchone()[0]:
                    continue
                if conn.execute("select count(*) from jobs where tenant_id=%s and status='running'", (tid,)).fetchone()[0] >= per_tenant_cap:
                    continue
                row = conn.execute(
                    "with c as (select tenant_id, id from jobs where tenant_id=%s and id=%s and status='queued' "
                    "           for update skip locked) "
                    f"update jobs j set status='running', attempts=j.attempts+1, locked_by=%s, "
                    "  locked_until=now()+make_interval(secs => %s), updated_at=now() "
                    "from c where j.tenant_id=c.tenant_id and j.id=c.id "
                    f"returning j.{COLS.replace(', ', ', j.')}", (tid, jid, worker_id, lease_s)).fetchone()
                if row:
                    return Job.from_row(row, worker_id)
        return None

    def heartbeat(self, job: Job, lease_s: float = 300) -> bool:
        """Extend the lease; returns False if the job is no longer ours or cancellation was requested."""
        with self.db.pool.connection() as conn, conn.transaction():
            r = conn.execute("update jobs set locked_until = now()+make_interval(secs => %s), updated_at=now() "
                             "where tenant_id=%s and id=%s and status='running' and locked_by=%s returning cancel_requested",
                             (lease_s, job.tenant_id, job.id, job.worker_id)).fetchone()
        return bool(r) and not r[0]

    def succeed(self, job: Job, result: dict):
        with self.db.pool.connection() as conn, conn.transaction():
            conn.execute("update jobs set status='succeeded', result=%s, error=null, locked_by=null, locked_until=null, "
                         "updated_at=now() where tenant_id=%s and id=%s and locked_by=%s",
                         (Jsonb(result), job.tenant_id, job.id, job.worker_id))

    def fail(self, job: Job, error: str, permanent: bool = False, base_delay_s: float = 5):
        with self.db.pool.connection() as conn, conn.transaction():
            conn.execute(
                "update jobs set status = case when %s or attempts >= max_attempts then 'failed' else 'queued' end, "
                "  error=%s, locked_by=null, locked_until=null, "
                "  run_after = now() + make_interval(secs => %s * power(2, greatest(attempts - 1, 0))), updated_at=now() "
                "where tenant_id=%s and id=%s and locked_by=%s",
                (permanent, error[:2000], base_delay_s, job.tenant_id, job.id, job.worker_id))

    def reap_expired(self) -> int:
        """Re-queue (or fail, when out of attempts) running jobs whose lease expired - a worker died."""
        with self.db.pool.connection() as conn, conn.transaction():
            r = conn.execute(
                "update jobs set status = case when attempts >= max_attempts then 'failed' else 'queued' end, "
                "  error='lease expired (worker lost)', locked_by=null, locked_until=null, run_after=now(), updated_at=now() "
                "where status='running' and locked_until < now() returning 1").fetchall()
        return len(r)
