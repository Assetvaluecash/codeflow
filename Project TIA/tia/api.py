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
from datetime import datetime, timezone
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
from pydantic import BaseModel, Field

from . import authapi, kfadapter
from .identity import current_principal
from .graphview import LEVELS, build_view, impact_overlay, landscape_stats, restrict
from .analysis_service import (coordination_status, execute, notify_owners, group_key, job_key, landscape_extracts, pr_event_group_key, pr_event_job_key,
                               refresh_landscape, reproduce, rescan_landscape, resolve)
from .graphstore import LANDSCAPE, get_or_build
from .testscope import labels_of, rule_gaps, shared_components
from .automation import api_inventory, summarise_api, ui_inventory
from .coverage import assess_test_case, coverage_report, summary_of as summary_of_coverage
from .graph import build_graph
from .evidence import (KINDS as EVIDENCE_KINDS, RESULTS, EvidenceError, EvidenceStore, parse_coverage, parse_deployments,
                       parse_edges, parse_junit, parse_otlp, parse_test_json)
from .enrich import deployment_view
from .temporal import compare as temporal_compare, consumers_of, deployed_at
from .incidents import investigate, parse_incident
from .retention import audit_report
from . import calibration, findingfilter, ratelimit
from .kms import TenantKeyWrapper
from .saas import PlanError, TenantPlan, usage as saas_usage
from .policy import evaluate as evaluate_policy, required_owners
from .registry import RegistryError, merged_config, parse_registry
from . import itsm, mcp
from .report import redact_policy
from .tenantconfig import TenantConfig

EVIDENCE_MAX = 25 * 1024 * 1024
from .scrub import scrub_json
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
from .report import evidence_view, permalink, redact, redact_snapshot, render_comment
from .db import Database
from .access import AccessStore, MODES as ACCESS_MODES
from .githubapp import AppError, TokenSource, installation_tenant, my_installations
from .ado import AdoConfig, AdoError
from . import scmhosts
from . import ado as ado_mod
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


class ChangeRecordIn(BaseModel):
    change_record: Optional[str] = Field(None, max_length=40)


class VerdictIn(BaseModel):
    finding_id: str = Field(..., pattern=r"^F\d{1,6}$")
    verdict: str = Field(..., pattern="^(confirmed|false_positive|irrelevant)$")
    comment: Optional[str] = Field(None, max_length=1000)


class AckIn(BaseModel):
    team: str = Field(..., min_length=1, max_length=120)
    comment: Optional[str] = Field(None, max_length=1000)


class ApprovalIn(BaseModel):
    owner: str = Field(..., min_length=1, max_length=120)
    decision: str = Field(..., pattern="^(approve|reject)$")
    comment: Optional[str] = Field(None, max_length=1000)


class TestCaseIn(BaseModel):
    title: str = Field("", max_length=500)
    steps: str = Field("", max_length=20000)
    expected: str = Field("", max_length=5000)
    entry_point: str = Field("", max_length=500)          # e.g. "POST /accounts/{id}/payments" or a page route
    id: Optional[str] = Field(None, max_length=200)


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
               auth: Optional[authapi.AuthSettings] = None, auth_http: Optional[dict] = None, ado_cfg: Optional[AdoConfig] = None) -> FastAPI:
    auth = auth or authapi.AuthSettings()
    # With authentication on, interactive docs would be an unauthenticated map of the API.
    app = FastAPI(title="TIA - Technical Impact Analysis", **({"docs_url": None, "redoc_url": None, "openapi_url": None} if auth.required else {}))
    scm = scm or ScmConfig()
    ucfg = uploads or UploadConfig()
    upload_root = Path(workspace) / ".uploads"
    jobs = JobQueue(db)
    tokens = TokenSource(scm, db)
    ado_cfg = ado_cfg or AdoConfig()

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
            self.plan = TenantPlan(db, tid)
            # the platform's key-encryption key, or the tenant's own customer-managed key (BYOK) when the operator set one
            self.secrets = (SecretStore(db, tid, TenantKeyWrapper(lambda: self.plan.settings()["key_ref"], secrets.wrapper))
                            if secrets is not None else None)

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

    limiter = ratelimit.RateLimiter(ratelimit.Limits(default_on=auth.required))
    app.state.rate_limiter = limiter

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        """T17: token buckets per principal, per tenant and per unauthenticated client address (see tia.ratelimit)."""
        p = current_principal.get()
        principal = (p.token_id or p.user_id or p.actor) if p is not None and p.kind != "system" else None
        wait = limiter.check(request.url.path, request.method, tid() if principal else None, principal, ratelimit.client_address(request))
        if wait:
            metrics.RATE_LIMITED.labels(caller="principal" if principal else "anonymous").inc()
            return JSONResponse({"detail": "too many requests: slow down and retry", "code": "rate_limited"}, status_code=429,
                                headers={"Retry-After": str(max(1, int(wait + 0.999))), "X-TIA-Code": "rate_limited"})
        return await call_next(request)

    @app.middleware("http")
    async def tenant_lifecycle(request: Request, call_next):
        """A suspended tenant gets 403 and an offboarded one 410 on every API call, the report page and the webhook."""
        path = request.url.path
        if path.startswith(("/api/", "/analyses/")) and path != "/api/v1/health":
            try:
                await run_in_threadpool(gate.check_active)
                await run_in_threadpool(lambda: ctx_for(tid()).plan.check_region())       # PL-015: never serve a tenant of another region
            except (GateError, PlanError) as e:
                metrics.GATE_DENIALS.labels(code=e.code).inc()
                return JSONResponse({"detail": e.detail, "code": e.code}, status_code=e.status_code, headers={"X-TIA-Code": e.code})
        return await call_next(request)

    app.state.auth = authn
    authapi.install(app, authn, lambda t: authapi.current_tenant.set(t))

    @app.middleware("http")
    async def failed_auth_limit(request: Request, call_next):
        """Outside authentication: an address whose requests keep failing authentication is slowed down (T17)."""
        client = ratelimit.client_address(request)
        wait = limiter.failed_auth_blocked(client)
        if wait and request.url.path not in ratelimit.EXEMPT:
            metrics.RATE_LIMITED.labels(caller="failed_auth").inc()
            return JSONResponse({"detail": "too many failed authentications: retry later", "code": "rate_limited"}, status_code=429,
                                headers={"Retry-After": str(max(1, int(wait + 0.999))), "X-TIA-Code": "rate_limited"})
        resp = await call_next(request)
        if resp.status_code == 401:
            limiter.charge_failed_auth(client)
        return resp

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

    def within_plan(what: str, adding: int = 1):
        """PL-014: refuse an action that would exceed the tenant's plan (nothing already stored is touched)."""
        try:
            ctx_for(tid()).plan.check(what, adding)
        except PlanError as e:
            metrics.GATE_DENIALS.labels(code=e.code).inc()
            store.audit("plan_limit_reached", what=what, detail=e.detail)
            raise HTTPException(e.status_code, e.detail, headers={"X-TIA-Code": e.code})

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
        within_plan("repositories")
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
        if s.m.get("name") not in {r.name for r in svc.list()}:
            within_plan("repositories")
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
        elif not token and not any(r.provider in ("gitlab", "bitbucket") for r in svc.list()):
            raise HTTPException(503, "no GitHub token is configured (TIA_GITHUB_TOKEN, or the tenant's stored github/token)")
        gh = token

        def per_repo(r):                                           # GitLab / Bitbucket repositories use their host's token
            if r.provider in ("gitlab", "bitbucket"):
                return host_token(r.provider)
            return gh(r) if callable(gh) else gh
        res = acc.sync(svc.list(), per_repo, scm_now().github_api_url)
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

    @app.get("/api/v1/graph/shared-components")
    def graph_shared_components(min_entry_points: int = 2, limit: int = 100, x_tia_readable_repos: Optional[str] = Header(None)):
        """The landscape seen by a tester before any change: endpoints and functions that many entry points depend on (a change to them needs
        the widest testing), and rules on a shared endpoint's data that some consumers apply and others do not. Confirmed relationships only."""
        ex = landscape()
        if not ex:
            return {"shared_components": [], "rule_gaps": [], "withheld": 0}
        g, gid = current_graph(ex)
        rows = shared_components(g, ex, max(min_entry_points, 2), min(max(limit, 1), 1000))
        gaps = rule_gaps(g, ex, labels=labels_of(ex))
        ok = audience(x_tia_readable_repos)
        withheld = 0
        if ok is not None:
            kept = []
            for r in rows:
                if r["repo"] not in ok:
                    withheld += 1
                    continue
                users = [u for u in r["used_by"] if u["repo"] in ok]
                if len(users) != len(r["used_by"]):
                    r = {**r, "used_by": users, "used_by_withheld": len(r["used_by"]) - len(users)}
                kept.append(r)
            rows = kept
            n = len(gaps)
            gaps = [x for x in gaps if x["repo"] in ok and all(y["repo"] in ok for y in x["applied_by"] + x["missing_in"])]
            withheld += n - len(gaps)
        return {"graph_version": gid, "shared_components": rows, "rule_gaps": gaps, "withheld": withheld,
                "basis": "confirmed relationships only; a rule counts when it tests fields of the shared endpoint's declared response schema"}

    # ---- tester views: automation inventory (UC-3) and technical coverage (UC-2) ---------------------------------------------
    def tester_scope(repos: Optional[str], header: Optional[str]):
        """Extracts and graph of the repositories the caller may read (optionally narrowed): nothing in an unreadable repository is
        matched, counted or named."""
        ok = audience(header)
        names = [x.strip() for x in repos.split(",") if x.strip()] if repos else None
        if names:
            for n in names:
                require_repo(n)
        ex = {n: e for n, e in landscape(names).items() if ok is None or n in ok}
        for n in names or []:
            if n not in ex:
                raise HTTPException(404, f"repository '{n}' is not registered")
        if not ex:
            return ex, None
        if names is None and len(ex) == len(svc.list()):
            return ex, current_graph(ex)[0]
        cfg = store.effective_config()
        return ex, build_graph(ex, tuple(cfg.strip_prefixes), cfg.test_naming)

    @app.get("/api/v1/automation/api")
    def automation_api(repos: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """Every endpoint with its inputs (mandatory, multi-valued, allowed values, limits), generated valid and invalid values, a test
        matrix with expected outcomes, and the error statuses its handler answers with."""
        ex, _ = tester_scope(repos, x_tia_readable_repos)
        items = api_inventory(ex, labels_of(ex))
        return scrub_json({"endpoints": items, "summary": summarise_api(items)})[0]

    @app.get("/api/v1/automation/ui")
    def automation_ui(repos: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """Every page (route) with its elements: a unique locator for each, mandatory flags, input rules, dropdown values, and the APIs
        the page calls."""
        ex, _ = tester_scope(repos, x_tia_readable_repos)
        pages = ui_inventory(ex)
        return scrub_json({"pages": pages, "summary": {"pages": len(pages), "elements": sum(p["summary"]["elements"] for p in pages),
                                                       "without_unique_locator": sum(p["summary"]["without_unique_locator"] for p in pages)}})[0]

    @app.get("/api/v1/coverage")
    def coverage(repos: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """The technical aspects of every entry point and page, and which automated tests and written test cases address each."""
        ex, g = tester_scope(repos, x_tia_readable_repos)
        if not ex:
            return {"entry_points": [], "unmatched_written_tests": [], "summary": summary_of_coverage([], 0, 0, 0)}
        return scrub_json(coverage_report(ex, g))[0]

    @app.post("/api/v1/coverage/test-case")
    def coverage_test_case(body: TestCaseIn, repos: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """One test case as a query: the entry points it addresses, which of their technical aspects it covers and which it misses."""
        ex, g = tester_scope(repos, x_tia_readable_repos)
        if not ex:
            raise HTTPException(404, "no repository to assess against")
        return scrub_json(assess_test_case(ex, g, body.model_dump()))[0]

    # ---- ingested evidence (FR-022 to FR-029): pushed before an analysis, never queried live during one -----------------------------
    def evstore():
        return EvidenceStore(store.db, store.tenant_id)

    def require_registered(name: str):
        require_repo(name)
        if name not in {r.name for r in svc.list()}:
            raise HTTPException(404, f"repository '{name}' is not registered")

    async def evidence_body(request: Request) -> str:
        try:
            raw = await authapi.read_capped(request, EVIDENCE_MAX)
        except authapi.AuthError:
            raise HTTPException(413, f"evidence larger than {EVIDENCE_MAX // 1048576} MB")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "evidence must be UTF-8 text")

    def ev_actor():
        p = current_principal.get()
        return getattr(p, "name", None) or getattr(p, "subject", None) or "anonymous"

    def ev_put(kind, source, data, **kw):
        within_plan("evidence")
        r = evstore().put(kind, source, data, actor=ev_actor(), **kw)
        store.audit("evidence_ingested", kind=kind, source=source, id=r["id"], created=r["created"], repo=kw.get("repo"))
        return r

    @app.post("/api/v1/evidence/test-results")
    async def evidence_tests(request: Request, repo: str, revision: Optional[str] = None, build_id: Optional[str] = None,
                             environment: Optional[str] = None, report_ref: Optional[str] = None, format: str = "junit"):
        """CI test results for one run of a repository (JUnit XML, or JSON: pytest-json-report or a list of {name, classname, result})."""
        require_registered(repo)
        text = await evidence_body(request)

        def work():
            try:
                tests = parse_junit(text) if format == "junit" else parse_test_json(text) if format == "json" else None
            except EvidenceError as e:
                raise HTTPException(400, f"test results: {e}")
            if tests is None:
                raise HTTPException(400, "format must be junit or json")
            counts = {k: sum(1 for t in tests if t["result"] == k) for k in RESULTS}
            r = ev_put("test_run", f"ci:{build_id or 'unknown'}", {"build_id": build_id, "report_ref": report_ref, "tests": tests, "counts": counts},
                       repo=repo, revision=(revision or "").lower() or None, environment=environment, observed_to=datetime.now(timezone.utc))
            return {**r, "tests": len(tests), "counts": counts}
        return await run_in_threadpool(work)

    @app.post("/api/v1/evidence/coverage")
    async def evidence_coverage(request: Request, repo: str, revision: Optional[str] = None, format: Optional[str] = None):
        """Coverage of one run (Cobertura XML, JaCoCo XML, or coverage.py JSON with --show-contexts for per-test execution)."""
        require_registered(repo)
        text = await evidence_body(request)

        def work():
            try:
                cov = parse_coverage(text, format)
            except EvidenceError as e:
                raise HTTPException(400, f"coverage: {e}")
            r = ev_put("coverage", f"coverage:{cov['format']}", cov, repo=repo, revision=(revision or "").lower() or None,
                       observed_to=datetime.now(timezone.utc))
            return {**r, "format": cov["format"], "files": len(cov["files"]), "tests_with_contexts": len(cov["per_test"])}
        return await run_in_threadpool(work)

    @app.post("/api/v1/evidence/runtime")
    async def evidence_runtime(request: Request, format: str = "otlp", source: str = "telemetry"):
        """Observed service-to-service calls: OpenTelemetry traces (OTLP/JSON) or pre-aggregated edges. One record per observed edge."""
        text = await evidence_body(request)

        def work():
            try:
                edges = parse_otlp(text) if format == "otlp" else parse_edges(json.loads(text)) if format == "edges" else None
            except (EvidenceError, json.JSONDecodeError) as e:
                raise HTTPException(400, f"runtime evidence: {e}")
            if edges is None:
                raise HTTPException(400, "format must be otlp or edges")
            ids = [ev_put("runtime_edge", source, e, service=e["from_service"], environment=e.get("environment"),
                          observed_from=e.get("from_ts"), observed_to=e.get("to_ts"))["id"] for e in edges]
            return {"edges": len(edges), "ids": ids}
        return await run_in_threadpool(work)

    @app.post("/api/v1/evidence/deployments")
    async def evidence_deployments(request: Request, source: str = "pipeline"):
        """Deployments: [{repo or service, environment, revision, artifact, deployed_at}]."""
        text = await evidence_body(request)

        def work():
            try:
                deps = parse_deployments(json.loads(text))
            except (EvidenceError, json.JSONDecodeError) as e:
                raise HTTPException(400, f"deployments: {e}")
            for d in deps:
                if d["repo"]:
                    require_registered(d["repo"])
            ids = [ev_put("deployment", source, d, repo=d["repo"], service=d["service"], environment=d["environment"], revision=d["revision"],
                          observed_to=d["deployed_at"])["id"] for d in deps]
            return {"deployments": len(deps), "ids": ids}
        return await run_in_threadpool(work)

    @app.get("/api/v1/usage")
    def tenant_usage(month: Optional[str] = None):
        """PL-014: this organisation's metered usage and its plan (administrators)."""
        if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "month must be YYYY-MM")
        p = ctx_for(tid()).plan
        with db.tx(tid()) as c:
            u = saas_usage(c, tid(), month)
        s = p.settings()
        return {"usage": u, "plan": s["plan"], "region": s["region"], "customer_managed_key": bool(s["key_ref"])}

    @app.get("/api/v1/evidence")
    def evidence_list(kind: str, repo: Optional[str] = None, limit: int = 50, x_tia_readable_repos: Optional[str] = Header(None)):
        if kind not in EVIDENCE_KINDS:
            raise HTTPException(400, f"kind must be one of {list(EVIDENCE_KINDS)}")
        if repo:
            require_repo(repo)
        ok = audience(x_tia_readable_repos)
        rows = evstore().list(kind, repo=repo, limit=min(max(limit, 1), 500))
        rows = [r for r in rows if ok is None or r["repo"] in ok]
        for r in rows:                                          # a list is a summary: the bulky part of a record stays in the store
            if kind in ("test_run", "coverage"):
                r["data"] = {k: v for k, v in r["data"].items() if k not in ("tests", "files", "per_test")}
        return {"kind": kind, "records": rows}

    @app.delete("/api/v1/evidence/{rid}")
    def evidence_delete(rid: int):
        if not evstore().delete(rid):
            raise HTTPException(404, "no such evidence record")
        store.audit("evidence_deleted", id=rid)
        return {"deleted": rid}

    @app.get("/api/v1/environments")
    def environments(x_tia_readable_repos: Optional[str] = Header(None)):
        """FR-029: per repository and environment, the revision deployed; per environment, the runtime observations held."""
        ok = audience(x_tia_readable_repos)
        ex = {n: e for n, e in landscape().items() if ok is None or n in ok}
        es = evstore()
        view = deployment_view(ex, es.list("deployment", limit=5000), {r: e.commit for r, e in ex.items()}, "", "", "")
        svcs = {e.service for e in ex.values()}
        rt = [r for r in es.list("runtime_edge", limit=5000) if r["data"]["from_service"] in svcs and r["data"]["to_service"] in svcs]
        by_env: dict = {}
        for r in rt:
            k = r["data"].get("environment") or "unspecified"
            by_env[k] = by_env.get(k, 0) + 1
        return {**view, "runtime_edges_by_environment": by_env}

    # ---- Release 3: releases and temporal comparison (FR-011, FR-030, AC-033), incidents (FR-031, FR-032, AC-034), audit report -------
    def release_record(name: str) -> dict:
        found = [r for r in evstore().list("release", limit=5000) if r["data"]["name"] == name]
        if not found:
            raise HTTPException(404, f"no release named '{name}'")
        rel = found[0]
        for repo in rel["data"]["revisions"]:
            require_repo(repo)                                    # a release that includes a repository the caller cannot read is not theirs
        return rel

    def release_graph(revisions: dict):
        from .impact import _extract
        cfg = store.effective_config()
        strip = tuple(cfg.strip_prefixes)
        ex = {repo: _extract(repo, svc.get(repo).path, sha, store, strip) for repo, sha in sorted(revisions.items())}
        g, gid, _ = get_or_build(store, ex, strip, cfg.test_naming)
        return g, gid, ex

    @app.post("/api/v1/releases")
    async def release_create(request: Request):
        """A named release: {name, revisions: {repo: commit, tag or branch}, description}. The graph at exactly those revisions is built and kept."""
        body = await evidence_body(request)

        def work():
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                raise HTTPException(400, "body must be JSON")
            if not isinstance(d, dict):
                raise HTTPException(400, "body must be a JSON object")
            name, revs = d.get("name"), d.get("revisions")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", name):
                raise HTTPException(400, "name must be 1-80 characters: letters, digits and . _ : / -")
            if not isinstance(revs, dict) or not revs or len(revs) > 500:
                raise HTTPException(400, "revisions must map 1-500 repositories to a commit, tag or branch")
            resolved = {}
            for repo, ref in revs.items():
                require_registered(repo)
                if not isinstance(ref, str) or not ref:
                    raise HTTPException(400, f"revision of {repo} must be a commit, tag or branch name")
                try:
                    resolved[repo] = svc.resolve(repo, ref)
                except IngestError as e:
                    raise HTTPException(400, f"{repo}: {e}")
            if any(r["data"]["name"] == name and r["data"]["revisions"] != resolved for r in evstore().list("release", limit=5000)):
                raise HTTPException(409, f"a release named '{name}' already exists with other revisions")
            g, gid, _ = release_graph(resolved)
            data = {"name": name, "revisions": resolved, "requested": revs, "graph_id": gid,
                    "description": str(d.get("description") or "")[:500]}
            r = ev_put("release", "api", data, observed_to=datetime.now(timezone.utc))
            store.audit("release_recorded", release=name, graph=gid)
            return {**r, **data, "nodes": len(g.nodes), "edges": len(g.edges)}
        return await run_in_threadpool(work)

    @app.get("/api/v1/releases")
    def release_list(x_tia_readable_repos: Optional[str] = Header(None)):
        ok = audience(x_tia_readable_repos)
        rows = [r for r in evstore().list("release", limit=5000) if ok is None or set(r["data"]["revisions"]) <= set(ok)]
        return {"releases": [{"id": r["id"], "name": r["data"]["name"], "revisions": r["data"]["revisions"], "graph_id": r["data"]["graph_id"],
                              "description": r["data"].get("description"), "recorded_at": r["ingested_at"]} for r in rows]}

    @app.get("/api/v1/releases/compare")
    def release_compare(base: str, head: str, cross_service_only: bool = True):
        """AC-033: dependencies introduced, removed and modified between two releases."""
        a, b = release_record(base), release_record(head)
        ga, gida, _ = release_graph(a["data"]["revisions"])
        gb, gidb, _ = release_graph(b["data"]["revisions"])
        out = temporal_compare(ga, gb, cross_service_only=cross_service_only)
        return {"base": {"name": base, "graph_id": gida, "revisions": a["data"]["revisions"]},
                "head": {"name": head, "graph_id": gidb, "revisions": b["data"]["revisions"]}, **out}

    @app.get("/api/v1/releases/{name}/consumers")
    def release_consumers(name: str, component: str):
        """Who was known to depend on an endpoint, schema or topic in that release (component: a node id, or 'METHOD /path' of an endpoint)."""
        rel = release_record(name)
        g, gid, _ = release_graph(rel["data"]["revisions"])
        node = component if component in g.nodes else next(
            (n for n, at in g.nodes.items() if at.get("type") == "endpoint" and n.endswith(":" + component)), None)
        if node is None:
            raise HTTPException(404, f"'{component}' is not a component of release '{name}'")
        return {"release": name, "graph_id": gid, "component": node, "consumers": consumers_of(g, node)}

    @app.get("/api/v1/environments/at")
    def environments_at(at: str, repo: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """FR-011: which revision of each repository was running in each environment at a point in time (from deployment records)."""
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "at must be an ISO-8601 timestamp")
        when = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        if repo:
            require_repo(repo)
        ok = audience(x_tia_readable_repos)
        view = deployed_at(evstore().list("deployment", limit=20000), when, repo)
        return {"at": when, "deployed": {k: v for k, v in sorted(view.items()) if ok is None or k in ok}}

    def incident_record(iid: str) -> dict:
        found = [r for r in evstore().list("incident", limit=5000) if r["data"]["id"] == iid]
        if not found:
            raise HTTPException(404, f"no incident '{iid}'")
        if found[0]["repo"]:
            require_repo(found[0]["repo"])
        return found[0]

    @app.post("/api/v1/incidents")
    async def incident_ingest(request: Request, source: str = "itsm"):
        """FR-031: an incident record {id, title, service or repo, environment, started_at, ended_at, severity, changes: [{repo, revision,
        pull_request, change_record}]}."""
        body = await evidence_body(request)

        def work():
            try:
                inc = parse_incident(json.loads(body))
            except (ValueError, json.JSONDecodeError) as e:
                raise HTTPException(400, f"incident: {e}")
            if inc["repo"]:
                require_registered(inc["repo"])
            r = ev_put("incident", source, inc, repo=inc["repo"], service=inc["service"], environment=inc["environment"],
                       observed_from=inc["started_at"], observed_to=inc["ended_at"] or inc["started_at"])
            return {**r, "incident": inc["id"]}
        return await run_in_threadpool(work)

    @app.post("/api/v1/incidents/{iid}/links")
    async def incident_link(iid: str, request: Request):
        """A person's validated link between an incident and a change: {repo, revision, note}. Recorded with who and when."""
        body = await evidence_body(request)

        def work():
            incident_record(iid)
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                raise HTTPException(400, "body must be JSON")
            if not isinstance(d, dict) or not isinstance(d.get("repo"), str):
                raise HTTPException(400, "body must be {repo, revision, note}")
            repo, rev = d["repo"], str(d.get("revision") or "").lower()
            require_registered(repo)
            if not re.fullmatch(r"[0-9a-f]{7,40}", rev):
                raise HTTPException(400, "revision must be a commit sha (7-40 hex characters)")
            link = {"incident_id": iid, "repo": repo, "revision": rev, "note": str(d.get("note") or "")[:500], "by": ev_actor(),
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            r = ev_put("incident_link", "manual", link, repo=repo, revision=rev, observed_to=datetime.now(timezone.utc))
            store.audit("incident_linked", incident=iid, repo=repo, revision=rev)
            return {**r, **link}
        return await run_in_threadpool(work)

    @app.get("/api/v1/incidents/{iid}/investigation")
    def incident_investigation(iid: str, window_hours: Optional[float] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """FR-032, AC-034: candidate changes with how each is associated (never a root cause)."""
        inc = incident_record(iid)["data"]
        cfg = store.effective_config()
        w = window_hours if window_hours is not None else cfg.incident_window_hours
        if not 1 <= w <= 2160:
            raise HTTPException(400, "window_hours must be from 1 to 2160")
        es = evstore()
        ex = landscape()
        g = current_graph(ex)[0] if ex else None

        def analyses_for(repo, rev):
            try:
                return [a["analysis_id"] for a in store.find_analyses(repo, revision=rev, limit=5)]
            except Exception:                  # noqa: BLE001 - a lookup aid only
                return []
        out = investigate(inc, es.list("deployment", limit=20000), [r["data"] for r in es.list("incident_link", limit=5000)], g, ex, w, analyses_for)
        ok = audience(x_tia_readable_repos)
        if ok is not None:
            kept = [c for c in out["candidates"] if c["repo"] in ok]
            out["withheld"] = len(out["candidates"]) - len(kept)
            out["candidates"] = kept
        return out

    @app.get("/api/v1/audit/report")
    def audit_report_route(start: str, end: str):
        """Who did what in a period, by event and by actor (auditors)."""
        for v in (start, end):
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(400, "start and end must be ISO-8601 timestamps")
        with db.tx(tid()) as c:
            return audit_report(c, tid(), start, end)

    # ---- Release 4: calibration (FR-044, AC-038), trends (FR-043), multi-team coordination (FR-045) ---------------------------------
    @app.post("/api/v1/impact-analyses/{aid}/verdicts")
    def analysis_verdict(aid: str, body: VerdictIn, x_tia_readable_repos: Optional[str] = Header(None)):
        """A reviewer's verdict on one finding: confirmed, false_positive or irrelevant. The report is never changed."""
        rep = readable_report(aid, x_tia_readable_repos)
        f = next((x for x in rep.get("findings", []) if x.get("id") == body.finding_id), None)
        if f is None or not visible(f.get("repository") or rep["change"]["repository"]):
            raise HTTPException(404, f"analysis {aid} has no finding {body.finding_id}")
        data = {"analysis_id": aid, "finding_id": body.finding_id, "verdict": body.verdict, "comment": body.comment, "by": ev_actor(),
                "at": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "rules": calibration.rules_of(rep, f)}
        r = ev_put("verdict", "reviewer", data, repo=rep["change"]["repository"], revision=rep["change"].get("proposed_revision"))
        store.audit("finding_verdict", analysis=aid, finding=body.finding_id, verdict=body.verdict, id=r["id"])
        return {**r, **data}

    def stored_reports(since: Optional[str], until: Optional[str]) -> list:
        for v in (since, until):
            if v:
                try:
                    datetime.fromisoformat(v.replace("Z", "+00:00"))
                except ValueError:
                    raise HTTPException(400, "since and until must be ISO-8601 dates")
        return [r for r in store.list_analyses(since, until) if visible(r["change"]["repository"])]

    @app.get("/api/v1/calibration")
    def calibration_report(since: Optional[str] = None, until: Optional[str] = None):
        """FR-044: per-rule precision from reviewer verdicts and incident outcomes."""
        es = evstore()
        verdicts = [{**r["data"], "evidence_id": r["id"]} for r in es.list("verdict", limit=50000) if visible(r["repo"] or "")
                    and (not since or r["data"]["at"] >= since) and (not until or r["data"]["at"] < until)]
        incidents = [r["data"] for r in es.list("incident", limit=5000)]
        links = [r["data"] for r in es.list("incident_link", limit=5000)]

        def analyses_for(repo, rev):
            if not visible(repo):
                return []
            try:
                return store.find_analyses(repo, revision=rev, limit=5)
            except Exception:                  # noqa: BLE001 - a short or unknown revision simply has no analyses
                return []
        return calibration.precision(verdicts, calibration.incident_confirmations(incidents, links, analyses_for))

    @app.get("/api/v1/trends")
    def change_trends(group: str = "application", period: str = "month", since: Optional[str] = None, until: Optional[str] = None):
        """FR-043: descriptive change-risk figures per application or team and period (never probabilities)."""
        if group not in ("application", "team") or period not in ("week", "month", "quarter"):
            raise HTTPException(400, "group must be application or team; period week, month or quarter")
        reports = stored_reports(since, until)
        cfg = store.effective_config()
        owners = {}
        for r in reports:
            owners.update(r.get("owners") or {})
        owners.update(cfg.ownership)
        es = evstore()
        refs = [(c.get("repo"), c.get("revision") or c.get("commit")) for r in es.list("incident", limit=5000) for c in r["data"].get("changes") or []]
        refs += [(r["data"]["repo"], r["data"]["revision"]) for r in es.list("incident_link", limit=5000)]
        return calibration.trends(reports, owners, refs, group, period)

    @app.get("/api/v1/impact-analyses/{aid}/findings")
    def analysis_findings(aid: str, severity: Optional[str] = None, service: Optional[str] = None, reach: Optional[str] = None,
                          kind: Optional[str] = None, evidence: Optional[str] = None, environment: Optional[str] = None,
                          policy: Optional[str] = None, issue: Optional[str] = None, x_tia_readable_repos: Optional[str] = Header(None)):
        """PRD 9.3: the findings that match every filter given (comma-separated values are alternatives). reach: direct|transitive; kind:
        contract|data|test|runtime|operational; issue: unresolved|stale|inferred."""
        rep = redact(readable_report(aid, x_tia_readable_repos), audience(x_tia_readable_repos))
        try:
            return findingfilter.apply(rep, severity, service, reach, kind, evidence, environment, policy, issue)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/v1/impact-analyses/{aid}/coordination")
    def analysis_coordination(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """FR-045: per affected team, the notification sent and the acknowledgement received."""
        return coordination_status(store, readable_report(aid, x_tia_readable_repos))

    @app.post("/api/v1/impact-analyses/{aid}/notifications")
    def analysis_notify(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """Notify the owning teams of materially affected components (once per team; a failed delivery is retried)."""
        rep = readable_report(aid, x_tia_readable_repos)
        return notify_owners(store, rep, ctx_for(tid()).secrets, permalink(scm_now().public_url, aid), ev_actor())

    @app.post("/api/v1/impact-analyses/{aid}/acknowledgements")
    def analysis_acknowledge(aid: str, body: AckIn, x_tia_readable_repos: Optional[str] = Header(None)):
        """An affected team acknowledges that it has seen the change (recorded with who and when)."""
        rep = readable_report(aid, x_tia_readable_repos)
        st = coordination_status(store, rep)
        teams = [t["team"] for t in st["teams"]]
        if body.team not in teams:
            raise HTTPException(400, f"'{body.team}' is not a team this change affects" + (f" (affected: {', '.join(teams)})" if teams else " (none is)"))
        data = {"analysis_id": aid, "team": body.team, "comment": body.comment, "by": ev_actor(),
                "at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        r = ev_put("acknowledgement", "tia-api", data, repo=rep["change"]["repository"], revision=rep["change"].get("proposed_revision"))
        store.audit("team_acknowledged", analysis=aid, team=body.team, id=r["id"])
        return {"acknowledgement": {**r, **data}, "coordination": coordination_status(store, rep), "policy": policy_now(rep)}

    # ---- release policy: owner approvals and re-evaluation (FR-033, FR-034), registry import ------------------------------------------
    def report_config(rep: dict):
        """The configuration an analysis ran under (its snapshot), so a re-evaluation applies the same rules."""
        snap = store.get_snapshot(rep.get("evidence_snapshot_id", "")) if rep.get("evidence_snapshot_id") else None
        tc = (snap or {}).get("tenant_config")
        return TenantConfig.from_stored(tc["settings"], tc["version"], tc["digest"]) if tc and tc.get("version") else DEFAULT_CONFIG

    def policy_now(rep: dict) -> dict:
        cfg = report_config(rep)
        approvals = evstore().approvals(rep["change"]["repository"], rep["change"]["proposed_revision"])
        owners = {**(rep.get("owners") or {}), **cfg.ownership}
        crit = {**cfg.criticality}
        return evaluate_policy(rep, cfg.policies, crit, owners, approvals, coordination_status(store, rep))

    def readable_report(aid: str, header) -> dict:
        rep = store.get_analysis(aid)
        if not rep or not visible(rep["change"]["repository"]):
            raise HTTPException(404, "unknown analysis")
        return rep

    @app.get("/api/v1/impact-analyses/{aid}/policy")
    def analysis_policy(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """The release-policy outcome of an analysis with the approvals recorded so far (the stored report is never changed)."""
        rep = readable_report(aid, x_tia_readable_repos)
        pol = policy_now(rep)
        ok = audience(x_tia_readable_repos)
        return redact_policy(pol, rep, ok) if ok is not None else pol

    @app.post("/api/v1/impact-analyses/{aid}/approvals")
    def analysis_approval(aid: str, body: ApprovalIn, x_tia_readable_repos: Optional[str] = Header(None)):
        """An owning team's approval (or rejection) of a change that materially affects it."""
        rep = readable_report(aid, x_tia_readable_repos)
        need = required_owners(rep, {**(rep.get("owners") or {}), **report_config(rep).ownership})
        if body.owner not in need:
            raise HTTPException(400, f"'{body.owner}' is not an owner whose approval this change needs" + (f" (needed: {', '.join(need)})" if need else " (none is needed)"))
        data = {"analysis_id": aid, "owner": body.owner, "decision": body.decision, "comment": body.comment, "by": ev_actor(),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        r = ev_put("approval", "tia-api", data, repo=rep["change"]["repository"], revision=rep["change"]["proposed_revision"])
        store.audit("owner_approval", analysis=aid, owner=body.owner, decision=body.decision, id=r["id"])
        return {"approval": r, "policy": policy_now(rep)}

    @app.post("/api/v1/registry/import")
    async def registry_import(request: Request, format: str = "backstage", x_tia_actor: Optional[str] = Header(None)):
        """Owners and criticalities from the organisation's service catalog, saved as a new configuration version."""
        text = await evidence_body(request)

        def work():
            try:
                rows = parse_registry(text, format)
            except RegistryError as e:
                raise HTTPException(400, f"registry: {e}")
            cur = store.effective_config().to_dict()
            new = merged_config(cur, rows)
            try:
                r = store.put_config(new, f"registry import ({format}, {len(rows)} services)", actor_of(x_tia_actor))
            except ConfigError as e:
                raise HTTPException(422, {"message": "the registry produces an invalid configuration", "problems": e.problems})
            if r["created"]:
                store.audit("tenant_config_changed", version=r["version"], id=r["id"], actor=actor_of(x_tia_actor), comment="registry import",
                            changes=[c["path"] for c in r["changes"]])
            return {**r, "services": len(rows)}
        return await run_in_threadpool(work)

    # ---- change management (FR-038) -------------------------------------------------------------------------------------------------
    def attach_change_record(rep: dict, number: str) -> dict:
        cfg = store.effective_config()
        try:
            client = itsm.client_for(cfg.itsm, ctx_for(tid()).secrets)
            r = client.attach(number, rep, permalink(scm_now().public_url, rep["analysis_id"]))
        except itsm.ItsmError as e:
            store.audit("change_record_attach_failed", analysis=rep["analysis_id"], change_record=number, error=str(e)[:200])
            raise HTTPException(400 if e.permanent else 502, str(e))
        if r["created"]:
            ev_put("itsm_attachment", "servicenow", {"analysis_id": rep["analysis_id"], **r}, repo=rep["change"]["repository"])
        store.audit("change_record_attached", analysis=rep["analysis_id"], change_record=number, created=r["created"])
        return r

    @app.post("/api/v1/impact-analyses/{aid}/change-record")
    def analysis_change_record(aid: str, body: ChangeRecordIn):
        """Attach the report summary and its link to a change record (the PR's change record when none is given). Nothing else is modified."""
        rep = store.get_analysis(aid)
        if not rep or not visible(rep["change"]["repository"]):
            raise HTTPException(404, "unknown analysis id")
        number = body.change_record or rep["change"].get("change_record")
        if not number:
            raise HTTPException(400, "no change record: give change_record, or analyse with one")
        return attach_change_record(rep, number)

    # ---- MCP for coding assistants (FR-037): read-only tools --------------------------------------------------------------------------
    @app.post("/mcp")
    async def mcp_endpoint(request: Request, x_tia_readable_repos: Optional[str] = Header(None)):
        try:
            raw = await authapi.read_capped(request, 1024 * 1024)
            msg = json.loads(raw)
        except (authapi.AuthError, json.JSONDecodeError):
            return JSONResponse(mcp.handle(None, {}), status_code=400)

        def tool_report(a):
            try:
                return get(a["analysis_id"], x_tia_readable_repos)
            except HTTPException:
                raise mcp.ToolError(f"no analysis '{a['analysis_id']}' that you can see")

        def tool_find(a):
            require_repo_soft = visible(a["repository"])
            found = store.find_analyses(a["repository"], a.get("pull_request"), (a.get("revision") or "").lower() or None) if require_repo_soft else []
            if not found:
                raise mcp.ToolError("no report for that repository, pull request or revision that you can see")
            return get(found[0]["analysis_id"], x_tia_readable_repos)

        def tool_scope(a):
            rep = tool_report(a)
            return {"analysis_id": rep["analysis_id"], "status": rep["status"], "test_scope": rep.get("test_scope"),
                    "test_recommendations": rep.get("test_recommendations"), "test_gaps": rep.get("test_gaps"),
                    "unknowns": [u for u in rep.get("unknowns", []) if u.get("affects_confidence")]}

        def tool_policy(a):
            rep = tool_report(a)
            full = store.get_analysis(a["analysis_id"])
            ok = audience(x_tia_readable_repos)
            pol = policy_now(full)
            return {"analysis_id": rep["analysis_id"], **(redact_policy(pol, full, ok) if ok is not None else pol)}

        tools = {"get_impact_report": tool_report, "find_impact_report": tool_find, "get_test_scope": tool_scope, "get_policy_outcome": tool_policy}
        msgs = msg if isinstance(msg, list) else [msg]
        out = [r for r in (await run_in_threadpool(lambda: [mcp.handle(m, tools) for m in msgs])) if r is not None]
        if not out:
            return Response(status_code=202)
        return JSONResponse(out if isinstance(msg, list) else out[0])

    # ---- analyses ----------------------------------------------------------------------------------------------
    @app.post("/api/v1/impact-analyses")
    def create(body: AnalysisIn, response: Response, async_: bool = Query(False, alias="async"),
               x_tia_readable_repos: Optional[str] = Header(None)):
        """Synchronous by default. With ?async=true the analysis is queued and a job is returned (HTTP 202);
        queued analyses never accept tokens (nothing secret is stored in the queue)."""
        within_plan("analyses")
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
        rep = wrap(execute, svc, store, concrete, ctx_for(tid()).secrets)
        gate.invalidate()
        return {"analysis_id": rep["analysis_id"], "status": rep["status"],
                "report": redact(rep, audience(x_tia_readable_repos))}

    # ---- tester pack: one finished analysis -> what to retest + coverage gate, locator pack, API matrix (docs/CASE-STUDY.md) ----------
    def tester_pack_of(rep: dict, header: Optional[str]) -> dict:
        """rep: the caller's (already redacted) report. The pack is computed over the repositories the caller may read, the changed
        repository at its proposed revision and the others at the revisions the analysis used. Nothing is analysed anew: the extracts
        and the graph come from the caches the analysis filled."""
        from .gitutil import GitError
        from .impact import _extract
        from .testerpack import build_pack
        ok = audience(header)
        cfg = store.effective_config()
        strip, excl = tuple(cfg.strip_prefixes), tuple(cfg.exclude_patterns)
        revs = {x["repo"]: x["commit"] for x in (rep.get("coverage") or {}).get("repositories_analysed", [])}
        target = rep["change"]["repository"]
        if target in revs:
            revs[target] = rep["change"]["proposed_revision"]
        ex, gone = {}, []
        for repo, sha in sorted(revs.items()):
            if ok is not None and repo not in ok:
                continue
            try:
                ex[repo] = _extract(repo, svc.get(repo).path, sha, store, strip, excl)
            except (IngestError, GitError):
                gone.append(repo)                                  # removed since, or its commit is no longer in the clone
        g = get_or_build(store, ex, strip, cfg.test_naming)[0] if ex else None
        return scrub_json(build_pack(rep, ex, g, {r: e.commit for r, e in sorted(ex.items())}, gone))[0]

    @app.get("/api/v1/impact-analyses/{aid}/tester-pack")
    def tester_pack(aid: str, x_tia_readable_repos: Optional[str] = Header(None)):
        """For the entry points this change reaches: what to retest with a coverage gate (covered / gaps / not_evaluated), the locator
        pack of the pages that call them, and the API test matrix of the touched endpoints. Reads the finished report; starts nothing."""
        rep = get(aid, x_tia_readable_repos)
        if rep.get("status") == "failed":
            raise HTTPException(409, "the analysis failed: there is no impact to build a tester pack from")
        return tester_pack_of(rep, x_tia_readable_repos)

    # ---- Knowledge Fabric V2.5 door (TIA_KF_V2_INTEGRATION.docx, option B: a sibling of KF's /query) ----------------------------
    @app.post("/query/tia", response_model=kfadapter.QueryResponse)
    def kf_query(body: kfadapter.QueryRequest, x_tia_readable_repos: Optional[str] = Header(None)):
        """A finished Change Impact Report in Knowledge Fabric's QueryResponse shape. The analysis is named (analysis_id, or repository plus
        pull_request/revision for the newest such report); the question text is never parsed and no analysis is started here."""
        t0 = time.perf_counter()
        if body.intent != kfadapter.INTENT:
            return kfadapter.empty(body, [f"intent '{body.intent}' is not served by TIA; use intent '{kfadapter.INTENT}'"], t0)
        rep = None
        if body.analysis_id:
            rep = store.get_analysis(body.analysis_id)
        elif body.repository:
            if not visible(body.repository):
                return kfadapter.empty(body, ["no analysis found for that selection"], t0)
            found = store.find_analyses(body.repository, body.pull_request, body.revision.lower() if body.revision else None)
            rep = found[0] if found else None
        else:
            return kfadapter.empty(body, ["name the analysis: analysis_id, or repository with pull_request or revision "
                                          "(TIA does not infer a repository or a change from the question text)"], t0)
        if rep is None or not visible(rep["change"]["repository"]):
            return kfadapter.empty(body, ["no analysis found for that selection"], t0)
        red = redact(rep, audience(x_tia_readable_repos))
        pack = tester_pack_of(red, x_tia_readable_repos) if body.include_tester_pack and red.get("status") != "failed" else None
        return kfadapter.to_query_response(red, body, t0, pack)

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

    # ---- GitLab and Bitbucket (FR-039, AC-036): merge/pull-request events, publishing ------------------------------------------------
    def host_token(provider: str) -> Optional[str]:
        cfg = scmhosts.HostConfig.from_env(provider)
        sec = ctx_for(tid()).secrets
        return cfg.token or (sec.get_text(f"{provider}/token") if sec else None)

    async def scm_host_webhook(provider: str, request: Request, response: Response):
        hcfg = scmhosts.HostConfig.from_env(provider)
        if not hcfg.webhook_secret:
            raise HTTPException(503, f"{provider} receiver is not configured (TIA_{provider.upper()}_WEBHOOK_SECRET)")
        try:
            raw = await authapi.read_capped(request, 5 * 1024 * 1024)
        except authapi.AuthError:
            raise HTTPException(413, "webhook payload too large")
        if not scmhosts.verify(hcfg, request.headers, raw):
            raise HTTPException(401, "invalid webhook signature or token")          # not audited: unauthenticated callers must not fill the log
        try:
            ev = scmhosts.parse_event(hcfg, request.headers, json.loads(raw))
        except (ValueError, json.JSONDecodeError) as e:
            raise HTTPException(400, str(e))
        if ev is None:
            response.status_code = 202
            return {"ignored": "event needs no analysis"}
        await run_in_threadpool(room)
        repo = match_repo(svc.list(), ev["remote_url"])
        if repo is None:
            store.audit("webhook_ignored", provider=provider, repo=ev["full_name"], reason="repository not registered")
            response.status_code = 202
            return {"ignored": f"repository '{ev['full_name']}' is not registered in TIA"}
        payload = {"provider": provider, "repo": repo.name, "pr": ev["pr"], "head_sha": ev["head_sha"], "base_ref": ev["base_ref"]}
        meta = {k: ev[k] for k in ("full_name", "project_id", "source_ref")}
        tp = tracing.current_traceparent()
        job = jobs.enqueue(tid(), "scm_pull_request", {**payload, "host": meta, **({"_traceparent": tp} if tp else {})},
                           pr_event_job_key(payload, store.effective_config()), pr_event_group_key(payload))
        store.audit("webhook_accepted", provider=provider, repo=ev["full_name"], pr=ev["pr"], action=ev["event"], head=ev["head_sha"],
                    job=job["job_id"], deduplicated=job["deduplicated"])
        response.status_code = 202
        return {**job, "repository": repo.name, "pr": ev["pr"]}

    @app.post("/api/v1/webhooks/gitlab")
    async def gitlab_webhook(request: Request, response: Response):
        """GitLab merge request events (secret token). Answers at once; the analysis is queued."""
        return await scm_host_webhook("gitlab", request, response)

    @app.post("/api/v1/webhooks/bitbucket")
    async def bitbucket_webhook(request: Request, response: Response):
        """Bitbucket Cloud and Data Center pull-request events (HMAC signature). Answers at once; the analysis is queued."""
        return await scm_host_webhook("bitbucket", request, response)

    def publish_host(rep, aid, repo, body):
        hcfg = scmhosts.HostConfig.from_env(repo.provider)
        token = host_token(repo.provider)
        if not token:
            raise HTTPException(503, f"no {repo.provider} token is configured (TIA_{repo.provider.upper()}_TOKEN, or the tenant's stored {repo.provider}/token)")
        full = scmhosts.full_name(hcfg, repo.url)
        if not full:
            raise HTTPException(400, f"repository '{repo.name}' is not on the configured {repo.provider} host")
        if rep["change"]["repository"] != repo.name:
            raise HTTPException(400, f"analysis {aid} is of '{rep['change']['repository']}', not '{repo.name}'")
        head = body.head_sha or rep["change"]["proposed_revision"]
        from .publish import audience_view
        scm_t = with_tenant_config(scm_now(), store.effective_config())
        view = audience_view(rep, repo.name, scm_t, [repo.name] if auth.required else None)
        try:
            res = scmhosts.publish(hcfg, token, rep, view, full, body.pr, head, scm_t.public_url)
        except scmhosts.ScmHostError as e:
            raise HTTPException(502 if not e.permanent else 400, str(e))
        audit_publication(store, provider=repo.provider, full_name=full, pr=body.pr, analysis_id=aid, result=res)
        return {"analysis_id": aid, "repository": repo.name, "pr": body.pr, "head_sha": head, **res}

    @app.post("/api/v1/webhooks/ado")
    async def ado_webhook(request: Request, response: Response):
        """Azure DevOps service hooks (pull request created / updated). Answers at once; the analysis is queued."""
        if not ado_cfg.receiving:
            raise HTTPException(503, "Azure DevOps receiver is not configured (TIA_ADO_HOOK_USERNAME / TIA_ADO_HOOK_PASSWORD)")
        try:
            raw = await authapi.read_capped(request, 5 * 1024 * 1024)
        except authapi.AuthError:
            raise HTTPException(413, "service-hook payload too large")
        if not ado_mod.verify_basic(ado_cfg, request.headers.get("authorization")):
            raise HTTPException(401, "invalid service-hook credentials")        # not audited: unauthenticated callers must not fill the log
        try:
            ev = ado_mod.parse_pull_request_event(json.loads(raw))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if ev is None:
            response.status_code = 202
            return {"ignored": "event needs no analysis"}
        if not ado_mod.allowed_host(ado_cfg, ev["base_url"]):
            raise HTTPException(400, "the event's organisation is not on an Azure DevOps host this server will call")
        if ev.get("collection_id"):
            owner = await run_in_threadpool(installation_tenant, db, ev["collection_id"], "ado")
            if owner:
                authapi.current_tenant.set(owner)                               # the organisation is mapped to a tenant: its events go there
        await run_in_threadpool(room)
        repo = match_repo(svc.list(), ev["remote_url"])
        if repo is None:
            store.audit("webhook_ignored", provider="ado", repo=f"{ev['project']}/{ev['repository']}", reason="repository not registered")
            response.status_code = 202
            return {"ignored": f"repository '{ev['repository']}' is not registered in TIA"}
        payload = {"provider": "ado", "repo": repo.name, "pr": ev["pr"], "head_sha": ev["head_sha"], "base_ref": ev["base_ref"]}
        ado_meta = {k: ev[k] for k in ("base_url", "project", "repository_id", "repository")}
        tp = tracing.current_traceparent()
        job = jobs.enqueue(tid(), "ado_pull_request", {**payload, "ado": ado_meta, **({"_traceparent": tp} if tp else {})},
                           pr_event_job_key(payload, store.effective_config()), pr_event_group_key(payload))
        store.audit("webhook_accepted", provider="ado", repo=f"{ev['project']}/{ev['repository']}", pr=ev["pr"], action=ev["event"], head=ev["head_sha"],
                    job=job["job_id"], deduplicated=job["deduplicated"])
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
        remote = ado_mod.parse_remote(repo.url) if repo.provider == "ado" else None
        if remote is not None:
            return publish_ado(rep, aid, repo, remote, body)
        if repo.provider in ("gitlab", "bitbucket"):
            return publish_host(rep, aid, repo, body)
        full_name = github_full_name(repo.url)
        if not full_name:
            raise HTTPException(400, f"repository '{repo.name}' has no GitHub or Azure DevOps remote to publish to")
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

    def publish_ado(rep, aid, repo, remote, body):
        token = ado_cfg.token or (ctx_for(tid()).secrets.get_text("ado/token") if ctx_for(tid()).secrets else None)
        if not token:
            raise HTTPException(503, "no Azure DevOps token is configured (TIA_ADO_TOKEN, or the tenant's stored ado/token)")
        if not ado_mod.allowed_host(ado_cfg, remote["base_url"]):
            raise HTTPException(400, "the repository is not on an Azure DevOps host this server will call")
        scm_t = with_tenant_config(scm_now(), store.effective_config())
        from .publish import audience_view
        view = audience_view(rep, repo.name, scm_t, [repo.name] if auth.required else None)     # no Azure DevOps permission mirror: the target only
        try:
            res = ado_mod.publish(ado_cfg, token, rep, view, {**remote, "repository_id": remote["repository"], "pr": body.pr}, scm_t.public_url)
        except AdoError as e:
            raise HTTPException(502, str(e))
        from .publish import audit_publication
        audit_publication(store, provider="ado", full_name=f"{remote['project']}/{remote['repository']}", pr=body.pr, analysis_id=aid,
                          result={"comment": res["comment"], "check_run": res["status"]})
        return {"analysis_id": aid, "repository": repo.name, "pr": body.pr, **res}

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
