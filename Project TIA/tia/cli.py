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

    s = sub.add_parser("shared-components", help="endpoints and functions many entry points depend on, and rule gaps between the "
                                                 "consumers of a shared endpoint (no change needed)")
    s.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    s.add_argument("--min-entry-points", type=int, default=2)
    s.add_argument("--limit", type=int, default=50)

    au = sub.add_parser("automation", help="automation inventory: every endpoint's inputs, rules, valid/invalid values and test matrix; "
                                           "with --ui every page's elements, locators, mandatory flags and dropdown values")
    au.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable")
    au.add_argument("--ui", action="store_true", help="pages and UI elements instead of APIs")

    cv = sub.add_parser("coverage", help="technical coverage: the aspects each entry point and page implies and the tests that address them")
    cv.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable")
    cv.add_argument("--test-case", metavar="FILE", help="assess one test case (JSON or YAML with title/steps/expected/entry_point) instead")

    args = ap.parse_args(argv)
    if args.cmd in ("automation", "coverage"):
        from .automation import api_inventory, summarise_api, ui_inventory
        from .coverage import assess_test_case, coverage_report
        from .graph import build_graph
        from .impact import _extract
        from .scrub import scrub_json
        from .testscope import labels_of
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        if args.cmd == "automation":
            items = api_inventory(ex, labels_of(ex))
            out = {"pages": ui_inventory(ex)} if args.ui else {"endpoints": items, "summary": summarise_api(items)}
        elif args.test_case:
            import yaml
            tc = yaml.safe_load(Path(args.test_case).read_text(encoding="utf-8"))
            if not isinstance(tc, dict):
                raise SystemExit("--test-case expects one test case (an object with title/steps/expected/entry_point)")
            out = assess_test_case(ex, build_graph(ex), tc)
        else:
            out = coverage_report(ex, build_graph(ex))
        sys.stdout.buffer.write((to_json(scrub_json(out)[0]) + "\n").encode("utf-8"))
        return 0
    if args.cmd == "shared-components":
        from .graph import build_graph
        from .impact import _extract
        from .testscope import labels_of, rule_gaps, shared_components
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        g = build_graph(ex)
        out = {"shared_components": shared_components(g, ex, max(args.min_entry_points, 2), args.limit),
               "rule_gaps": rule_gaps(g, ex, labels=labels_of(ex))}
        sys.stdout.buffer.write((to_json(out) + "\n").encode("utf-8"))
        return 0
    if args.cmd == "analyze":
        req = AnalysisRequest(repos=_repos(args.repo), target=args.target, base=args.base, head=args.head,
                              pull_request=args.pr, change_record=args.change_record,
                              max_depth=args.max_depth, max_results=args.max_results)
        store = Store(Database(args.db_url), args.tenant_id) if args.db_url and args.tenant_id else None
        rep = analyze(req, store)
        readable = args.readable.split(",") if args.readable else None
        _emit(rep, args.format, args.out, readable)
        if args.github_repo:
            from .github import upsert_comment
            tok = os.environ.get("GITHUB_TOKEN")
            if not (tok and args.pr):
                raise SystemExit("--github-repo needs --pr and $GITHUB_TOKEN")
            print(upsert_comment(args.github_repo, args.pr, render_comment(redact(rep, readable)), tok), file=sys.stderr)
        return 0 if rep["status"] != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
