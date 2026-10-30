"""TIA HTTP API and web UI (TFD section 8). Requires the `api` extra.

Repositories are ingested from GitHub, Azure DevOps, any git URL, or (in development mode) a local path;
analyses run against the registered repositories. Analyses run synchronously here; the contract - POST
returns an analysis id, GET retrieves the immutable report - matches the asynchronous design.

NOT YET PRODUCTION: authentication/authorisation are not implemented in this module (tracked: PL-001, PL-002);
the tenant is fixed at start-up. Callers may narrow the audience with X-TIA-Readable-Repos (placeholder).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .graphview import LEVELS, build_view, impact_overlay, landscape_stats, restrict
from .analysis_service import (execute, group_key, job_key, landscape_extracts, pr_event_group_key, pr_event_job_key,
                               refresh_landscape, reproduce, rescan_landscape, resolve)
from .graphstore import LANDSCAPE, get_or_build
from .publish import PublishError, audit_publication, publish_to_github
from .webhooks import ScmConfig, github_full_name, match_repo, parse_pull_request, verify_signature
from .jobs import JobQueue
from .repos import IngestError, RepoService, _clean
from .upload import CHUNK, Session, UploadConfig, UploadTooLarge
from .report import evidence_view, redact, redact_snapshot, render_comment
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


class UploadIn(BaseModel):
    name: str                           # repository name; uploading the same name again adds a revision


class PublishIn(BaseModel):
    repository: str                     # registered repository the pull request belongs to
    pr: int
    head_sha: Optional[str] = None      # default: the analysed revision
    comment: bool = True
    check_run: bool = True


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
               allowed_hosts: Optional[list] = None, audit_key: Optional[bytes] = None,
               scm: Optional[ScmConfig] = None, uploads: Optional[UploadConfig] = None) -> FastAPI:
    app = FastAPI(title="TIA - Technical Impact Analysis")
    scm = scm or ScmConfig()
    ucfg = uploads or UploadConfig()
    upload_root = Path(workspace) / ".uploads"
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
        return landscape_extracts(svc, store, names)

    def current_graph(ex: dict):
        """The persisted graph version for these extracts; the head advances when this is the whole landscape."""
        g, gid, _ = get_or_build(store, ex)
        if len(ex) == len(svc.list()):
            if store.set_head(LANDSCAPE, gid):
                store.audit("graph_version_advanced", graph=gid, repos={r: e.commit for r, e in sorted(ex.items())})
        return g, gid

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
        r = wrap(svc.register, body.url, body.path, body.name, body.token, body.branch).public()
        refresh_landscape(svc, store)
        return r

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
        try:
            r = svc.sync(name, body.token).public()
        except IngestError as e:
            refresh_landscape(svc, store)          # records the failed scan: relationships touching it become stale
            raise HTTPException(400, str(e))
        refresh_landscape(svc, store)
        return svc.get(name).public()

    @app.delete("/api/v1/repositories/{name}")
    def delete(name: str):
        wrap(svc.remove, name)
        refresh_landscape(svc, store)
        return {"deleted": name}

    # ---- code upload: a zip or a whole folder from the user's drive ----------------------------------------------
    def session(uid: str) -> Session:
        if not ucfg.enabled:
            raise HTTPException(403, "uploads are disabled on this server")
        try:
            return Session.open(upload_root, uid, ucfg, tenant_id)
        except IngestError as e:
            raise HTTPException(404, str(e))

    def reject(s: Session, e: IngestError):
        """Any rejection discards the whole staged upload (fail closed) and is audited."""
        s.discard()
        store.audit("upload_rejected", name=s.m["name"], reason=str(e)[:300])
        raise HTTPException(413 if isinstance(e, UploadTooLarge) else 400, str(e))

    def write_batch(s: Session, files: list, paths: list):
        s.use_source("folder")
        for i, f in enumerate(files):
            s.add(paths[i] if i < len(paths) else f.filename, iter(lambda: f.file.read(CHUNK), b""))

    def guarded(s: Session, fn, *a):
        try:
            return fn(*a)
        except IngestError as e:
            reject(s, e)
        finally:
            s.flush()

    async def receive_archive(s: Session, request: Request, strip_root: str):
        s.use_source("archive")
        limit_mb = ucfg.max_bytes // (1024 * 1024)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > ucfg.max_bytes:
            raise UploadTooLarge(f"upload exceeds the size limit ({limit_mb} MB)")
        zp, total = s.dir / "archive.zip", 0
        with open(zp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > ucfg.max_bytes:
                    raise UploadTooLarge(f"upload exceeds the size limit ({limit_mb} MB)")
                f.write(chunk)
        try:
            await run_in_threadpool(s.add_zip, zp, strip_root)
        finally:
            zp.unlink(missing_ok=True)

    async def archive_into(s: Session, request: Request, strip_root: str):
        try:
            await receive_archive(s, request, strip_root)
        except IngestError as e:
            reject(s, e)
        finally:
            s.flush()

    def finish_upload(s: Session) -> dict:
        label = f"Upload ({s.m['source']}): {s.m['files']} files, {s.m['bytes']} bytes"
        repo, sha, created = wrap(svc.commit_upload, s.m["name"], s.stage, label, s.m["files"], s.m["bytes"], s.m["skipped"])
        out = {"revision": sha, "files": s.m["files"], "bytes": s.m["bytes"], "skipped_files": s.m["skipped"],
               "unchanged": not created}
        s.discard()
        refresh_landscape(svc, store)
        return {**svc.get(repo.name).public(), **out}

    def upload_target(name: str) -> str:
        if not ucfg.enabled:
            raise HTTPException(403, "uploads are disabled on this server")
        n = wrap(_clean, name)
        existing = store.get_repo(n)
        if existing and existing.kind != "upload":
            raise HTTPException(400, f"a repository named '{n}' is already registered from {existing.provider}; "
                                     "choose another name")
        return n

    @app.post("/api/v1/uploads")
    def upload_start(body: UploadIn):
        n = upload_target(body.name)
        s = Session.create(upload_root, ucfg, tenant_id, n)
        return {"upload_id": s.sid, "name": n, "max_bytes": ucfg.max_bytes, "max_files": ucfg.max_files}

    @app.post("/api/v1/uploads/{uid}/files")
    async def upload_files(uid: str, request: Request):
        """A batch of folder files: parts named 'files' (and, in the same order, 'paths' with each relative path)."""
        s = session(uid)
        form = await request.form(max_files=2000, max_fields=4000)
        files, paths = form.getlist("files"), [str(x) for x in form.getlist("paths")]
        await run_in_threadpool(guarded, s, write_batch, s, files, paths)
        return {"files": s.m["files"], "bytes": s.m["bytes"], "skipped_files": s.m["skipped"]}

    @app.put("/api/v1/uploads/{uid}/archive")
    async def upload_archive(uid: str, request: Request, strip_root: str = "auto"):
        """The whole zip as the raw request body (streamed to disk, never held in memory)."""
        s = session(uid)
        await archive_into(s, request, strip_root)
        return {"files": s.m["files"], "bytes": s.m["bytes"], "skipped_files": s.m["skipped"]}

    @app.post("/api/v1/uploads/{uid}/commit")
    def upload_commit(uid: str):
        s = session(uid)
        if not s.m["files"]:
            raise HTTPException(400, "nothing has been uploaded yet")
        return finish_upload(s)

    @app.delete("/api/v1/uploads/{uid}")
    def upload_cancel(uid: str):
        session(uid).discard()
        return {"cancelled": uid}

    @app.put("/api/v1/repositories/{name}/upload")
    async def upload_zip_once(name: str, request: Request, strip_root: str = "auto"):
        """One call for scripts: PUT the zip as the body; creates (or adds a revision to) the repository `name`."""
        s = Session.create(upload_root, ucfg, tenant_id, upload_target(name))
        await archive_into(s, request, strip_root)
        return await run_in_threadpool(finish_upload, s)

    @app.get("/api/v1/repositories/{name}/revisions")
    def repo_revisions(name: str, limit: int = 50):
        return wrap(svc.revisions, name, min(max(limit, 1), 500))

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
            g, _ = current_graph(ex)
            view = build_view(ex, level, repos.split(",") if repos else None, q, focus, depth, min(max(limit, 10), 5000), graph=g,
                              stale={r["key"]: r for r in store.list_relationships("stale")})
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        return restrict(view, audience(x_tia_readable_repos))

    @app.get("/api/v1/graph/versions")
    def graph_versions(limit: int = 50):
        head = store.get_head(LANDSCAPE)
        return {"head": head, "versions": [{**v, "current": bool(head and head["graph_id"] == v["id"])}
                                           for v in store.list_graphs(min(max(limit, 1), 500))]}

    @app.get("/api/v1/graph/versions/{gid}")
    def graph_version(gid: str):
        v = next((x for x in store.list_graphs(500) if x["id"] == gid), None)
        if not v:
            raise HTTPException(404, "unknown graph version")
        return v

    @app.get("/api/v1/graph/relationships")
    def graph_relationships(state: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """Cross-repository relationships with their state: current, or stale with the reason (AC-011)."""
        if state not in (None, "current", "stale"):
            raise HTTPException(400, "state must be 'current' or 'stale'")
        ok = audience(x_tia_readable_repos)
        rows = [r for r in store.list_relationships(state) if ok is None or (r["consumer_repo"] in ok and r["provider_repo"] in ok)]
        return {"relationships": rows, "current": sum(1 for r in rows if r["state"] == "current"),
                "stale": sum(1 for r in rows if r["state"] == "stale")}

    @app.post("/api/v1/graph/rescan")
    def graph_rescan():
        """Full rescan of every repository (ignores caches), with drift detection against the incremental result."""
        return rescan_landscape(svc, store)

    @app.get("/api/v1/graph/stats")
    def graph_stats(x_tia_readable_repos: Optional[str] = Header(None)):
        ex = landscape()
        rows = landscape_stats(ex, current_graph(ex)[0] if ex else None)
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

    # ---- SCM webhooks (authenticated by signature, not by session) ------------------------------------------------
    @app.post("/api/v1/webhooks/github")
    async def github_webhook(request: Request, response: Response, x_hub_signature_256: Optional[str] = Header(None),
                             x_github_event: Optional[str] = Header(None), x_github_delivery: Optional[str] = Header(None)):
        """GitHub pull_request events. Returns quickly: verification and matching happen here, the work is queued."""
        if not scm.github_webhook_secret:
            raise HTTPException(503, "GitHub webhook receiver is not configured (TIA_GITHUB_WEBHOOK_SECRET)")
        raw = await request.body()
        if not verify_signature(scm.github_webhook_secret, raw, x_hub_signature_256):
            raise HTTPException(401, "invalid webhook signature")        # not audited: unauthenticated callers must not fill the log
        if x_github_event == "ping":
            return {"ok": True, "event": "ping"}
        if x_github_event != "pull_request":
            response.status_code = 202
            return {"ignored": f"event '{x_github_event}' is not handled"}
        try:
            ev = parse_pull_request(json.loads(raw))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if ev is None:
            response.status_code = 202
            return {"ignored": "pull_request action needs no analysis"}
        if not scm.github_token:
            raise HTTPException(503, "no GitHub token is configured (TIA_GITHUB_TOKEN)")
        repo = match_repo(svc.list(), ev["clone_url"])
        if repo is None:
            store.audit("webhook_ignored", provider="github", delivery=x_github_delivery, repo=ev["full_name"],
                        reason="repository not registered")
            response.status_code = 202
            return {"ignored": f"repository '{ev['full_name']}' is not registered in TIA"}
        payload = {"provider": "github", "repo": repo.name, "full_name": ev["full_name"], "pr": ev["pr"],
                   "head_sha": ev["head_sha"], "base_ref": ev["base_ref"]}
        job = jobs.enqueue(tenant_id, "github_pull_request", payload, pr_event_job_key(payload), pr_event_group_key(payload))
        store.audit("webhook_accepted", provider="github", delivery=x_github_delivery, repo=ev["full_name"], pr=ev["pr"],
                    action=ev["action"], head=ev["head_sha"], job=job["job_id"], deduplicated=job["deduplicated"])
        response.status_code = 202
        return {**job, "repository": repo.name, "pr": ev["pr"]}

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

    @app.get("/api/v1/impact-analyses/{aid}/evidence")
    def evidence(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """AC-015: the evidence snapshot and the exact evidence path behind every material finding."""
        rep = store.get_analysis(aid)
        snap = store.get_snapshot(rep.get("evidence_snapshot_id", "")) if rep else None
        if not rep or not snap:
            raise HTTPException(404, "unknown analysis, or it has no materialised evidence snapshot")
        ok = audience(x_tia_readable_repos)
        return evidence_view(redact(rep, ok), redact_snapshot(snap, ok))

    @app.post("/api/v1/impact-analyses/{aid}/publish")
    def publish(aid: str, body: PublishIn):
        """Post (or update in place) the advisory comment and check run for a stored analysis on a GitHub pull request."""
        rep = store.get_analysis(aid)
        if not rep:
            raise HTTPException(404, "unknown analysis id")
        repo = next((r for r in svc.list() if r.name == body.repository), None)
        if repo is None:
            raise HTTPException(404, f"repository '{body.repository}' is not registered")
        full_name = github_full_name(repo.url)
        if not full_name:
            raise HTTPException(400, f"repository '{repo.name}' has no GitHub remote to publish to")
        if rep["change"]["repository"] != repo.name:
            raise HTTPException(400, f"analysis {aid} is of '{rep['change']['repository']}', not '{repo.name}'")
        head = body.head_sha or rep["change"]["proposed_revision"]
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise HTTPException(400, "head_sha must be a full 40-character commit sha")
        try:
            res = publish_to_github(rep, target_repo=repo.name, full_name=full_name, pr=body.pr, head_sha=head, scm=scm,
                                    comment=body.comment, check_run=body.check_run)
        except PublishError as e:
            raise HTTPException(503 if "TIA_GITHUB_TOKEN" in str(e) else 502, str(e))
        audit_publication(store, provider="github", full_name=full_name, pr=body.pr, analysis_id=aid, result=res)
        return {"analysis_id": aid, "repository": repo.name, "pr": body.pr, "head_sha": head, **res}

    @app.post("/api/v1/impact-analyses/{aid}/reproduce")
    def reproduce_analysis(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """Re-run the analysis on its pinned snapshot; the result must be identical (AC-015)."""
        r = wrap(reproduce, svc, store, aid)
        return {**r, "report": redact(r["report"], audience(x_tia_readable_repos))}

    @app.get("/api/v1/evidence-snapshots/{sid}")
    def snapshot(sid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        snap = store.get_snapshot(sid)
        if not snap:
            raise HTTPException(404, "unknown evidence snapshot")
        return redact_snapshot(snap, audience(x_tia_readable_repos))

    @app.get("/api/v1/impact-analyses/{aid}/paths")
    def paths(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return [{"finding": f["id"], "component": f["component"], "evidence_status": f["evidence_status"],
                 "path": f["path"], "path_edges": f["path_edges"]} for f in get(aid, x_tia_readable_repos)["findings"]]

    @app.get("/api/v1/impact-analyses/{aid}/unknowns")
    def unknowns(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return get(aid, x_tia_readable_repos)["unknowns"]

    return app
