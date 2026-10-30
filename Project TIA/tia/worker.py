"""Worker loop: claims jobs from the queue and runs handlers.

    python -m tia.worker      (env: TIA_WORKER_DATABASE_URL, TIA_WORKSPACE, TIA_AUDIT_KEY, TIA_ALLOW_LOCAL_PATHS, ...)

A handler receives the Job and returns a JSON-able result. Raising PermanentError fails the job without retry;
any other exception retries with exponential backoff until max_attempts.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import uuid
from typing import Callable, Optional

from .jobs import Job, JobQueue


class PermanentError(Exception):
    """Do not retry: the job can never succeed (bad input, missing repository, ...)."""


class Worker:
    def __init__(self, queue: JobQueue, handlers: dict[str, Callable[[Job], dict]], worker_id: Optional[str] = None,
                 lease_s: float = 300, per_tenant_cap: int = 4, base_delay_s: float = 5):
        self.queue, self.handlers = queue, handlers
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_s, self.cap, self.base_delay_s = lease_s, per_tenant_cap, base_delay_s

    def run_once(self) -> bool:
        """Process at most one job. Returns True if a job was claimed (whatever its outcome)."""
        self.queue.reap_expired()
        job = self.queue.claim(self.worker_id, self.lease_s, self.cap, kinds=list(self.handlers))
        if not job:
            return False
        try:
            result = self.handlers[job.kind](job)
            self.queue.succeed(job, result if isinstance(result, dict) else {"result": result})
        except PermanentError as e:
            self.queue.fail(job, str(e), permanent=True)
        except Exception as e:                                   # noqa: BLE001 - any handler failure is retried
            self.queue.fail(job, f"{type(e).__name__}: {e}", base_delay_s=self.base_delay_s)
            print(f"job {job.id} ({job.kind}) failed attempt {job.attempts}:\n{traceback.format_exc()}", file=sys.stderr)
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
    dsn = os.environ.get("TIA_WORKER_DATABASE_URL")
    if not dsn:
        sys.exit("TIA_WORKER_DATABASE_URL is required")
    hosts = [h.strip() for h in os.environ.get("TIA_ALLOWED_GIT_HOSTS", "").split(",") if h.strip()]
    db = Database(dsn)
    handlers = make_handlers(db, allow_local=os.environ.get("TIA_ALLOW_LOCAL_PATHS", "").lower() in ("1", "true", "yes"),
                             allowed_hosts=hosts or None,
                             audit_key=os.environ["TIA_AUDIT_KEY"].encode() if os.environ.get("TIA_AUDIT_KEY") else None,
                             workspace=os.environ.get("TIA_WORKSPACE", ".tia-data/repos"),
                             scm=ScmConfig.from_env())
    stop = threading.Event()
    print(f"worker started; kinds={list(handlers)}", file=sys.stderr)
    try:
        Worker(JobQueue(db), handlers).run_forever(stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
