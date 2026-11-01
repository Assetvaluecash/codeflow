"""Health, readiness and scrape-time metrics (PL-007).

  /healthz   liveness: the process is up and serving. No dependencies, so an orchestrator never restarts a healthy process because
             its database blinked.
  /readyz    readiness: can this process do its job? Database reachable, schema not behind the code, workspace writable; and whether
             anything is consuming the queue. A tenant that is suspended or an analysis that is incomplete does NOT make the platform
             unready: platform health is reported apart from analysis completeness (TFD 14).
  /metrics   Prometheus text; tia_* families (see metrics.py). Served only to a bearer token, or to this machine.

Nothing here returns a DSN, a path, a tenant id or a secret.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import metrics
from .db import MIGRATIONS, Database

QUEUE_STALE_SECONDS = 300
WORKER_STALE_SECONDS = 60


def expected_schema_version() -> str:
    return max(f.stem for f in MIGRATIONS.glob("*.sql"))


def liveness() -> dict:
    return {"status": "ok", "uptime_seconds": round(time.time() - metrics.START, 1)}


def _check(fn) -> dict:
    t = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as e:                                                   # noqa: BLE001 - a failing check is a result, not a crash
        ok, detail = False, f"{type(e).__name__}"
    return {"ok": bool(ok), "detail": detail, "ms": round((time.perf_counter() - t) * 1000, 1)}


def readiness(db: Database, tenant_id: Optional[str], workspace: Optional[str], expected: Optional[str] = None,
              check_queue: bool = True) -> tuple:
    """-> (http_status, body). 503 only when the platform cannot do its job."""
    expected = expected or expected_schema_version()
    checks: dict = {}

    def database():
        with db.pool.connection(timeout=2) as c:
            c.execute("select 1").fetchone()
        return True, "reachable"

    def schema():
        with db.pool.connection(timeout=2) as c:
            v = c.execute("select tia_schema_version()").fetchone()[0]
        if v is None or v < expected:
            return False, f"schema {v or 'none'} is behind this code ({expected}); run python -m tia.migrate"
        return True, f"schema {v}" + ("" if v == expected else f" (newer than this code, {expected})")

    def disk():
        if not workspace:
            return True, "not configured"
        p = Path(workspace)
        p.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=p, prefix=".ready-", delete=True):
            pass
        return True, "writable"
    checks["database"] = _check(database)
    if checks["database"]["ok"]:
        checks["schema"] = _check(schema)
    else:
        checks["schema"] = {"ok": False, "detail": "not checked: database unreachable", "ms": 0}
    checks["workspace"] = _check(disk)
    degraded = []
    body: dict = {}
    if check_queue and checks["database"]["ok"]:
        def queue():
            with db.pool.connection(timeout=2) as c, c.transaction():
                if tenant_id:
                    c.execute("select set_config('tia.tenant_id', %s, true)", (str(tenant_id),))
                queued, oldest = c.execute("select count(*), extract(epoch from now() - min(created_at)) from jobs where status = 'queued' "
                                           "and run_after <= now()").fetchone()
                workers = c.execute("select count(*) from worker_heartbeats where last_seen > now() - make_interval(secs => %s)",
                                    (WORKER_STALE_SECONDS,)).fetchone()[0]
            oldest = float(oldest) if oldest is not None else 0.0
            if queued and not workers:
                degraded.append("no worker has been seen recently")
            if oldest > QUEUE_STALE_SECONDS:
                degraded.append(f"the oldest queued job has waited {int(oldest)}s")
            return True, {"queued": queued, "oldest_queued_seconds": round(oldest, 1), "workers_seen_last_minute": workers}
        checks["queue"] = _check(queue)
    failed = [k for k, v in checks.items() if not v["ok"] and k != "queue"]
    status = "not_ready" if failed else ("degraded" if degraded or ("queue" in checks and not checks["queue"]["ok"]) else "ready")
    body = {"status": status, "checks": checks, **({"degraded": degraded} if degraded else {}),
            "note": "platform readiness only: tenant status and analysis completeness are reported elsewhere"}
    return (503 if status == "not_ready" else 200), body


# ---- scrape-time metrics ----------------------------------------------------------------------------------------------------------
class Cached:
    def __init__(self, fn, ttl: float):
        self.fn, self.ttl, self.at, self.value = fn, ttl, 0.0, None
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            if self.value is None or time.monotonic() - self.at > self.ttl:
                self.value, self.at = self.fn(), time.monotonic()
            return self.value


def register_collectors(db: Database, tenant_id: Optional[str] = None, store=None, ttl: float = 5.0) -> None:
    """Queue depth, database pool, repository freshness and relationship state, computed when scraped (cached briefly so a scrape storm
    costs one query). tenant_id: the API process counts its own tenant; the worker (tenant_id None) counts every tenant's jobs."""

    def jobs():
        sql = "select status, count(*) from jobs group by 1"
        age = "select coalesce(extract(epoch from now() - min(created_at)), 0) from jobs where status = 'queued' and run_after <= now()"
        retry = "select count(*) from jobs where status = 'queued' and attempts > 0"
        with db.pool.connection(timeout=2) as c, c.transaction():
            if tenant_id:
                c.execute("select set_config('tia.tenant_id', %s, true)", (str(tenant_id),))       # transaction-local: never leaks into the pool
            by = dict(c.execute(sql).fetchall())
            oldest, retrying = float(c.execute(age).fetchone()[0]), c.execute(retry).fetchone()[0]
        return by, oldest, retrying

    cached_jobs = Cached(jobs, ttl)

    def collect_jobs():
        by, oldest, retrying = cached_jobs()
        known = ("queued", "running", "succeeded", "failed", "cancelled", "superseded")
        yield ("tia_jobs", "gauge", "Jobs by status.", [({"status": s}, by.get(s, 0)) for s in known])
        yield ("tia_jobs_oldest_queued_seconds", "gauge", "Age of the oldest runnable queued job.", [({}, oldest)])
        yield ("tia_jobs_retrying", "gauge", "Queued jobs that have already failed an attempt.", [({}, retrying)])

    def collect_pool():
        s = db.pool.get_stats()
        yield ("tia_db_pool_size", "gauge", "Connections in the pool.", [({}, s.get("pool_size", 0))])
        yield ("tia_db_pool_available", "gauge", "Idle connections in the pool.", [({}, s.get("pool_available", 0))])
        yield ("tia_db_pool_requests_waiting", "gauge", "Requests waiting for a connection.", [({}, s.get("requests_waiting", 0))])
        yield ("tia_db_pool_connection_errors_total", "counter", "Failed attempts to open a connection.", [({}, s.get("connections_errors", 0))])

    metrics.REGISTRY.collector("jobs", collect_jobs)
    metrics.REGISTRY.collector("pool", collect_pool)
    if store is not None:
        def repos():
            now = time.time()
            by, oldest = {}, 0.0
            for r in store.list_repos():
                by[r.scan_status or "unknown"] = by.get(r.scan_status or "unknown", 0) + 1
                if r.last_successful_scan:
                    oldest = max(oldest, now - float(r.last_successful_scan))
            rel = {s: len(store.list_relationships(s)) for s in ("current", "stale")}
            return by, oldest, rel
        cached_repos = Cached(repos, ttl)

        def collect_repos():
            by, oldest, rel = cached_repos()
            yield ("tia_repositories", "gauge", "Registered repositories by last scan status.", [({"scan_status": s}, n) for s, n in sorted(by.items())])
            yield ("tia_repository_oldest_successful_scan_seconds", "gauge", "Age of the stalest repository's last successful scan (evidence freshness).",
                   [({}, oldest)])
            yield ("tia_relationships", "gauge", "Cross-repository relationships by state (current, stale).",
                   [({"state": s}, rel.get(s, 0)) for s in ("current", "stale")])
        metrics.REGISTRY.collector("repositories", collect_repos)


# ---- who may scrape ------------------------------------------------------------------------------------------------------------------
def metrics_allowed(client_host: Optional[str], authorization: Optional[str]) -> tuple:
    """-> (allowed, reason). A configured TIA_METRICS_TOKEN is required; without one only this machine may scrape (an orchestrator
    sidecar, or a proxy that adds the token); TIA_METRICS_ALLOW_ANY=true opens it, for a network that already restricts it."""
    from .credentials import env_secret, is_loopback
    import hmac
    token = env_secret("TIA_METRICS_TOKEN")
    if token:
        given = (authorization or "")[7:] if (authorization or "").lower().startswith("bearer ") else ""
        return (hmac.compare_digest(given.encode(), token.encode()), "a valid bearer token is required")
    if os.environ.get("TIA_METRICS_ALLOW_ANY", "").lower() in ("1", "true", "yes") or is_loopback(client_host):
        return True, ""
    return False, "metrics are served to this machine only; set TIA_METRICS_TOKEN to scrape remotely"


# ---- the worker has no web server of its own: a tiny one for metrics and health ---------------------------------------------------------
def serve_worker_endpoints(port: int, db: Database, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: str, ctype="application/json"):
            data = body.encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            import json
            if self.path == "/healthz":
                return self._send(200, json.dumps(liveness()))
            if self.path == "/readyz":
                code, body = readiness(db, None, None, check_queue=False)
                age = time.time() - (metrics.WORKER_POLL.values.get((), 0) or time.time())
                body["checks"]["poll"] = {"ok": age < 60, "detail": f"last poll {int(age)}s ago"}
                if age >= 60 and code == 200:
                    code, body["status"] = 503, "not_ready"
                return self._send(code, json.dumps(body))
            if self.path == "/metrics":
                ok, why = metrics_allowed(self.client_address[0], self.headers.get("Authorization"))
                return self._send(200 if ok else 403, metrics.REGISTRY.render() if ok else why + "\n", "text/plain; version=0.0.4; charset=utf-8")
            self._send(404, "{}")
    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="tia-worker-http").start()
    return srv
