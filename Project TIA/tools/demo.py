"""Support script (not production code): build a demo landscape and analyse one PR branch.

    python tools/demo.py [--landscape payments|lanes] [--branch BRANCH] [--format comment|json|both] [--dir PATH]

payments: the 6-repository landscape (branches breaking, impl, tests_only, docs_only, syntax_error, removed_endpoint)
lanes:    the UC-1 test-scope landscape, one shared endpoint and two consumers, one of which applies a rule the other does not
          (branches behaviour, move_rule, signature, signature_unfixed, docstring, contract); see docs/CASE-STUDY.md
"""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fixtures import build_fixture, build_lanes_fixture  # noqa: E402
from tia.cli import _emit  # noqa: E402
from tia.impact import AnalysisRequest, analyze  # noqa: E402

LANDSCAPES = {"payments": (build_fixture, "breaking"), "lanes": (build_lanes_fixture, "behaviour")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--landscape", choices=sorted(LANDSCAPES), default="payments")
    ap.add_argument("--branch")
    ap.add_argument("--format", choices=["json", "comment", "both"], default="comment")
    a = ap.parse_args()
    module, default_branch = LANDSCAPES[a.landscape]
    dest = a.dir or tempfile.mkdtemp(prefix="tia-demo-")
    fx = module.build(dest)
    branch = a.branch or default_branch
    if branch not in fx["shas"] or branch == "base":
        raise SystemExit(f"unknown branch '{branch}' for {a.landscape}: one of {sorted(k for k in fx['shas'] if k != 'base')}")
    rep = analyze(AnalysisRequest(repos=fx["paths"], target="payments-service", base=fx["shas"]["base"],
                                  head=fx["shas"][branch], pull_request=1842))
    _emit(rep, a.format, None, None)
    print(f"\n(fixture in {dest})", file=sys.stderr)


if __name__ == "__main__":
    main()
