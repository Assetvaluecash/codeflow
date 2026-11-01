"""Publish the advisory PR comment (PRD FR-035 P0) and a check run. Updates a single comment / check run in place.

The comment upsert is verified live against github.com and, in tests, against a local fake. Creating a check run needs a GitHub
App installation token: GitHub refuses it for personal access tokens, so the publisher reports that as skipped (the comment is
still posted). The check run is advisory by construction: its conclusion is always ``neutral`` and it never blocks a merge.
Requires a token with pull-request write (comment) and checks write (App) permission only.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from . import metrics, tracing
from .report import MARKER, render_comment

CHECK_NAME = "TIA impact analysis"
MAX_ANNOTATIONS = 50                      # GitHub accepts at most 50 per request
MAX_TEXT = 60000                          # the check run text limit is 65535 characters


def find_marker_comment(comments: list[dict]) -> Optional[dict]:
    return next((c for c in comments if MARKER in (c.get("body") or "")), None)


def _operation(url: str) -> str:
    return "check_run" if "check-runs" in url else "comment" if "/issues/" in url else "api"


def _call(method: str, url: str, token: str, payload: Optional[dict] = None):
    op, t0, outcome = _operation(url), time.perf_counter(), "success"
    try:
        with tracing.span(f"github.{op}", "client", method=method) as sp:
            req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload else None,
                                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                                  "Content-Type": "application/json", "User-Agent": "tia",
                                                  **tracing.propagation_headers()})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                outcome = f"http_{e.code // 100}xx"
                sp.set(status=e.code)
                raise
            except OSError:
                outcome = "unreachable"
                raise
    finally:
        metrics.CONNECTOR.labels(provider="github", operation=op, outcome=outcome).inc()
        metrics.CONNECTOR_DURATION.labels(provider="github", operation=op).observe(time.perf_counter() - t0)


ROLE_ORDER = ("pull", "triage", "push", "maintain", "admin")
_ROLE_NAMES = {"read": "pull", "write": "push"}


def list_collaborators(full_repo: str, token: str, api_url: str = "https://api.github.com") -> list[tuple[str, str]]:
    """(login, permission) of everyone with access to the repository, teams and organisation members included (affiliation=all).
    GitHub only answers this for a token with push access to the repository."""
    out, page = [], 1
    while True:
        batch = _call("GET", f"{api_url}/repos/{full_repo}/collaborators?affiliation=all&per_page=100&page={page}", token) or []
        for c in batch:
            login = (c.get("login") or "").lower()
            perms = c.get("permissions") or {}
            role = _ROLE_NAMES.get(c.get("role_name"), c.get("role_name"))
            if role not in ROLE_ORDER:
                role = next((r for r in reversed(ROLE_ORDER) if perms.get(r)), None)
            if login and role:
                out.append((login, role))
        if len(batch) < 100:
            return out
        page += 1


def upsert_comment(full_repo: str, pr_number: int, body: str, token: str,
                   api_url: str = "https://api.github.com") -> str:
    base = f"{api_url}/repos/{full_repo}"
    existing, page = None, 1
    while existing is None:
        batch = _call("GET", f"{base}/issues/{pr_number}/comments?per_page=100&page={page}", token) or []
        existing = find_marker_comment(batch)
        if len(batch) < 100:
            break
        page += 1
    if existing:
        _call("PATCH", f"{base}/issues/comments/{existing['id']}", token, {"body": body})
        return "updated"
    _call("POST", f"{base}/issues/{pr_number}/comments", token, {"body": body})
    return "created"


# ---- check run -----------------------------------------------------------------------------------------------------
def _title(report: dict) -> str:
    if report["status"] == "failed":
        return "Analysis failed - no impact conclusions"
    s = report["summary"]
    if not s["findings"] and not report.get("redacted", {}).get("findings"):
        return "No downstream impact identified in the analysed scope (not proof of none)"
    sev = s["by_severity"]
    parts = ", ".join(f"{sev[k]} {k}" for k in ("critical", "high", "medium", "low", "unknown") if sev.get(k))
    held = report.get("redacted", {}).get("findings", 0)
    return f"{s['findings']} finding(s), {s['material_findings']} material" + (f" ({parts})" if parts else "") + \
        (f"; {held} withheld by visibility policy" if held else "")


def _summary(report: dict, details_url: Optional[str]) -> str:
    ch = report["change"]
    lines = [f"`{ch['repository']}` change type: **{ch.get('change_type', 'unknown')}** - analysis status `{report['status']}`."]
    if report["status"] not in ("complete", "failed"):
        lines.append("> This is not a clean impact assessment: see the unknowns that affect confidence.")
    for f in [x for x in report.get("findings", []) if x["material"]][:5]:
        lines.append(f"- **{f['severity']}** {f['component']}: {f['reason'].split('. Change:')[0]}")
    unk = [u for u in report.get("unknowns", []) if u.get("affects_confidence")]
    if unk:
        lines.append(f"{len(unk)} unknown(s) affect confidence: " + ", ".join(sorted({u['area'] for u in unk})) + ".")
    if details_url:
        lines.append(f"[Full report]({details_url})")
    lines.append("Advisory only - this check never blocks a merge.")
    return "\n".join(lines)


def check_run_annotations(report: dict) -> list:
    """File/line annotations for the contract and schema changes found in the pull request's own repository."""
    out = []
    for c in report.get("change", {}).get("changed_components", []):
        if not c.get("file") or not c.get("line") or not (c["type"].startswith("api") or c["type"].startswith("db_")):
            continue
        out.append({"path": c["file"], "start_line": int(c["line"]), "end_line": int(c["line"]),
                    "annotation_level": "warning" if c.get("breaking") else "notice",
                    "title": f"TIA: {c['change'].replace('_', ' ')}", "message": c.get("detail") or c["name"]})
    return out[:MAX_ANNOTATIONS]


def check_run_payload(report: dict, head_sha: str, details_url: Optional[str] = None) -> dict:
    """The check run for one analysis. Always completed and ``neutral``: TIA is advisory and must never block a merge."""
    text = render_comment(report, max_rows=50)
    payload = {"name": CHECK_NAME, "head_sha": head_sha, "status": "completed", "conclusion": "neutral",
               "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
               "external_id": report["analysis_id"],
               "output": {"title": _title(report)[:255], "summary": _summary(report, details_url)[:MAX_TEXT],
                          "text": text[:MAX_TEXT]}}
    if details_url:
        payload["details_url"] = details_url
    notes = check_run_annotations(report)
    if notes:
        payload["output"]["annotations"] = notes
    return payload


def upsert_check_run(full_repo: str, head_sha: str, payload: dict, token: str, api_url: str = "https://api.github.com") -> str:
    """Create the check run for this commit, or update the one TIA already created for it."""
    base = f"{api_url}/repos/{full_repo}"
    q = urllib.parse.urlencode({"check_name": CHECK_NAME, "per_page": 100})
    runs = (_call("GET", f"{base}/commits/{head_sha}/check-runs?{q}", token) or {}).get("check_runs", [])
    existing = next((r for r in runs if r.get("name") == CHECK_NAME), None)
    if existing:
        _call("PATCH", f"{base}/check-runs/{existing['id']}", token, {k: v for k, v in payload.items() if k != "head_sha"})
        return "updated"
    _call("POST", f"{base}/check-runs", token, payload)
    return "created"
