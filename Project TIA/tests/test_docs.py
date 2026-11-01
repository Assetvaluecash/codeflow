"""UC1-28: the documentation cannot drift from the code. The API reference is generated; the guides are checked against the code they describe."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import gen_docs  # noqa: E402
from tia import rbac  # noqa: E402
from tia.identity import parse_idp  # noqa: E402
from tia.tenantconfig import parse as parse_config  # noqa: E402

DOCS = ROOT / "docs"
ADMIN = (DOCS / "ADMIN-GUIDE.md").read_text(encoding="utf-8")
USER = (DOCS / "USER-GUIDE.md").read_text(encoding="utf-8")
GUIDES = {"ADMIN-GUIDE.md": ADMIN, "USER-GUIDE.md": USER}


def test_the_api_reference_is_current():
    assert (DOCS / "API.md").read_text(encoding="utf-8") == gen_docs.render(), "docs/API.md is out of date: run  python tools/gen_docs.py"


def test_every_route_is_in_the_api_reference_with_its_permission():
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    for (method, path), perm in rbac.ROUTES.items():
        assert f"### `{method} {path}`" in text, (method, path)


def test_every_environment_variable_the_code_reads_is_documented():
    names = set()
    for f in (ROOT / "tia").glob("*.py"):
        names |= set(re.findall(r"\b(TIA_[A-Z0-9_]+|VAULT_[A-Z_]+)\b(?!\.)", f.read_text(encoding="utf-8")))   # not a file name such as TIA_KF_V2_INTEGRATION.docx
    names = {n for n in names if not n.endswith("_")}                                           # prefixes such as TIA_TLS_
    missing = sorted(n for n in names if n not in ADMIN)
    assert not missing, f"undocumented in docs/ADMIN-GUIDE.md: {missing}"


def test_every_documented_environment_variable_exists():
    code = "\n".join(f.read_text(encoding="utf-8") for f in (ROOT / "tia").glob("*.py"))
    documented = set(re.findall(r"`((?:TIA|VAULT)_[A-Z0-9_]+)`", ADMIN))
    gone = sorted(n for n in documented if n not in code)
    assert not gone, f"documented but not in the code: {gone}"


def subcommands(module):
    out = subprocess.run([sys.executable, "-m", module, "--help"], capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT), "PATH": __import__("os").environ.get("PATH", ""), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}).stdout
    m = re.search(r"\{([a-z0-9,-]+)\}", out)
    return set(m.group(1).split(",")) if m else set()


def test_every_command_the_guide_shows_exists_and_every_tenant_command_is_documented():
    real = subcommands("tia.tenants")
    assert {"create", "idp-set", "token-create", "map-installation", "offboard"} <= real
    shown = set(re.findall(r"python -m tia\.tenants ([a-z-]+)", ADMIN))
    assert shown <= real, f"commands in the guide that do not exist: {sorted(shown - real)}"
    undocumented = sorted(c for c in real if f"tia.tenants {c}" not in ADMIN and f"`{c}`" not in ADMIN)
    assert not undocumented, f"tenant commands missing from the guide: {undocumented}"


def test_modules_the_guide_runs_exist():
    for mod in set(re.findall(r"python -m (tia\.[a-z_]+)", ADMIN + USER)):
        assert (ROOT / "tia" / (mod.split(".", 1)[1] + ".py")).is_file(), mod


def examples(text):
    return {m.group(1): json.loads(m.group(2)) for m in re.finditer(r"<!-- check: ([a-z-]+) -->\s*```json\n(.*?)\n```", text, re.S)}


def test_the_configuration_examples_are_accepted_by_the_real_validators():
    ex = examples(ADMIN)
    assert set(ex) == {"idp-oidc", "idp-saml", "tenant-config"}
    assert parse_idp("oidc", ex["idp-oidc"]["config"])["role_map"]["tia-admins"] == "administrator"
    assert parse_idp("saml", ex["idp-saml"]["config"])["want_assertions_signed"] is True
    cfg = parse_config(ex["tenant-config"])
    assert cfg.max_depth == 6 and cfg.strip_prefixes == ("/api/gateway",) and cfg.comment_audience == "target"


def test_the_role_table_matches_the_permission_model():
    for role in rbac.ROLES:
        assert f"`{role}`" in ADMIN
    # the guide says release managers can change configuration and engineers cannot; auditors read the audit log and cannot analyse
    assert "release_manager" in rbac.PERMISSIONS["config_write"] and "engineer" not in rbac.PERMISSIONS["config_write"]
    assert "auditor" in rbac.PERMISSIONS["audit_read"] and "auditor" not in rbac.PERMISSIONS["analyze"]
    assert rbac.PERMISSIONS["admin"] == {"administrator"}


def norm(p):
    return "/" + "/".join("{}" if s.startswith("{") else s for s in p.strip("/").split("/"))


def test_every_endpoint_the_guides_mention_exists():
    known = {(m, norm(p)) for m, p in rbac.ROUTES}
    known_paths = {p for _, p in known}
    for name, text in GUIDES.items():
        for m in re.finditer(r"\b(GET|POST|PUT|DELETE)\s+`?(/[A-Za-z0-9/_{}.-]+)", text):
            assert (m.group(1), norm(m.group(2))) in known, f"{name}: {m.group(0)} is not an API route"
        for m in re.finditer(r"`(/(?:api/v1|auth)/[A-Za-z0-9/_{}.-]+)`", text):
            p = norm(m.group(1))
            assert any(k == p or k.startswith(p + "/") for k in known_paths), f"{name}: {m.group(1)} is not an API route"


def slug(heading):
    s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", s)


def test_relative_links_and_anchors_resolve():
    for name, text in {**GUIDES, "API.md": (DOCS / "API.md").read_text(encoding="utf-8")}.items():
        for m in re.finditer(r"\]\((?!https?:|#)([^)#\s]+)(?:#([^)\s]+))?\)", text):
            target = (DOCS / m.group(1)).resolve()
            assert target.is_file(), f"{name}: link to {m.group(1)} is broken"
            if m.group(2):
                heads = [slug(h) for h in re.findall(r"^#{1,6}\s+(.*)$", target.read_text(encoding="utf-8"), re.M)]
                assert m.group(2) in heads, f"{name}: anchor #{m.group(2)} not found in {m.group(1)}"


def test_the_guides_state_what_is_not_verified_or_not_built():
    assert "No real GitHub App, identity provider" in ADMIN and "What TIA does not do" in ADMIN
    assert "Azure DevOps" in ADMIN and "rate limiting" in ADMIN.lower()
    assert "advisory" in USER and "Read the unknowns" in USER
