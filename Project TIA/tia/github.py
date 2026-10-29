"""Publish the advisory PR comment (PRD FR-035 P0). Updates a single comment in place.

NOTE: not exercised against a live GitHub in this repository's tests (no credentials in the
build environment); only the pure helper `find_marker_comment` is unit-tested.
Requires a token with pull-request write permission only; never reads more than comments.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

from .report import MARKER


def find_marker_comment(comments: list[dict]) -> Optional[dict]:
    return next((c for c in comments if MARKER in (c.get("body") or "")), None)


def _call(method: str, url: str, token: str, payload: Optional[dict] = None):
    req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "tia"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"null")


def upsert_comment(full_repo: str, pr_number: int, body: str, token: str,
                   api_url: str = "https://api.github.com") -> str:
    base = f"{api_url}/repos/{full_repo}"
    existing = find_marker_comment(_call("GET", f"{base}/issues/{pr_number}/comments?per_page=100", token) or [])
    if existing:
        _call("PATCH", f"{base}/issues/comments/{existing['id']}", token, {"body": body})
        return "updated"
    _call("POST", f"{base}/issues/{pr_number}/comments", token, {"body": body})
    return "created"
