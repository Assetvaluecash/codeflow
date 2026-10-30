"""Resolve -> execute an impact analysis. Shared by the synchronous API and the queue worker.

resolve():  turns user-facing refs (branch, PR number, defaults) into a concrete, reproducible request
            (exact commit shas for the target and every other repository in scope).
execute():  runs the analysis for such a concrete request.
job_key():  idempotency key: the same concrete request is the same job.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from . import CONFIG_VERSION, EXTRACTOR_VERSION, EXTRACTOR_VERSIONS, gitutil
from .extraction import extract_commit
from .graph import Graph
from .graphstore import LANDSCAPE, get_or_build, graph_id, graph_inputs
from .relationships import UNVERIFIABLE, reconcile
from .impact import AnalysisRequest, _extract, analyze
from .repos import IngestError, RepoService
from .publish import PublishError, audit_publication, publish_to_github
from .report import differences
from .store import Store
from .webhooks import ScmConfig
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


def _iso(epoch: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def freshness_of(svc: RepoService, scope: list) -> dict:
    """Scan state of each repository in scope (FR-005): what the report must say about how current its evidence is."""
    out = {}
    for n in scope:
        r = svc.get(n)
        out[n] = {"scan_status": r.scan_status, "scan_error": r.scan_error, "last_successful_scan": _iso(r.last_successful_scan),
                  "last_analysed_revision": r.analysed_commit}
    return out


def execute(svc: RepoService, store: Store, c: dict) -> dict:
    paths = {n: svc.get(n).path for n in c["scope"]}
    scope = set(c["scope"])
    stale = [r for r in store.list_relationships("stale") if r["stale_reason"] in UNVERIFIABLE
             and r["consumer_repo"] in scope and r["provider_repo"] in scope]
    rep = analyze(AnalysisRequest(repos=paths, target=c["target"], base=c["base"], head=c["head"],
                                  revisions=c["revisions"], pull_request=c.get("pr"), change_record=c.get("change_record"),
                                  max_depth=c["max_depth"], max_results=c["max_results"],
                                  freshness=freshness_of(svc, c["scope"]), stale_relationships=stale), store)
    if rep["status"] != "failed":
        pin_analysis(svc, rep)
    return rep


def pin_analysis(svc: RepoService, rep: dict):
    """Keep every analysed commit (and the proposed head) reachable in TIA's clones."""
    commits = {r["repo"]: [r["commit"]] for r in rep["coverage"]["repositories_analysed"]}
    target = rep["change"]["repository"]
    commits.setdefault(target, []).extend([rep["change"]["base_revision"], rep["change"]["proposed_revision"]])
    for repo, shas in commits.items():
        try:
            svc.pin_commits(repo, shas)
        except IngestError:
            continue


def reproduce(svc: RepoService, store: Store, analysis_id: str) -> dict:
    """Re-run an analysis on its materialised evidence snapshot and say whether the result is identical (AC-015)."""
    orig = store.get_analysis(analysis_id)
    if not orig or orig.get("status") == "failed":
        raise IngestError("unknown analysis, or an analysis that failed has no evidence to reproduce")
    snap = store.get_snapshot(orig.get("evidence_snapshot_id", ""))
    if not snap:
        raise IngestError("this analysis has no materialised evidence snapshot")
    target = snap["target"]
    needed = {repo: [commit] for repo, commit in snap["revisions"].items()}
    needed[target] = sorted({snap["target_base"], snap["target_head"]})          # the proposed head is not in 'revisions'
    paths = {}
    for repo, commits in needed.items():
        for commit in commits:
            try:
                r = svc.get(repo)
                gitutil.resolve_rev(r.path, commit)
            except (IngestError, gitutil.GitError):
                raise IngestError(f"cannot reproduce: revision {commit[:7]} of repository '{repo}' is no longer available")
        paths[repo] = r.path
    new = analyze(AnalysisRequest(
        repos=paths, target=target, base=snap["target_base"], head=snap["target_head"],
        revisions={r: c for r, c in snap["revisions"].items() if r != target}, pull_request=orig["change"].get("pull_request"),
        change_record=orig["change"].get("change_record"), max_depth=snap["max_depth"], max_results=snap["max_results"],
        test_naming=snap["test_naming"], strip_prefixes=tuple(snap["strip_prefixes"]), freshness=snap["freshness"],
        stale_relationships=snap["stale_relationships"]), store, persist=False)
    diff = differences(orig, new)
    versions_match = (snap["config_version"] == CONFIG_VERSION and snap["extractor_versions"] == EXTRACTOR_VERSIONS)
    store.audit("analysis_reproduced", analysis=analysis_id, snapshot=snap["id"], identical=not diff, versions_match=versions_match)
    return {"analysis_id": analysis_id, "evidence_snapshot_id": snap["id"], "identical": not diff, "differences": diff,
            "versions_match": versions_match, "report": new}


def landscape_extracts(svc: RepoService, store: Store, names: Optional[list] = None) -> dict:
    """Extracts of every registered repository at its default branch (incremental: unchanged files are not re-parsed)."""
    out = {}
    for r in svc.list():
        if names and r.name not in names:
            continue
        try:
            ex = _extract(r.name, r.path, svc.resolve(r.name, None), store, ())
        except Exception as e:           # a broken repository must not blank the whole graph
            store.audit("graph_extract_failed", repo=r.name, error=str(e)[:200])
            svc.record_scan(r.name, error=str(e)[:300])
            continue
        out[r.name] = ex
        svc.record_scan(r.name, commit=ex.commit, files_failed=len(ex.files_failed))
    return out


def refresh_landscape(svc: RepoService, store: Store) -> dict:
    """Bring the persisted landscape graph and the relationship history up to date with the repositories' current
    default branches and scan outcomes."""
    ex = landscape_extracts(svc, store)
    repos = svc.list()
    if ex:
        g, gid, built = get_or_build(store, ex)
        moved = store.set_head(LANDSCAPE, gid)
        if moved:
            store.audit("graph_version_advanced", graph=gid, built=built, repos={r: e.commit for r, e in sorted(ex.items())})
    else:
        g, gid, built, moved = Graph(), None, False, False
    rel = reconcile(store, g, ex, repos, gid)
    return {"graph_id": gid, "built": built, "moved": moved, "relationships": rel}


def _norm(d) -> str:
    """Canonical JSON text: database round trips turn tuples into lists, which must not count as drift."""
    return json.dumps(d, sort_keys=True, default=list)


def rescan_landscape(svc: RepoService, store: Store) -> dict:
    """Full-rescan path: re-derive every repository's facts from source, ignoring all caches, compare with what the
    incremental path had produced, repair any drift, and rebuild the graph. Drift means a cache or extractor fault."""
    repos, rescanned = [], {}
    for r in svc.list():
        try:
            commit = svc.resolve(r.name, None)
            cached = store.get_extract(r.name, commit, EXTRACTOR_VERSION)
            incremental = cached or extract_commit(r.name, r.path, commit, store, ())[0]
            full, stats = extract_commit(r.name, r.path, commit, store, (), force=True)
        except Exception as e:
            repos.append({"repo": r.name, "error": str(e)[:200]})
            continue
        drift = _norm(incremental.to_dict()) != _norm(full.to_dict())
        store.put_extract(full)
        rescanned[r.name] = full
        repos.append({"repo": r.name, "commit": commit, "files": stats["files"], "parsed": stats["parsed"], "drift": drift})
    if not rescanned:
        return {"repositories": repos, "graph_id": None, "graph_drift": False}
    old_id = graph_id(graph_inputs(rescanned))
    old = store.get_graph(old_id)
    g, gid, _ = get_or_build(store, rescanned, rebuild=True)
    graph_drift = old is not None and _norm(old) != _norm(g.to_dict())
    moved = store.set_head(LANDSCAPE, gid)
    store.audit("graph_rescanned", graph=gid, drifted=[x["repo"] for x in repos if x.get("drift")], graph_drift=graph_drift,
                head_moved=moved)
    return {"repositories": repos, "graph_id": gid, "graph_drift": graph_drift, "head_moved": moved}


def pr_event_job_key(p: dict) -> str:
    ident = [p["provider"], p["repo"], p["pr"], p["head_sha"], p["base_ref"]]
    return hashlib.sha256(json.dumps([ident, CONFIG_VERSION]).encode()).hexdigest()


def pr_event_group_key(p: dict) -> str:
    return f"pr:{p['repo']}:{p['pr']}"          # same group as direct analyses of this PR: newest wins


def _transient(msg: str) -> bool:
    return "Cannot reach" in msg or "timed out" in msg


def make_handlers(db, allow_local: bool = False, allowed_hosts: Optional[list] = None, audit_key: Optional[bytes] = None,
                  workspace: str = ".tia-data/repos", scm: Optional[ScmConfig] = None):
    """Job handlers for the worker. Each job runs bound to its own tenant."""
    from .jobs import JobQueue
    queue = JobQueue(db)
    scm = scm or ScmConfig()

    def cancelled(job) -> bool:
        state = queue.get(job.tenant_id, job.id)
        return bool(state and state["cancel_requested"])

    def github_pull_request(job):
        """A GitHub pull_request webhook: sync, fetch the PR, analyse against its real base, upsert the PR comment."""
        p = job.payload
        if cancelled(job):
            return {"skipped": "superseded by a newer push"}
        if not scm.github_token:
            raise PermanentError("no GitHub token is configured on the worker (TIA_GITHUB_TOKEN)")
        store = Store(db, job.tenant_id, audit_key)
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        try:
            svc.sync(p["repo"], scm.github_token)
            refresh_landscape(svc, store)
            concrete = resolve(svc, {"target": p["repo"], "pr": p["pr"], "base": p["base_ref"], "token": scm.github_token})
            rep = execute(svc, store, concrete)
        except IngestError as e:
            refresh_landscape(svc, store)          # a failed fetch makes this repository's relationships stale
            if _transient(str(e)):
                raise
            raise PermanentError(str(e))
        if cancelled(job):
            return {"analysis_id": rep["analysis_id"], "skipped_publish": "superseded by a newer push"}
        try:
            res = publish_to_github(rep, target_repo=p["repo"], full_name=p["full_name"], pr=p["pr"], head_sha=p["head_sha"], scm=scm)
        except PublishError as e:
            if e.permanent:
                raise PermanentError(str(e))
            raise
        audit_publication(store, provider="github", full_name=p["full_name"], pr=p["pr"], analysis_id=rep["analysis_id"], result=res)
        return {"analysis_id": rep["analysis_id"], "status": rep["status"], "comment": res["comment"], "check_run": res["check_run"]}

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

    return {"impact_analysis": impact_analysis, "github_pull_request": github_pull_request}
