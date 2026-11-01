"""Worker loop: claims jobs from the queue and runs handlers.

    python -m tia.worker      (env: TIA_WORKER_DATABASE_URL, TIA_WORKSPACE, TIA_AUDIT_KEY, TIA_ALLOW_LOCAL_PATHS, ...)

A handler receives the Job and returns a JSON-able result. Raising PermanentError fails the job without retry;
any other exception retries with exponential backoff until max_attempts.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from typing import Callable, Optional

from . import metrics, tracing
from .jobs import Job, JobQueue

log = logging.getLogger("tia.worker")


class PermanentError(Exception):
    """Do not retry: the job can never succeed (bad input, missing repository, ...)."""


class Worker:
    def __init__(self, queue: JobQueue, handlers: dict[str, Callable[[Job], dict]], worker_id: Optional[str] = None,
                 lease_s: float = 300, per_tenant_cap: int = 4, base_delay_s: float = 5):
        self.queue, self.handlers = queue, handlers
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_s, self.cap, self.base_delay_s = lease_s, per_tenant_cap, base_delay_s
        self._beat_at = 0.0

    def run_once(self) -> bool:
        """Process at most one job. Returns True if a job was claimed (whatever its outcome)."""
        metrics.WORKER_POLL.set(time.time())
        if time.monotonic() - self._beat_at > 10:
            self._beat_at = time.monotonic()
            try:
                self.queue.beat(self.worker_id)
            except Exception:                                    # noqa: BLE001 - a heartbeat must never stop the worker
                log.warning("worker heartbeat failed")
        self.queue.reap_expired()
        job = self.queue.claim(self.worker_id, self.lease_s, self.cap, kinds=list(self.handlers))
        if not job:
            return False
        parent = tracing.parse_traceparent((job.payload or {}).get("_traceparent"))       # the request's trace continues in the worker
        t0, outcome = time.perf_counter(), "succeeded"
        with tracing.span("job.run", "consumer", parent=parent, job_kind=job.kind, attempt=job.attempts) as sp:
            try:
                result = self.handlers[job.kind](job)
                self.queue.succeed(job, result if isinstance(result, dict) else {"result": result})
            except PermanentError as e:
                outcome = "failed"
                self.queue.fail(job, str(e), permanent=True)
                log.warning("job failed permanently", extra={"job_id": job.id, "kind": job.kind, "error": str(e)})
            except Exception as e:                               # noqa: BLE001 - any handler failure is retried
                outcome = "retried" if job.attempts < job.max_attempts else "failed"
                self.queue.fail(job, f"{type(e).__name__}: {e}", base_delay_s=self.base_delay_s)
                log.error("job attempt failed", extra={"job_id": job.id, "kind": job.kind, "attempt": job.attempts}, exc_info=True)
            sp.set(outcome=outcome)
        metrics.JOB_RUNS.labels(kind=job.kind, outcome=outcome).inc()
        metrics.JOB_DURATION.labels(kind=job.kind).observe(time.perf_counter() - t0)
        return True

    def run_forever(self, stop: threading.Event, poll_s: float = 1.0):
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:                                    # noqa: BLE001 - DB hiccup: keep the worker alive
                print(traceback.format_exc(), file=sys.stderr)
                busy = False
            if not busy:
                stop.wait(poll_s)


def main():
    from .analysis_service import make_handlers
    from .db import Database
    from .webhooks import ScmConfig
    from . import health, obs
    from .credentials import SecretError, env_secret, keywrapper_from_env
    from .transport import check_db_tls
    obs.setup_logging()
    tracing.configure_from_env(logging.getLogger("tia.trace"))
    try:
        dsn = env_secret("TIA_WORKER_DATABASE_URL")
        if not dsn:
            sys.exit("TIA_WORKER_DATABASE_URL is required")
        hosts = [h.strip() for h in os.environ.get("TIA_ALLOWED_GIT_HOSTS", "").split(",") if h.strip()]
        db = Database(check_db_tls(dsn))
        audit_key = env_secret("TIA_AUDIT_KEY")
        handlers = make_handlers(db, allow_local=os.environ.get("TIA_ALLOW_LOCAL_PATHS", "").lower() in ("1", "true", "yes"),
                                 allowed_hosts=hosts or None, audit_key=audit_key.encode() if audit_key else None,
                                 workspace=os.environ.get("TIA_WORKSPACE", ".tia-data/repos"), scm=ScmConfig.from_env(),
                                 keywrapper=keywrapper_from_env())
    except SecretError as e:
        sys.exit(f"configuration error: {e}")
    stop = threading.Event()
    health.register_collectors(db)
    if os.environ.get("TIA_WORKER_METRICS_PORT"):
        health.serve_worker_endpoints(int(os.environ["TIA_WORKER_METRICS_PORT"]), db, os.environ.get("TIA_WORKER_METRICS_HOST", "127.0.0.1"))
    logging.getLogger("tia.worker").info("worker started", extra={"kinds": list(handlers)})
    try:
        Worker(JobQueue(db), handlers).run_forever(stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
