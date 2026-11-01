"""Publish an analysis to a GitHub pull request: one advisory comment and one advisory check run, both updated in place.

Shared by the webhook worker (a pull request event) and the product endpoint POST /impact-analyses/{id}/publish.
The audience rule is applied here, once: unless the operator opts in to full detail, only the pull request's own repository is
named, so a comment never reveals downstream repositories the author may not be allowed to see. Tokens are never logged, audited
or returned; error text carries only the HTTP status.
"""
from __future__ import annotations

import urllib.error
from typing import Optional

from . import github, metrics
from .report import permalink, redact, render_comment
from .webhooks import ScmConfig


class PublishError(Exception):
    """GitHub refused the comment. permanent=True: retrying cannot help (permissions, unknown PR); False: transient."""

    def __init__(self, message: str, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


def audience_view(report: dict, target_repo: str, scm: ScmConfig, comment_repos: Optional[list] = None) -> dict:
    """What the comment may show. `comment_repos` is the proven-safe set from AccessStore.comment_repos (None when it was not computed)."""
    if scm.comment_audience in ("readers", "all") and comment_repos is not None:
        return redact(report, comment_repos)
    if scm.comment_audience == "all":
        return report
    return redact(report, [target_repo])


def comment_scope(access, svc, scm: ScmConfig, target_repo: str, auth_required: bool) -> Optional[list]:
    """The repositories a PR comment may name, or None when no restriction beyond the audience setting applies."""
    if scm.comment_audience == "readers" or (scm.comment_audience == "all" and auth_required):
        return access.comment_repos(target_repo, svc.list())
    return None


def _skip_reason(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403, 404):
            return (f"GitHub refused the check run (HTTP {e.code}): creating check runs needs a GitHub App installation token "
                    "with checks:write, not a personal access token")
        return f"GitHub rejected the check run (HTTP {e.code})"
    return "the check run could not be published (GitHub unreachable)"


def publish_to_github(report: dict, *, target_repo: str, full_name: str, pr: int, head_sha: str, scm: ScmConfig,
                      comment: bool = True, check_run: Optional[bool] = None, comment_repos: Optional[list] = None) -> dict:
    """Returns {"comment": created|updated|None, "check_run": created|updated|{"skipped": reason}|None, "permalink": url|None}.
    The comment is the contract: if GitHub refuses it a PublishError is raised. The check run is best effort and never fails the
    publication."""
    if not scm.github_token:
        raise PublishError("no GitHub token is configured (TIA_GITHUB_TOKEN)", permanent=True)
    view = audience_view(report, target_repo, scm, comment_repos)
    link = permalink(scm.public_url, report["analysis_id"])
    out: dict = {"comment": None, "check_run": None, "permalink": link}
    if comment:
        try:
            out["comment"] = github.upsert_comment(full_name, pr, render_comment(view, link=link), scm.github_token, scm.github_api_url)
            metrics.PUBLISH.labels(kind="comment", outcome=out["comment"]).inc()
        except urllib.error.HTTPError as e:
            metrics.PUBLISH.labels(kind="comment", outcome="failed").inc()
            if 400 <= e.code < 500 and e.code != 429:
                raise PublishError(f"GitHub rejected the comment (HTTP {e.code}); the token needs pull-request write access",
                                   permanent=True)
            raise PublishError(f"GitHub could not accept the comment (HTTP {e.code})", permanent=False)
        except urllib.error.URLError:
            metrics.PUBLISH.labels(kind="comment", outcome="failed").inc()
            raise PublishError("GitHub is unreachable", permanent=False)
    if scm.check_run if check_run is None else check_run:
        try:
            out["check_run"] = github.upsert_check_run(full_name, head_sha, github.check_run_payload(view, head_sha, link),
                                                       scm.github_token, scm.github_api_url)
            metrics.PUBLISH.labels(kind="check_run", outcome=out["check_run"]).inc()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            out["check_run"] = {"skipped": _skip_reason(e)}
            metrics.PUBLISH.labels(kind="check_run", outcome="skipped").inc()
    return out


def audit_publication(store, *, provider: str, full_name: str, pr: int, analysis_id: str, result: dict) -> None:
    if result.get("comment"):
        store.audit("pr_comment_published", provider=provider, repo=full_name, pr=pr, analysis=analysis_id, action=result["comment"])
    cr = result.get("check_run")
    if isinstance(cr, dict):
        store.audit("check_run_skipped", provider=provider, repo=full_name, pr=pr, analysis=analysis_id, reason=cr["skipped"])
    elif cr:
        store.audit("check_run_published", provider=provider, repo=full_name, pr=pr, analysis=analysis_id, action=cr)
