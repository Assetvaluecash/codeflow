"""Support script (not production code): build the demo landscape and analyse one PR branch.

    python tools/demo.py [--branch breaking|impl|tests_only|docs_only|syntax_error|removed_endpoint]
                         [--format comment|json|both] [--dir PATH]
"""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fixtures.build_fixture import build  # noqa: E402
from tia.cli import _emit  # noqa: E402
from tia.impact import AnalysisRequest, analyze  # noqa: E402
from tia.store import Store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--branch", default="breaking",
                    choices=["breaking", "impl", "tests_only", "docs_only", "syntax_error", "removed_endpoint"])
    ap.add_argument("--format", choices=["json", "comment", "both"], default="comment")
    a = ap.parse_args()
    dest = a.dir or tempfile.mkdtemp(prefix="tia-demo-")
    fx = build(dest)
    rep = analyze(AnalysisRequest(repos=fx["paths"], target="payments-service", base=fx["shas"]["base"],
                                  head=fx["shas"][a.branch], pull_request=1842), Store(str(Path(dest) / ".tia")))
    _emit(rep, a.format, None, None)
    print(f"\n(fixture in {dest})", file=sys.stderr)


if __name__ == "__main__":
    main()
