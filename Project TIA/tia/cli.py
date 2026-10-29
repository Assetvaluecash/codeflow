"""tia command line: analyze"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .impact import AnalysisRequest, analyze
from .report import redact, render_comment, to_json
from .db import Database
from .store import Store


def _repos(items: list[str]) -> dict:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--repo expects NAME=PATH, got '{it}'")
        n, p = it.split("=", 1)
        out[n] = p
    return out


def _emit(report: dict, fmt: str, out_path: str | None, readable):
    rep = redact(report, readable)
    text = to_json(rep) if fmt == "json" else render_comment(rep)
    if fmt == "both":
        text = render_comment(rep) + "\n\n---\n\n```json\n" + to_json(rep) + "\n```"
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tia")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyse a proposed change (base..head) in one repository")
    a.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    a.add_argument("--target", required=True)
    a.add_argument("--base", required=True)
    a.add_argument("--head", required=True)
    a.add_argument("--pr", type=int)
    a.add_argument("--change-record")
    a.add_argument("--db-url", default=os.environ.get("TIA_DATABASE_URL"), help="PostgreSQL DSN (runtime role); optional: without it nothing is persisted")
    a.add_argument("--tenant-id", default=os.environ.get("TIA_TENANT_ID"))
    a.add_argument("--format", choices=["json", "comment", "both"], default="comment")
    a.add_argument("--out")
    a.add_argument("--readable", help="comma-separated repos the audience may see (default: all)")
    a.add_argument("--max-depth", type=int, default=8)
    a.add_argument("--max-results", type=int, default=200)
    a.add_argument("--github-repo", help="OWNER/REPO to post the PR comment to (needs --pr and $GITHUB_TOKEN)")

    args = ap.parse_args(argv)
    if args.cmd == "analyze":
        req = AnalysisRequest(repos=_repos(args.repo), target=args.target, base=args.base, head=args.head,
                              pull_request=args.pr, change_record=args.change_record,
                              max_depth=args.max_depth, max_results=args.max_results)
        store = Store(Database(args.db_url), args.tenant_id) if args.db_url and args.tenant_id else None
        rep = analyze(req, store)
        readable = args.readable.split(",") if args.readable else None
        _emit(rep, args.format, args.out, readable)
        if args.github_repo:
            import os
            from .github import upsert_comment
            tok = os.environ.get("GITHUB_TOKEN")
            if not (tok and args.pr):
                raise SystemExit("--github-repo needs --pr and $GITHUB_TOKEN")
            print(upsert_comment(args.github_repo, args.pr, render_comment(redact(rep, readable)), tok), file=sys.stderr)
        return 0 if rep["status"] != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
