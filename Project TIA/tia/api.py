"""TIA HTTP API and web UI (TFD section 8). Requires the `api` extra.

Repositories are ingested from GitHub, Azure DevOps, any git URL, or (in development mode) a local path;
analyses run against the registered repositories. Analyses run synchronously here; the contract - POST
returns an analysis id, GET retrieves the immutable report - matches the asynchronous design.

NOT YET PRODUCTION: authentication/authorisation are not implemented in this module (tracked: PL-001, PL-002);
the tenant is fixed at start-up. Callers may narrow the audience with X-TIA-Readable-Repos (placeholder).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .graphview import LEVELS, build_view, impact_overlay, landscape_stats, restrict
from .analysis_service import execute, group_key, job_key, resolve
from .impact import _extract
from .jobs import JobQueue
from .repos import IngestError, RepoService
from .report import redact, render_comment
from .db import Database
from .store import Store

WEB = Path(__file__).parent / "web"


class RepoIn(BaseModel):
    url: Optional[str] = None
    path: Optional[str] = None
    name: Optional[str] = None
    token: Optional[str] = None
    branch: Optional[str] = None


class DiscoverIn(BaseModel):
    provider: str                       # github | ado
    org: str
    project: Optional[str] = None       # required for ado
    token: str
    api_base: Optional[str] = None      # on-prem Azure DevOps Server / GitHub Enterprise
    ingest: bool = True
    names: Optional[list[str]] = None   # ingest only these (default: all discovered)


class SyncIn(BaseModel):
    token: Optional[str] = None


class AnalysisIn(BaseModel):
    target: str
    head: Optional[str] = None          # branch, tag, or sha in the target repository
    pr: Optional[int] = None            # pull request number (alternative to head)
    base: Optional[str] = None          # default: the repository's default branch (merge-base is used)
    token: Optional[str] = None         # only needed to fetch a PR ref from a private remote
    repos: Optional[list[str]] = None   # scope; default: every registered repository
    revisions: dict[str, str] = {}      # other repositories: name -> ref (default: default branch)
    change_record: Optional[str] = None
    max_depth: int = 8
    max_results: int = 200


def create_app(db: Database, tenant_id: str, workspace: str = ".tia-data/repos", allow_local: bool = False,
               allowed_hosts: Optional[list] = None, audit_key: Optional[bytes] = None) -> FastAPI:
    app = FastAPI(title="TIA - Technical Impact Analysis")
    # Until SSO (PL-001) the app serves exactly one tenant, fixed at start-up; isolation is enforced by RLS regardless.
    store = Store(db, tenant_id, audit_key)
    jobs = JobQueue(db)
    svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)

    def audience(h):
        return h.split(",") if h else None

    def wrap(fn, *a, **k):
        try:
            return fn(*a, **k)
        except IngestError as e:
            raise HTTPException(400, str(e))

    app.mount("/vendor", StaticFiles(directory=WEB / "vendor"), name="vendor")

    def landscape(names: Optional[list] = None) -> dict:
        """Extracts of every registered repository at its default branch (cached by commit)."""
        out = {}
        for r in svc.list():
            if names and r.name not in names:
                continue
            try:
                out[r.name] = _extract(r.name, r.path, svc.resolve(r.name, None), store, ())
            except Exception as e:           # a broken repo must not blank the whole graph
                store.audit("graph_extract_failed", repo=r.name, error=str(e)[:200])
        return out

    # ---- UI / meta ----------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "repositories": len(svc.list())}

    @app.get("/api/v1/config")
    def config():
        return {"allow_local_paths": allow_local, "allowed_git_hosts": allowed_hosts or [],
                "auth": "none (development mode)"}

    # ---- repositories -------------------------------------------------------------------------------------
    @app.post("/api/v1/repositories")
    def add_repo(body: RepoIn):
        return wrap(svc.register, body.url, body.path, body.name, body.token, body.branch).public()

    @app.get("/api/v1/repositories")
    def list_repos():
        return [r.public() for r in svc.list()]

    @app.post("/api/v1/repositories/discover")
    def discover(body: DiscoverIn):
        found = wrap(svc.discover, body.provider, body.org, body.project, body.token, body.api_base)
        if not body.ingest:
            return {"discovered": found, "ingested": [], "failed": []}
        ok, failed = [], []
        for r in found:
            if body.names and r["name"] not in body.names:
                continue
            try:
                ok.append(svc.register(url=r["url"], name=r["name"], token=body.token).public())
            except IngestError as e:
                failed.append({"name": r["name"], "error": str(e)})
        return {"discovered": found, "ingested": ok, "failed": failed}

    @app.post("/api/v1/repositories/{name}/sync")
    def sync(name: str, body: SyncIn = SyncIn()):
        return wrap(svc.sync, name, body.token).public()

    @app.delete("/api/v1/repositories/{name}")
    def delete(name: str):
        wrap(svc.remove, name)
        return {"deleted": name}

    # ---- audit ----------------------------------------------------------------------------------------------
    @app.get("/api/v1/audit")
    def audit(limit: int = 100):
        return store.audit_events(min(max(limit, 1), 1000))

    @app.get("/api/v1/audit/verify")
    def audit_verify():
        return store.verify_audit()

    # ---- code graph ---------------------------------------------------------------------------------------------
    @app.get("/api/v1/graph")
    def graph(level: str = "service", repos: Optional[str] = None, q: Optional[str] = None,
              focus: Optional[str] = None, depth: int = 1, limit: int = 600,
              x_tia_readable_repos: Optional[str] = Header(None)):
        if level not in LEVELS:
            raise HTTPException(400, f"level must be one of {list(LEVELS)}")
        ex = landscape()
        if not ex:
            return {"level": level, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "total_nodes": 0,
                                                                         "total_edges": 0, "truncated": False, "limit": limit,
                                                                         "unresolved_call_sites": 0}}
        try:
            view = build_view(ex, level, repos.split(",") if repos else None, q, focus, depth, min(max(limit, 10), 5000))
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        return restrict(view, audience(x_tia_readable_repos))

    @app.get("/api/v1/graph/stats")
    def graph_stats(x_tia_readable_repos: Optional[str] = Header(None)):
        rows = landscape_stats(landscape())
        ok = audience(x_tia_readable_repos)
        return [r for r in rows if ok is None or r["repo"] in ok]

    # ---- analyses ----------------------------------------------------------------------------------------------
    @app.post("/api/v1/impact-analyses")
    def create(body: AnalysisIn, response: Response, async_: bool = Query(False, alias="async"),
               x_tia_readable_repos: Optional[str] = Header(None)):
        """Synchronous by default. With ?async=true the analysis is queued and a job is returned (HTTP 202);
        queued analyses never accept tokens (nothing secret is stored in the queue)."""
        params = body.model_dump()
        if async_:
            if body.token:
                raise HTTPException(400, "tokens are not accepted for queued analyses; sync the repository first")
            concrete = wrap(resolve, svc, params, False)
            job = jobs.enqueue(tenant_id, "impact_analysis", concrete, job_key(concrete), group_key(concrete))
            store.audit("analysis_queued", job=job["job_id"], target=concrete["target"], head=concrete["head"],
                        deduplicated=job["deduplicated"])
            response.status_code = 202
            return {**job, "target": concrete["target"], "base": concrete["base"], "head": concrete["head"]}
        concrete = wrap(resolve, svc, params)
        rep = wrap(execute, svc, store, concrete)
        return {"analysis_id": rep["analysis_id"], "status": rep["status"],
                "report": redact(rep, audience(x_tia_readable_repos))}

    @app.get("/api/v1/jobs/{job_id}")
    def job_status(job_id: str):
        j = jobs.get(tenant_id, job_id)
        if not j:
            raise HTTPException(404, "unknown job")
        return {**j, "is_latest": jobs.is_latest(tenant_id, job_id)}

    @app.delete("/api/v1/jobs/{job_id}")
    def job_cancel(job_id: str):
        r = jobs.cancel(tenant_id, job_id)
        if not r:
            raise HTTPException(409, "job is not queued or running")
        store.audit("job_cancelled", job=job_id, result=r)
        return {"job_id": job_id, "result": r}

    def get(aid: str, h):
        rep = store.get_analysis(aid)
        if not rep:
            raise HTTPException(404, "unknown analysis id")
        return redact(rep, audience(h))

    @app.get("/api/v1/impact-analyses/{aid}")
    def report(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return get(aid, x_tia_readable_repos)

    @app.get("/api/v1/impact-analyses/{aid}/graph")
    def analysis_graph(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return restrict(impact_overlay(get(aid, x_tia_readable_repos)), audience(x_tia_readable_repos))

    @app.get("/api/v1/impact-analyses/{aid}/comment", response_class=PlainTextResponse)
    def comment(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return render_comment(get(aid, x_tia_readable_repos))

    @app.get("/api/v1/impact-analyses/{aid}/paths")
    def paths(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return [{"finding": f["id"], "component": f["component"], "evidence_status": f["evidence_status"],
                 "path": f["path"], "path_edges": f["path_edges"]} for f in get(aid, x_tia_readable_repos)["findings"]]

    @app.get("/api/v1/impact-analyses/{aid}/unknowns")
    def unknowns(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return get(aid, x_tia_readable_repos)["unknowns"]

    return app
