"""Resolve -> execute an impact analysis. Shared by the synchronous API and the queue worker.

resolve():  turns user-facing refs (branch, PR number, defaults) into a concrete, reproducible request
            (exact commit shas for the target and every other repository in scope).
execute():  runs the analysis for such a concrete request.
job_key():  idempotency key: the same concrete request is the same job.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from . import CONFIG_VERSION
from .impact import AnalysisRequest, analyze
from .repos import IngestError, RepoService
from .store import Store
from .worker import PermanentError


def resolve(svc: RepoService, p: dict, fetch_pr: bool = True) -> dict:
    """p keys: target, head|pr, base, token, repos, revisions, change_record, max_depth, max_results."""
    target = p["target"]
    svc.get(target)
    if p.get("pr") is not None:
        if not fetch_pr:
            raise IngestError("pull-request refs are fetched by the connector before queueing; pass 'head' (commit or branch)")
        head_sha = svc.fetch_pull_request(target, p["pr"], p.get("token"))
    elif p.get("head"):
        head_sha = svc.resolve(target, p["head"])
    else:
        raise IngestError("provide 'head' (branch/sha) or 'pr' (number)")
    base_sha = svc.merge_base(target, svc.resolve(target, p.get("base")), head_sha)
    scope = list(p.get("repos") or [r.name for r in svc.list()])
    if target not in scope:
        scope.append(target)
    scope = sorted(set(scope))
    revs = {n: svc.resolve(n, (p.get("revisions") or {}).get(n)) for n in scope if n != target}
    return {"target": target, "base": base_sha, "head": head_sha, "scope": scope, "revisions": revs,
            "pr": p.get("pr"), "change_record": p.get("change_record"),
            "max_depth": int(p.get("max_depth", 8)), "max_results": int(p.get("max_results", 200)),
            "head_ref": p.get("head")}


def job_key(c: dict) -> str:
    ident = {k: c[k] for k in ("target", "base", "head", "scope", "revisions", "max_depth", "max_results")}
    return hashlib.sha256(json.dumps([ident, CONFIG_VERSION], sort_keys=True).encode()).hexdigest()


def group_key(c: dict) -> str:
    """Submissions for the same pull request (or branch) supersede each other: newest wins."""
    return f"pr:{c['target']}:{c['pr']}" if c.get("pr") is not None else f"head:{c['target']}:{c.get('head_ref') or c['head']}"


def execute(svc: RepoService, store: Store, c: dict) -> dict:
    paths = {n: svc.get(n).path for n in c["scope"]}
    return analyze(AnalysisRequest(repos=paths, target=c["target"], base=c["base"], head=c["head"],
                                   revisions=c["revisions"], pull_request=c.get("pr"), change_record=c.get("change_record"),
                                   max_depth=c["max_depth"], max_results=c["max_results"]), store)


def make_handlers(db, allow_local: bool = False, allowed_hosts: Optional[list] = None, audit_key: Optional[bytes] = None,
                  workspace: str = ".tia-data/repos"):
    """Job handlers for the worker. Each job runs bound to its own tenant."""
    from .jobs import JobQueue
    queue = JobQueue(db)

    def impact_analysis(job):
        store = Store(db, job.tenant_id, audit_key)
        state = queue.get(job.tenant_id, job.id)
        if state and state["cancel_requested"]:
            return {"skipped": "superseded by a newer submission"}      # newest wins: do not do stale work
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        try:
            rep = execute(svc, store, job.payload)
        except IngestError as e:
            raise PermanentError(str(e))
        return {"analysis_id": rep["analysis_id"], "status": rep["status"]}

    return {"impact_analysis": impact_analysis}
