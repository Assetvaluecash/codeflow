"""UC1-29: the CI gates themselves. A gate that cannot fail proves nothing, so each one is shown to catch the thing it exists for."""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import ci  # noqa: E402

GOOD = textwrap.dedent("""
    name: x
    on:
      pull_request:
    permissions:
      contents: read
    jobs:
      t:
        runs-on: ubuntu-latest
        timeout-minutes: 10
        steps:
          - uses: actions/checkout@v4
          - run: echo hello
""")


def workflow(tmp_path, monkeypatch, text):
    f = tmp_path / ".github" / "workflows" / "tia-ci.yml"
    f.parent.mkdir(parents=True)
    f.write_text(text)
    monkeypatch.setattr(ci, "WORKFLOW", f)
    return ci.step_workflow()


def test_the_real_workflow_passes_every_gate():
    assert ci.step_workflow() == []
    assert ci.step_migrations() == []
    assert ci.step_secrets() == []


def test_a_good_workflow_passes(tmp_path, monkeypatch):
    assert workflow(tmp_path, monkeypatch, GOOD) == []


@pytest.mark.parametrize("change,fragment", [
    (lambda t: t.replace("on:\n  pull_request:", "on:\n  pull_request_target:"), "pull_request_target"),
    (lambda t: t.replace("permissions:\n  contents: read", "permissions:\n  contents: write"), "read-only"),
    (lambda t: t.replace("permissions:\n  contents: read\n", ""), "permissions"),
    (lambda t: t.replace("    timeout-minutes: 10\n", ""), "timeout"),
    (lambda t: t.replace("echo hello", "echo ${{ github.event.pull_request.title }}"), "script injection"),
    (lambda t: t.replace("echo hello", "echo ${{ github.head_ref }}"), "script injection"),
    (lambda t: t.replace("actions/checkout@v4", "actions/checkout"), "not versioned"),
    (lambda t: t.replace("echo hello", "echo ${{ secrets.TOKEN }}"), "secrets"),
])
def test_unsafe_workflows_are_caught(tmp_path, monkeypatch, change, fragment):
    problems = workflow(tmp_path, monkeypatch, change(GOOD))
    assert any(fragment in p for p in problems), problems


def test_a_missing_workflow_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "WORKFLOW", tmp_path / "nope.yml")
    assert ci.step_workflow()


def test_the_secret_scan_finds_credentials_and_honours_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "REPO", tmp_path)
    (tmp_path / "a.py").write_text('TOKEN = "ghp_' + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + '"\n')
    (tmp_path / "b.md").write_text("-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n")
    (tmp_path / "c.py").write_text('K = "AKIA' + "ABCDEFGHIJKLMNOP" + '"\n')
    (tmp_path / "ok.py").write_text('F = "xoxb-1234567890-abcdefghij"  # tia-ci: allow-secret (fake)\nA = "AKIAIOSFODNN7EXAMPLE"\n')
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "x.py").write_text('T = "ghp_' + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + '"\n')       # dependencies are not ours to scan
    found = ci.step_secrets()
    names = sorted(p.split(":")[0] for p in found)
    assert names == ["a.py", "b.md", "c.py"], found
    assert any("GitHub token" in p for p in found) and any("private key" in p for p in found) and any("AWS" in p for p in found)


def test_the_migration_gate_catches_gaps_duplicates_and_bad_names(tmp_path, monkeypatch):
    (tmp_path / "tia" / "migrations").mkdir(parents=True)
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    for n in ("0001_a.sql", "0002_b.sql"):
        (tmp_path / "tia" / "migrations" / n).write_text("select 1;")
    assert not [p for p in ci.step_migrations() if "numbered" in p or "named" in p]
    (tmp_path / "tia" / "migrations" / "0004_d.sql").write_text("select 1;")
    assert any("numbered" in p for p in ci.step_migrations())
    (tmp_path / "tia" / "migrations" / "0003_c.sql").write_text("select 1;")
    (tmp_path / "tia" / "migrations" / "3_bad.sql").write_text("select 1;")
    assert any("named" in p for p in ci.step_migrations())


def test_the_migration_gate_catches_a_tenant_table_the_runtime_does_not_know(tmp_path, monkeypatch):
    (tmp_path / "tia" / "migrations").mkdir(parents=True)
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    (tmp_path / "tia" / "migrations" / "0001_a.sql").write_text(
        "create table brand_new_things (\n    tenant_id uuid not null,\n    id int\n);\n")
    problems = ci.step_migrations()
    assert any("brand_new_things" in p and "RUNTIME_TABLES" in p for p in problems), problems


def test_every_step_has_a_name_the_workflow_can_call():
    import yaml
    doc = yaml.safe_load(ci.WORKFLOW.read_text(encoding="utf-8"))
    called = set()
    for job in doc["jobs"].values():
        for st in job["steps"]:
            if "tools/ci.py" in st.get("run", ""):
                called.add(st["run"].split("--only")[1].strip())
    assert called <= set(ci.STEPS) and {"package", "migrations", "secrets"} <= called
