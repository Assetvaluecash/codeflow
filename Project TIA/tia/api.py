"""TIA HTTP API and web UI (TFD section 8). Requires the `api` extra.

Repositories are ingested from GitHub, Azure DevOps, any git URL, or (in development mode) a local path;
analyses run against the registered repositories. Analyses run synchronously here; the contract - POST
returns an analysis id, GET retrieves the immutable report - matches the asynchronous design.

Authentication and authorisation (PL-001, PL-002) live in authapi.py: with TIA_AUTH_REQUIRED every request is resolved to a principal
(service token, OIDC bearer token or browser session) that carries its TENANT and roles; without it (development) everyone is an
administrator on the deployment's one tenant. X-TIA-Readable-Repos can only narrow a principal's repository access, never widen it.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote as _url_quote

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import authapi
from .identity import current_principal
from .graphview import LEVELS, build_view, impact_overlay, landscape_stats, restrict
from .analysis_service import (execute, group_key, job_key, landscape_extracts, pr_event_group_key, pr_event_job_key,
                               refresh_landscape, reproduce, rescan_landscape, resolve)
from .graphstore import LANDSCAPE, get_or_build
from .publish import PublishError, audit_publication, comment_scope, publish_to_github
from .reportpage import not_found_page, render_page
from .tenantconfig import DEFAULT as DEFAULT_CONFIG, ConfigError, diff as config_diff, parse as parse_config
from .webhooks import (ScmConfig, github_full_name, match_repo, parse_pull_request, verify_signature, with_stored_token,
                       with_tenant_config)
from . import CONFIG_VERSION, EXTRACTOR_VERSION, metrics, obs, tracing
from . import health as probes      # not 'health': create_app defines an endpoint function of that name
from .gate import GateError, TenantGate
from .jobs import JobQueue
from .repos import IngestError, RepoService, _clean
from .upload import CHUNK, Session, UploadConfig, UploadTooLarge
from .report import evidence_view, redact, redact_snapshot, render_comment
from .db import Database
from .access import AccessStore, MODES as ACCESS_MODES
from .githubapp import AppError, TokenSource, installation_tenant, my_installations
from .secretstore import SecretStore
from .store import Store

WEB = Path(__file__).parent / "web"


class RepoIn(BaseModel):
    url: Optional[str] = None
    path: Optional[str] = None
    name: Optional[str] = None
    token: Optional[str] = None
    branch: Optional[str] = None


class DiscoverIn(BaseModel):
    provider: str                       # github | ado | github-app (every repository of this organisation's GitHub App installations)
    org: Optional[str] = None
    project: Optional[str] = None       # required for ado
    token: Optional[str] = None
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


class InstallationIn(BaseModel):
    installation_id: int
    code: str                           # GitHub's user-authorisation code from the installation redirect: proof the caller can see the installation


class AccessIn(BaseModel):
    mode: Optional[str] = None
    grants: Optional[list[str]] = None      # user ids; replaces the grants of a 'restricted' repository


class ConfigIn(BaseModel):
    config: dict
    comment: Optional[str] = None


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
    max_depth: Optional[int] = None     # default and ceiling: the tenant's configured limit
    max_results: Optional[int] = None


def create_app(db: Database, tenant_id: str, workspace: str = ".tia-data/repos", allow_local: bool = False,
               allowed_hosts: Optional[list] = None, audit_key: Optional[bytes] = None,
               scm: Optional[ScmConfig] = None, uploads: Optional[UploadConfig] = None, gate_ttl: float = 5.0,
               default_quota: Optional[int] = None, secrets=None, hsts: bool = False,
               auth: Optional[authapi.AuthSettings] = None, auth_http: Optional[dict] = None) -> FastAPI:
    auth = auth or authapi.AuthSettings()
    # With authentication on, interactive docs would be an unauthenticated map of the API.
    app = FastAPI(title="TIA - Technical Impact Analysis", **({"docs_url": None, "redoc_url": None, "openapi_url": None} if auth.required else {}))
    scm = scm or ScmConfig()
    ucfg = uploads or UploadConfig()
    upload_root = Path(workspace) / ".uploads"
    jobs = JobQueue(db)
    tokens = TokenSource(scm, db)

    # One deployment serves many tenants: the authenticated principal names the tenant, and the services below are that tenant's own
    # (its store, its repository workspace, its quota gate, its secret store). Without authentication the one configured tenant is used.
    class Ctx:
        def __init__(self, tid: str):
            ws = str(Path(workspace) / tid) if auth.required else workspace      # tenants never share a clone directory
            self.tenant_id = tid
            self.store = Store(db, tid, audit_key)
            self.svc = RepoService(self.store, ws, allow_local=allow_local, allowed_hosts=allowed_hosts)
            self.gate = TenantGate(db, tid, ws, default_quota, gate_ttl)
            self.access = AccessStore(db, tid)
            self.secrets = (secrets if tid == str(tenant_id) else SecretStore(db, tid, secrets.wrapper)) if secrets is not None else None

    contexts: dict = {}
    ctx_lock = threading.Lock()

    def ctx_for(t: str) -> Ctx:
        c = contexts.get(t)
        if c is None:
            with ctx_lock:
                c = contexts.get(t)
                if c is None:
                    if len(contexts) >= 512:
                        contexts.clear()                                    # cheap objects; rebuilt on demand
                    c = contexts[t] = Ctx(t)
        return c

    def tid() -> str:
        return authapi.current_tenant.get() or str(tenant_id)

    class _Current:
        """Attribute access goes to the current request's tenant services."""

        def __init__(self, name):
            object.__setattr__(self, "_name", name)

        def __getattr__(self, attr):
            return getattr(getattr(ctx_for(tid()), self._name), attr)

    store, svc, gate = _Current("store"), _Current("svc"), _Current("gate")
    default = ctx_for(str(tenant_id))
    authn = authapi.Auth(db, str(tenant_id), auth, ctx_for, lambda t: ctx_for(t).secrets, **(auth_http or {}))

    @app.middleware("http")
    async def tenant_lifecycle(request: Request, call_next):
        """A suspended tenant gets 403 and an offboarded one 410 on every API call, the report page and the webhook."""
        path = request.url.path
        if path.startswith(("/api/", "/analyses/")) and path != "/api/v1/health":
            try:
                await run_in_threadpool(gate.check_active)
            except GateError as e:
                metrics.GATE_DENIALS.labels(code=e.code).inc()
                return JSONResponse({"detail": e.detail, "code": e.code}, status_code=e.status_code, headers={"X-TIA-Code": e.code})
        return await call_next(request)

    app.state.auth = authn
    authapi.install(app, authn, lambda t: authapi.current_tenant.set(t))

    @app.middleware("http")
    async def transport_security(request: Request, call_next):
        """HSTS on https requests (directly, or behind a TLS-terminating proxy that says so)."""
        resp = await call_next(request)
        if hsts and (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    quiet_routes = {"/healthz", "/readyz", "/metrics", "/api/v1/health"}

    @app.middleware("http")
    async def observe(request: Request, call_next):
        """Outermost: request id, trace span, metrics and one structured log line for every request, including refused ones."""
        rid = obs.new_request_id(request.headers.get("x-request-id"))
        token = obs.request_id_var.set(rid)
        method = request.method if request.method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS") else "OTHER"
        metrics.HTTP_IN_FLIGHT.inc()
        t0, status, route = time.perf_counter(), 500, "unmatched"
        try:
            with tracing.span("http.request", "server", parent=tracing.parse_traceparent(request.headers.get("traceparent")),
                              method=method) as sp:
                try:
                    resp = await call_next(request)
                    status = resp.status_code
                    matched = request.scope.get("route")
                    route = getattr(matched, "path", None) or "unmatched"
                    sp.set(route=route, status=status)
                    if status >= 500:
                        sp.status = "error"
                    resp.headers["X-Request-ID"] = rid
                    resp.headers["X-Trace-Id"] = sp.trace_id
                    return resp
                finally:
                    dur = time.perf_counter() - t0               # still inside the span: the log line carries its trace and span ids
                    metrics.HTTP_REQUESTS.labels(method=method, route=route, status=status).inc()
                    metrics.HTTP_DURATION.labels(method=method, route=route).observe(dur)
                    (obs.http_log.debug if route in quiet_routes else obs.http_log.info)(
                        "request", extra={"method": method, "route": route, "status": status, "duration_ms": round(dur * 1000, 2)})
        finally:
            metrics.HTTP_IN_FLIGHT.dec()
            obs.request_id_var.reset(token)

    metrics.BUILD_INFO.labels(extractors=EXTRACTOR_VERSION, config=CONFIG_VERSION).set(1)
    probes.register_collectors(db, tenant_id, default.store, ttl=float(os.environ.get("TIA_METRICS_CACHE_TTL", "5")))

    def github_token(repo_url: Optional[str] = None, full: Optional[str] = None, hint=None, explicit: Optional[str] = None) -> Optional[str]:
        """The GitHub credential for a repository of this tenant: what the caller supplied, else an installation token (GitHub App), else the
        interim token. A GitHub App problem is reported as what it means."""
        if explicit:
            return explicit
        try:
            return tokens.token_for(tid(), repo_url, full, hint, fallback=scm_now().github_token)
        except AppError as e:
            raise HTTPException(503 if e.code in ("not_configured", "bad_key") else 502 if e.code == "unreachable" else 424, str(e))

    def scm_now():
        """The connector configuration with the token the tenant has stored (encrypted) if none is set in the environment."""
        return with_stored_token(scm, ctx_for(tid()).secrets)

    def room(adding: int = 0):
        """Refuse work that grows storage once the tenant's quota is used up."""
        try:
            gate.check_room(adding)
        except GateError as e:
            metrics.GATE_DENIALS.labels(code=e.code).inc()
            raise HTTPException(e.status_code, e.detail, headers={"X-TIA-Code": e.code})

    def declared(request: Request) -> int:
        v = request.headers.get("content-length")
        return int(v) if v and v.isdigit() else 0

    def audience(h):
        """Repositories the caller may see: what the principal may read, narrowed (never widened) by the X-TIA-Readable-Repos header."""
        asked = [x.strip() for x in h.split(",") if x.strip()] if h else None
        allowed = readable(current_principal.get())
        if allowed is None:
            return asked
        return sorted(set(allowed) & set(asked)) if asked is not None else sorted(allowed)

    def readable(p):
        """None = every repository of the tenant; otherwise the names this principal may read (service-token scope, SCM mirror, grants)."""
        if p is None or p.kind == "system":
            return None
        return ctx_for(p.tenant_id).access.readable(p, ctx_for(p.tenant_id).svc.list())

    def visible(name: str) -> bool:
        allowed = readable(current_principal.get())
        return allowed is None or name in allowed

    def public_if_visible(r: dict) -> dict:
        """Details (branches, commit, location) of a repository only to someone who may read it; the registrar of a hosted repository may not be one."""
        return r if visible(r["name"]) else {"name": r["name"], "provider": r.get("provider"), "note": "registered; you do not have access to read it"}

    def require_repo(name: str):
        """A repository the caller cannot read does not exist for them."""
        if not visible(name):
            raise HTTPException(404, f"repository '{name}' is not registered")

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
        cfg = store.effective_config()
        g, gid, _ = get_or_build(store, ex, tuple(cfg.strip_prefixes), cfg.test_naming)
        if len(ex) == len(svc.list()):
            if store.set_head(LANDSCAPE, gid):
                store.audit("graph_version_advanced", graph=gid, repos={r: e.commit for r, e in sorted(ex.items())})
        return g, gid

    # ---- UI / meta ----------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Liveness: the process is serving. No dependencies."""
        return probes.liveness()

    @app.get("/readyz", include_in_schema=False)
    def readyz(response: Response):
        """Readiness of the platform (database, schema, workspace, queue consumers); never about analysis completeness or tenant status."""
        code, body = probes.readiness(db, tenant_id, workspace)
        response.status_code = code
        return body

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint(request: Request):
        ok, why = probes.metrics_allowed(request.client.host if request.client else None, request.headers.get("authorization"))
        if not ok:
            return PlainTextResponse(why + "\n", status_code=403)
        return PlainTextResponse(metrics.REGISTRY.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    # ---- tenant configuration (versioned; every change is a new immutable version) ---------------------------------------------
    CFG = "/api/v1/tenant/config"

    def actor_of(h: Optional[str]) -> str:
        p = current_principal.get()
        if p is not None and p.kind != "system":
            return p.actor                                  # the authenticated principal; a header can no longer name someone else
        return h if h and re.fullmatch(r"[A-Za-z0-9@._ -]{1,64}", h) else "api"          # development mode only

    @app.get(CFG)
    def get_tenant_config():
        """The configuration in force (defaults filled in), the built-in defaults, and who set it."""
        cfg = store.effective_config()
        meta = store.get_config_version(cfg.version) if cfg.version else None
        return {"id": cfg.id, "version": cfg.version, "is_default": cfg.is_default, "config": cfg.to_dict(), "defaults": DEFAULT_CONFIG.to_dict(),
                **({k: meta[k] for k in ("comment", "actor", "created_at")} if meta else {})}

    @app.post(CFG + "/validate")
    def validate_tenant_config(body: ConfigIn):
        """Dry run: every problem with a configuration, or what it would change. Saves nothing."""
        try:
            new = parse_config(body.config)
        except ConfigError as e:
            return {"valid": False, "problems": e.problems}
        return {"valid": True, "config": new.to_dict(), "changes": config_diff(store.effective_config().to_dict(), new.to_dict())}

    @app.put(CFG)
    def put_tenant_config(body: ConfigIn, x_tia_actor: Optional[str] = Header(None)):
        """Save a new version (a no-op when identical to the current one). Invalid configurations are refused with every problem listed."""
        try:
            r = store.put_config(body.config, body.comment, actor_of(x_tia_actor))
        except ConfigError as e:
            raise HTTPException(422, {"message": "invalid configuration", "problems": e.problems})
        if r["created"]:
            store.audit("tenant_config_changed", version=r["version"], id=r["id"], actor=actor_of(x_tia_actor), comment=body.comment,
                        changes=[c["path"] for c in r["changes"]])
        return r

    @app.get(CFG + "/versions")
    def tenant_config_versions(limit: int = 50):
        return store.list_config_versions(min(max(limit, 1), 500))

    @app.get(CFG + "/versions/{n}")
    def tenant_config_version(n: int):
        v = store.get_config_version(n)
        if not v:
            raise HTTPException(404, "unknown configuration version")
        return v

    @app.get(CFG + "/diff")
    def tenant_config_diff(from_version: int, to_version: int):
        """Setting-by-setting differences between two versions (0 = the built-in defaults)."""
        def settings(n):
            if n == 0:
                return DEFAULT_CONFIG.to_dict()
            v = store.get_config_version(n)
            if not v:
                raise HTTPException(404, f"unknown configuration version {n}")
            return v["config"]
        return {"from": from_version, "to": to_version, "changes": config_diff(settings(from_version), settings(to_version))}

    @app.post(CFG + "/versions/{n}/restore")
    def tenant_config_restore(n: int, x_tia_actor: Optional[str] = Header(None)):
        """Roll back by saving an old version's settings as a NEW version: history is never rewritten."""
        v = store.get_config_version(n)
        if not v:
            raise HTTPException(404, "unknown configuration version")
        r = store.put_config(v["config"], f"restored from version {n}", actor_of(x_tia_actor))
        if r["created"]:
            store.audit("tenant_config_changed", version=r["version"], id=r["id"], actor=actor_of(x_tia_actor),
                        comment=f"restored from version {n}", changes=[c["path"] for c in r["changes"]])
        return r

    @app.get("/api/v1/tenant")
    def tenant_state():
        """This tenant's own lifecycle status and storage use against its quota (approximate: row sizes plus workspace bytes)."""
        return gate.summary()

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "repositories": len(svc.list())}

    @app.get("/api/v1/config")
    def config():
        return {"allow_local_paths": allow_local, "allowed_git_hosts": allowed_hosts or [],
                "auth": "required" if auth.required else "none (development mode)"}

    # ---- repositories -------------------------------------------------------------------------------------
    @app.post("/api/v1/repositories")
    def add_repo(body: RepoIn):
        room()
        tok = body.token
        if tok is None and body.url and tokens.uses_app and github_full_name(body.url):
            tok = github_token(body.url)                      # a GitHub repository: clone with an installation token
        r = wrap(svc.register, body.url, body.path, body.name, tok, body.branch).public()
        refresh_landscape(svc, store)
        gate.invalidate()
        return public_if_visible(r)

    @app.get("/api/v1/repositories")
    def list_repos():
        allowed = readable(current_principal.get())
        return [r.public() for r in svc.list() if allowed is None or r.name in allowed]

    @app.post("/api/v1/repositories/discover")
    def discover(body: DiscoverIn):
        if body.ingest:
            room()
        if body.provider == "github-app":
            return discover_app(body)
        if not body.org or not body.token:
            raise HTTPException(422, "org and token are required for this provider")
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

    def discover_app(body: DiscoverIn):
        if not tokens.uses_app:
            raise HTTPException(503, "the GitHub App is not configured on this server (TIA_GITHUB_APP_ID, TIA_GITHUB_APP_PRIVATE_KEY)")
        mine = my_installations(db, tid())
        if not mine:
            raise HTTPException(424, "no GitHub App installation is linked to this organisation yet")
        found, ok, failed = [], [], []
        try:
            for inst in mine:
                for r in tokens.app.repositories(inst["installation_id"]):
                    found.append({"name": r["name"], "url": r["clone_url"], "private": r["private"], "installation": inst["installation_id"]})
        except AppError as e:
            raise HTTPException(424, str(e))
        if not body.ingest:
            return {"discovered": found, "ingested": [], "failed": []}
        for r in found:
            if body.names and r["name"] not in body.names:
                continue
            try:
                tok = github_token(r["url"], hint=r["installation"])
                ok.append(public_if_visible(svc.register(url=r["url"], name=r["name"], token=tok).public()))
            except IngestError as e:
                failed.append({"name": r["name"], "error": str(e)})
        refresh_landscape(svc, store)
        gate.invalidate()
        return {"discovered": found, "ingested": ok, "failed": failed}

    @app.post("/api/v1/repositories/{name}/sync")
    def sync(name: str, body: SyncIn = SyncIn()):
        require_repo(name)
        room()
        try:
            repo = svc.get(name)
            tok = body.token or (github_token(repo.url) if tokens.uses_app and repo.provider == "github" else None)
            r = svc.sync(name, tok).public()
        except IngestError as e:
            refresh_landscape(svc, store)          # records the failed scan: relationships touching it become stale
            raise HTTPException(400, str(e))
        refresh_landscape(svc, store)
        gate.invalidate()
        return svc.get(name).public()

    @app.delete("/api/v1/repositories/{name}")
    def delete(name: str):
        require_repo(name)
        wrap(svc.remove, name)
        refresh_landscape(svc, store)
        return {"deleted": name}

    # ---- code upload: a zip or a whole folder from the user's drive ----------------------------------------------
    def session(uid: str) -> Session:
        if not ucfg.enabled:
            raise HTTPException(403, "uploads are disabled on this server")
        try:
            return Session.open(upload_root, uid, ucfg, tid())
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
        if store.get_repo(n) is not None:
            require_repo(n)                               # adding a revision to a repository needs access to it
        existing = store.get_repo(n)
        if existing and existing.kind != "upload":
            raise HTTPException(400, f"a repository named '{n}' is already registered from {existing.provider}; "
                                     "choose another name")
        return n

    @app.post("/api/v1/uploads")
    def upload_start(body: UploadIn):
        room()
        n = upload_target(body.name)
        s = Session.create(upload_root, ucfg, tid(), n)
        return {"upload_id": s.sid, "name": n, "max_bytes": ucfg.max_bytes, "max_files": ucfg.max_files}

    @app.post("/api/v1/uploads/{uid}/files")
    async def upload_files(uid: str, request: Request):
        """A batch of folder files: parts named 'files' (and, in the same order, 'paths' with each relative path)."""
        s = session(uid)
        await run_in_threadpool(room, declared(request))
        form = await request.form(max_files=2000, max_fields=4000)
        files, paths = form.getlist("files"), [str(x) for x in form.getlist("paths")]
        await run_in_threadpool(guarded, s, write_batch, s, files, paths)
        return {"files": s.m["files"], "bytes": s.m["bytes"], "skipped_files": s.m["skipped"]}

    @app.put("/api/v1/uploads/{uid}/archive")
    async def upload_archive(uid: str, request: Request, strip_root: str = "auto"):
        """The whole zip as the raw request body (streamed to disk, never held in memory)."""
        s = session(uid)
        await run_in_threadpool(room, declared(request))
        await archive_into(s, request, strip_root)
        return {"files": s.m["files"], "bytes": s.m["bytes"], "skipped_files": s.m["skipped"]}

    @app.post("/api/v1/uploads/{uid}/commit")
    def upload_commit(uid: str):
        s = session(uid)
        if not s.m["files"]:
            raise HTTPException(400, "nothing has been uploaded yet")
        room(s.m["bytes"])                                  # the committed copy lives in the workspace as well
        return finish_upload(s)

    @app.delete("/api/v1/uploads/{uid}")
    def upload_cancel(uid: str):
        session(uid).discard()
        return {"cancelled": uid}

    @app.put("/api/v1/repositories/{name}/upload")
    async def upload_zip_once(name: str, request: Request, strip_root: str = "auto"):
        """One call for scripts: PUT the zip as the body; creates (or adds a revision to) the repository `name`."""
        await run_in_threadpool(room, declared(request))
        s = Session.create(upload_root, ucfg, tid(), upload_target(name))
        await archive_into(s, request, strip_root)
        await run_in_threadpool(room, s.m["bytes"])
        out = await run_in_threadpool(finish_upload, s)
        gate.invalidate()
        return out

    @app.get("/api/v1/repositories/{name}/revisions")
    def repo_revisions(name: str, limit: int = 50):
        require_repo(name)
        return wrap(svc.revisions, name, min(max(limit, 1), 500))

    # ---- who can read which repository (PL-002) ---------------------------------------------------------------
    @app.get("/api/v1/admin/repo-access")
    def repo_access_list():
        """How every repository is governed, who is granted, and how fresh the mirror of the source-code host's permissions is."""
        acc = ctx_for(tid()).access
        modes, grants, status = acc.modes(), acc.grants(), {s["repo"]: s for s in acc.status()}
        from .access import default_mode
        return {"freshness_limit_seconds": acc.age,
                "repositories": [{"name": r.name, "provider": r.provider, "mode": modes.get(r.name) or default_mode(r.provider),
                                  "explicit": r.name in modes, "grants": sorted(grants.get(r.name, ())), "mirror": status.get(r.name)}
                                 for r in svc.list()]}

    @app.put("/api/v1/admin/repo-access/{name}")
    def repo_access_set(name: str, body: AccessIn):
        acc = ctx_for(tid()).access
        if store.get_repo(name) is None:
            raise HTTPException(404, f"repository '{name}' is not registered")
        if body.mode is None and body.grants is None:
            raise HTTPException(422, "give a mode, grants, or both")
        known = {u["id"] for u in authn.ids(tid()).list_users()}
        if body.grants is not None and not set(body.grants) <= known:
            raise HTTPException(422, "grants must be ids of known users")
        try:
            if body.mode is not None:
                acc.set_mode(name, body.mode, actor_of(None))
            if body.grants is not None:
                acc.set_grants(name, body.grants, actor_of(None))
        except ValueError as e:
            raise HTTPException(422, str(e))
        store.audit("repo_access_changed", repo=name, mode=body.mode, grants=body.grants if body.grants is None else len(body.grants))
        return {"repository": name, "mode": acc.modes().get(name), "grants": sorted(acc.grants(name).get(name, ()))}

    @app.post("/api/v1/admin/repo-access/sync")
    def repo_access_sync():
        """Refresh the mirror of who the source-code host says can read each hosted repository. Needs a token with push access to them."""
        acc = ctx_for(tid()).access
        token = scm_now().github_token
        if tokens.uses_app:
            def token(r):                                          # one installation token per repository's installation
                return tokens.token_for(tid(), r.url)
        elif not token:
            raise HTTPException(503, "no GitHub token is configured (TIA_GITHUB_TOKEN, or the tenant's stored github/token)")
        res = acc.sync(svc.list(), token, scm_now().github_api_url)
        store.audit("repo_access_synced", repositories=len(res), failed=[r["repo"] for r in res if not r["ok"]])
        return {"results": res}

    # ---- GitHub App installations of this organisation ---------------------------------------------------------------
    GHI = "/api/v1/admin/github/installations"

    @app.get(GHI)
    def installations_list():
        return {"app_configured": tokens.uses_app, "linking_available": bool(tokens.uses_app and scm.github_app_client_id and scm.github_app_client_secret),
                "installations": my_installations(db, tid())}

    @app.post(GHI)
    def installations_link(body: InstallationIn):
        """Link a GitHub App installation to this organisation. The caller must prove, through GitHub's own user authorisation, that they
        can see the installation: knowing its id is not enough."""
        if not tokens.uses_app:
            raise HTTPException(503, "the GitHub App is not configured on this server")
        try:
            info = tokens.app.installation(body.installation_id)
            if not tokens.app.user_sees_installation(body.code, body.installation_id, info["account"], info.get("account_type", "Organization")):
                store.audit("installation_link_refused", installation_id=str(body.installation_id), reason="the authorising GitHub user does not own the account")
                raise HTTPException(403, "the GitHub account you authorised does not own that installation")
        except AppError as e:
            raise HTTPException(503 if e.code in ("not_configured", "bad_key") else 502 if e.code == "unreachable" else 424, str(e))
        with db.tx(tid()) as c:
            outcome = c.execute("select tia_map_installation('github', %s, %s)", (str(body.installation_id), info["account"])).fetchone()[0]
        if outcome != "mapped":
            raise HTTPException(409, "that installation is already linked to an organisation")
        store.audit("installation_linked", installation_id=str(body.installation_id), account=info["account"])
        return {"installation_id": str(body.installation_id), "account": info["account"]}

    @app.delete(GHI + "/{iid}")
    def installations_unlink(iid: str):
        with db.tx(tid()) as c:
            ok = c.execute("select tia_unmap_installation('github', %s)", (iid,)).fetchone()[0]
        if not ok:
            raise HTTPException(404, "that installation is not linked to this organisation")
        tokens.app.forget(iid) if tokens.uses_app else None
        store.audit("installation_unlinked", installation_id=iid)
        return {"unlinked": iid}

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

    def only_readable_inputs(v: dict) -> dict:
        """A graph version records which repository was at which commit: only those the caller may read."""
        allowed = readable(current_principal.get())
        inputs = v.get("inputs")
        if allowed is None or not isinstance(inputs, dict) or not isinstance(inputs.get("repos"), dict):
            return v
        return {**v, "inputs": {**inputs, "repos": {k: x for k, x in inputs["repos"].items() if k in allowed}}}

    @app.get("/api/v1/graph/versions")
    def graph_versions(limit: int = 50):
        head = store.get_head(LANDSCAPE)
        return {"head": head, "versions": [{**only_readable_inputs(v), "current": bool(head and head["graph_id"] == v["id"])}
                                           for v in store.list_graphs(min(max(limit, 1), 500))]}

    @app.get("/api/v1/graph/versions/{gid}")
    def graph_version(gid: str):
        v = next((x for x in store.list_graphs(500) if x["id"] == gid), None)
        if not v:
            raise HTTPException(404, "unknown graph version")
        return only_readable_inputs(v)

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
        out = rescan_landscape(svc, store)
        allowed = readable(current_principal.get())
        if allowed is not None:                                  # which repositories exist, at which commit, with which errors: only for those readable
            out = {**out, "repositories": [x for x in out["repositories"] if x["repo"] in allowed]}
        return out

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
        require_repo(body.target)
        room()
        params = body.model_dump()
        if async_:
            if body.token:
                raise HTTPException(400, "tokens are not accepted for queued analyses; sync the repository first")
            concrete = wrap(resolve, svc, params, False, store.effective_config())
            tp = tracing.current_traceparent()                       # the worker continues this request's trace
            job = jobs.enqueue(tid(), "impact_analysis", {**concrete, **({"_traceparent": tp} if tp else {})}, job_key(concrete),
                               group_key(concrete))
            store.audit("analysis_queued", job=job["job_id"], target=concrete["target"], head=concrete["head"],
                        deduplicated=job["deduplicated"])
            response.status_code = 202
            return {**job, "target": concrete["target"], "base": concrete["base"], "head": concrete["head"]}
        concrete = wrap(resolve, svc, params, True, store.effective_config())
        rep = wrap(execute, svc, store, concrete)
        gate.invalidate()
        return {"analysis_id": rep["analysis_id"], "status": rep["status"],
                "report": redact(rep, audience(x_tia_readable_repos))}

    # ---- SCM webhooks (authenticated by signature, not by session) ------------------------------------------------
    @app.post("/api/v1/webhooks/github")
    async def github_webhook(request: Request, response: Response, x_hub_signature_256: Optional[str] = Header(None),
                             x_github_event: Optional[str] = Header(None), x_github_delivery: Optional[str] = Header(None)):
        """GitHub pull_request events. Returns quickly: verification and matching happen here, the work is queued."""
        if not scm.github_webhook_secret:
            raise HTTPException(503, "GitHub webhook receiver is not configured (TIA_GITHUB_WEBHOOK_SECRET)")
        try:
            raw = await authapi.read_capped(request, 26 * 1024 * 1024)          # GitHub's own payload cap is 25 MB; read nothing unbounded before the signature
        except authapi.AuthError:
            raise HTTPException(413, "webhook payload too large")
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
        if tokens.uses_app:
            # the platform's App serves many organisations: the installation says whose event this is
            owner = await run_in_threadpool(installation_tenant, db, ev["installation_id"]) if ev.get("installation_id") is not None else None
            if owner is None:
                response.status_code = 202
                return {"ignored": "the installation is not linked to an organisation of this server"}
            authapi.current_tenant.set(owner)
        elif not (await run_in_threadpool(scm_now)).github_token:
            raise HTTPException(503, "no GitHub token is configured (TIA_GITHUB_TOKEN)")
        await run_in_threadpool(room)
        repo = match_repo(svc.list(), ev["clone_url"])
        if repo is None:
            store.audit("webhook_ignored", provider="github", delivery=x_github_delivery, repo=ev["full_name"],
                        reason="repository not registered")
            response.status_code = 202
            return {"ignored": f"repository '{ev['full_name']}' is not registered in TIA"}
        payload = {"provider": "github", "repo": repo.name, "full_name": ev["full_name"], "pr": ev["pr"],
                   "head_sha": ev["head_sha"], "base_ref": ev["base_ref"]}
        tp = tracing.current_traceparent()
        job = jobs.enqueue(tid(), "github_pull_request", {**payload, **({"installation_id": ev["installation_id"]} if ev.get("installation_id") else {}),
                                                          **({"_traceparent": tp} if tp else {})},
                           pr_event_job_key(payload, store.effective_config()), pr_event_group_key(payload))
        store.audit("webhook_accepted", provider="github", delivery=x_github_delivery, repo=ev["full_name"], pr=ev["pr"],
                    action=ev["action"], head=ev["head_sha"], job=job["job_id"], deduplicated=job["deduplicated"])
        response.status_code = 202
        return {**job, "repository": repo.name, "pr": ev["pr"]}

    @app.get("/api/v1/jobs/{job_id}")
    def job_status(job_id: str):
        j = jobs.get(tid(), job_id, with_payload=True)
        pl = j.pop("payload", None) if j else None
        if not j or (pl and not visible(pl.get("target") or pl.get("repo") or "")):
            raise HTTPException(404, "unknown job")
        return {**j, "is_latest": jobs.is_latest(tid(), job_id)}

    @app.delete("/api/v1/jobs/{job_id}")
    def job_cancel(job_id: str):
        j = jobs.get(tid(), job_id, with_payload=True)
        pl = (j or {}).get("payload") or {}
        if j and not visible(pl.get("target") or pl.get("repo") or ""):
            raise HTTPException(404, "unknown job")
        r = jobs.cancel(tid(), job_id)
        if not r:
            raise HTTPException(409, "job is not queued or running")
        store.audit("job_cancelled", job=job_id, result=r)
        return {"job_id": job_id, "result": r}

    def get(aid: str, h):
        rep = store.get_analysis(aid)
        if not rep or not visible(rep["change"]["repository"]):
            raise HTTPException(404, "unknown analysis id")
        return redact(rep, audience(h))

    @app.get("/api/v1/impact-analyses/{aid}")
    def report(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        return get(aid, x_tia_readable_repos)

    PAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
                                               "frame-ancestors 'none'",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-cache"}

    @app.get("/analyses/{aid}", response_class=HTMLResponse, include_in_schema=False)
    def analysis_page(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """The shareable report page (UC1-16) that the PR comment and check run link to. Same access rule as the JSON API."""
        rep = store.get_analysis(aid)
        if not rep or not visible(rep["change"]["repository"]):
            return HTMLResponse(not_found_page(), status_code=404, headers=PAGE_HEADERS)
        return HTMLResponse(render_page(redact(rep, audience(x_tia_readable_repos)), f"/api/v1/impact-analyses/{_url_quote(aid)}"),
                            headers=PAGE_HEADERS)

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
        if not rep or not snap or not visible(rep["change"]["repository"]):
            raise HTTPException(404, "unknown analysis, or it has no materialised evidence snapshot")
        ok = audience(x_tia_readable_repos)
        return evidence_view(redact(rep, ok), redact_snapshot(snap, ok))

    @app.post("/api/v1/impact-analyses/{aid}/publish")
    def publish(aid: str, body: PublishIn):
        """Post (or update in place) the advisory comment and check run for a stored analysis on a GitHub pull request."""
        rep = store.get_analysis(aid)
        if not rep or not visible(rep["change"]["repository"]):
            raise HTTPException(404, "unknown analysis id")
        repo = next((r for r in svc.list() if r.name == body.repository and visible(r.name)), None)
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
            scm_t = dataclasses.replace(with_tenant_config(scm_now(), store.effective_config()),
                                        github_token=github_token(repo.url) if tokens.uses_app else scm_now().github_token)
            res = publish_to_github(rep, target_repo=repo.name, full_name=full_name, pr=body.pr, head_sha=head, scm=scm_t,
                                    comment=body.comment, check_run=body.check_run,
                                    comment_repos=comment_scope(ctx_for(tid()).access, svc, scm_t, repo.name, auth.required))
        except PublishError as e:
            raise HTTPException(503 if "TIA_GITHUB_TOKEN" in str(e) else 502, str(e))
        audit_publication(store, provider="github", full_name=full_name, pr=body.pr, analysis_id=aid, result=res)
        return {"analysis_id": aid, "repository": repo.name, "pr": body.pr, "head_sha": head, **res}

    @app.post("/api/v1/impact-analyses/{aid}/reproduce")
    def reproduce_analysis(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """Re-run the analysis on its pinned snapshot; the result must be identical (AC-015)."""
        get(aid, None)                                                  # 404 for an analysis of a repository the caller cannot read
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
