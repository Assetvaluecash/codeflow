"""The CI pipeline of TIA itself (UC1-29), runnable locally exactly as the GitHub Actions workflow runs it.

    python tools/ci.py                 # everything: workflow, migrations, secrets, package, tests
    python tools/ci.py --only secrets  # one step (the workflow calls them separately)

Steps
  workflow    the workflow file is well-formed and safe: least-privilege permissions, timeouts, no pull_request_target, no
              untrusted github.event text interpolated into a shell script
  migrations  migration files are numbered 0001.. without gaps or duplicates and every tenant table is registered with the runtime
  secrets     no credential-shaped string in the working tree (tests build theirs at run time)
  package     the built wheel contains every migration and the web UI
  tests       the whole test suite
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
WORKFLOW = REPO / ".github" / "workflows" / "tia-ci.yml"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tia-data", ".pytest_cache", "vendor", "build", "dist"}
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "bearer literal": re.compile(r"[Bb]earer\s+(?:ey[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})"),
}
PUBLIC_EXAMPLES = {"AKIAIOSFODNN7EXAMPLE"}          # the example key from AWS's own documentation
ALLOW_MARKER = "tia-ci: allow-secret"              # a test that needs a credential-SHAPED constant says so on that line
TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".json", ".toml", ".txt", ".sql", ".html", ".js", ".cfg", ".ini", ".env", ".sh", ".ps1", ""}


def step_workflow() -> list[str]:
    import yaml
    problems: list[str] = []
    if not WORKFLOW.is_file():
        return [f"{WORKFLOW} is missing"]
    text = WORKFLOW.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    on = doc.get(True, doc.get("on"))                                  # YAML 1.1 reads the key `on` as True
    if "pull_request_target" in (on if isinstance(on, dict) else {}):
        problems.append("pull_request_target runs untrusted code with secrets: not allowed")
    perms = doc.get("permissions")
    if not isinstance(perms, dict) or any(v == "write" for v in perms.values()):
        problems.append("top-level permissions must be set and read-only")
    for name, job in (doc.get("jobs") or {}).items():
        if "timeout-minutes" not in job:
            problems.append(f"job '{name}' has no timeout-minutes")
        for i, st in enumerate(job.get("steps") or []):
            run = st.get("run", "")
            if re.search(r"\$\{\{\s*github\.(event|head_ref)", run):
                problems.append(f"job '{name}' step {i + 1} interpolates github.event/head_ref into a shell script (script injection)")
            uses = st.get("uses", "")
            if uses and "@" not in uses:
                problems.append(f"job '{name}' step {i + 1}: '{uses}' is not versioned")
    if "secrets." in text:
        problems.append("the workflow reads repository secrets; CI must not need any")
    return problems


def step_migrations() -> list[str]:
    problems: list[str] = []
    files = sorted((ROOT / "tia" / "migrations").glob("*.sql"))
    nums = []
    for f in files:
        m = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", f.name)
        if not m:
            problems.append(f"migration '{f.name}' is not named NNNN_snake_case.sql")
            continue
        nums.append(int(m.group(1)))
    if nums != list(range(1, len(nums) + 1)):
        problems.append(f"migrations must be numbered 1..N without gaps or duplicates, found {nums}")
    sys.path.insert(0, str(ROOT))
    from tia.db import RUNTIME_TABLES
    created = set()
    for f in files:
        created |= set(re.findall(r"create table (?:if not exists )?([a-z_]+)\s*\(", f.read_text(encoding="utf-8")))
    sql = "\n".join(f.read_text(encoding="utf-8") for f in files)
    tenant_owned = {t for t in created if re.search(rf"create table (?:if not exists )?{t}\s*\((?:[^;]*?)\btenant_id\b", sql, re.S)}
    missing = sorted(tenant_owned - set(RUNTIME_TABLES) - {"tenant_installations"})
    if missing:
        problems.append(f"tables with a tenant_id column that RUNTIME_TABLES (tia/db.py) does not list: {missing}")
    unforced = [t for t in sorted(tenant_owned - {"tenant_installations"}) if f"'{t}'" not in sql and f"on {t} " not in sql and f"table {t} enable" not in sql]
    if unforced:
        problems.append(f"tenant tables with no row-level security statement: {unforced}")
    return problems


def step_secrets() -> list[str]:
    problems: list[str] = []
    for path in REPO.rglob("*"):
        if not path.is_file() or any(p in SKIP_DIRS for p in path.relative_to(REPO).parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.split("\n")
        for label, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                if m.group(0) in PUBLIC_EXAMPLES or ALLOW_MARKER in lines[line - 1]:
                    continue
                problems.append(f"{path.relative_to(REPO)}:{line}: looks like a {label}")
    return problems


def step_package() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", out, str(ROOT)], capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            return ["building the wheel failed: " + (r.stderr or r.stdout)[-400:]]
        wheel = next(Path(out).glob("*.whl"))
        names = zipfile.ZipFile(wheel).namelist()
    for f in (ROOT / "tia" / "migrations").glob("*.sql"):
        if f"tia/migrations/{f.name}" not in names:
            problems.append(f"the wheel lacks migration {f.name}")
    for need in ("tia/web/index.html",):
        if need not in names:
            problems.append(f"the wheel lacks {need}")
    if not any(n.startswith("tia/web/vendor/") for n in names):
        problems.append("the wheel lacks the vendored web libraries")
    return problems


def step_tests() -> list[str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], cwd=ROOT)
    return [] if r.returncode == 0 else [f"pytest exited with {r.returncode}"]


STEPS = {"workflow": step_workflow, "migrations": step_migrations, "secrets": step_secrets, "package": step_package, "tests": step_tests}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=sorted(STEPS), action="append", help="run just this step (repeatable)")
    ap.add_argument("--skip", choices=sorted(STEPS), action="append", default=[], help="skip a step")
    a = ap.parse_args(argv)
    failed = 0
    for name in (a.only or [s for s in STEPS if s not in a.skip]):
        print(f"== {name}", flush=True)
        problems = STEPS[name]()
        for p in problems:
            print(f"  FAIL {p}")
        print(f"   {'ok' if not problems else str(len(problems)) + ' problem(s)'}")
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
