"""UC1-23: deployment artefacts. Docker, Compose and Helm are not available on the development machine, so these are STATIC checks only:
valid YAML, secrets only from files, non-root and read-only containers, probes on endpoints the code really serves, the migration first, and every
setting the artefacts pass is one the code reads."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
CODE = "\n".join(f.read_text(encoding="utf-8") for f in (ROOT / "tia").glob("*.py"))
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
DOCKERFILE = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
CHART = DEPLOY / "helm" / "tia"
TEMPLATES = "\n".join(f.read_text(encoding="utf-8") for f in (CHART / "templates").glob("*"))
SECRETISH = re.compile(r"(password|secret|token|key|dsn)", re.I)


def env_names(text):
    return set(re.findall(r"\b(TIA_[A-Z0-9_]+?)(?:_FILE)?\b", text))


def code_reads(name):
    return name in CODE


# ---- Dockerfile ----------------------------------------------------------------------------------------------------------------------
def test_the_image_runs_as_a_numeric_non_root_user_with_a_health_check():
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert "HEALTHCHECK" in DOCKERFILE and "/healthz" in DOCKERFILE
    assert "TIA_AUTH_REQUIRED=true" in DOCKERFILE                                         # authentication is on unless someone turns it off
    for line in re.findall(r"^FROM (\S+)", DOCKERFILE, re.M):
        assert ":" in line and not line.endswith(":latest"), line                            # a pinned version, not "latest"
    assert not re.search(r"^(ADD|COPY) .*secret", DOCKERFILE, re.M | re.I)


def test_the_image_installs_only_what_the_product_needs():
    assert "git" in DOCKERFILE and "[api]" in DOCKERFILE and "--no-install-recommends" in DOCKERFILE


# ---- Compose -------------------------------------------------------------------------------------------------------------------------
def test_compose_services_and_order():
    s = COMPOSE["services"]
    assert set(s) == {"db", "migrate", "api", "worker"}
    for svc in ("api", "worker"):
        assert s[svc]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert s["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert s["api"]["ports"] == ["127.0.0.1:8080:8080"]                                   # never exposed on all interfaces without a proxy


def test_compose_hardening():
    for svc in ("api", "worker"):
        x = COMPOSE["services"][svc]
        assert x["read_only"] is True and x["cap_drop"] == ["ALL"] and "no-new-privileges:true" in x["security_opt"]


def test_compose_secrets_come_only_from_files():
    declared = set(COMPOSE["secrets"])
    for name, svc in COMPOSE["services"].items():
        for k, v in (svc.get("environment") or {}).items():
            if SECRETISH.search(k) and k != "TIA_AUTH_REQUIRED":
                assert k.endswith("_FILE") and str(v).startswith("/run/secrets/"), (name, k)
                assert str(v).rsplit("/", 1)[-1] in svc.get("secrets", []), (name, k)
        assert set(svc.get("secrets", [])) <= declared
    assert all(set(v) == {"file"} for v in COMPOSE["secrets"].values())


def test_every_setting_compose_passes_is_one_the_code_reads():
    for name, svc in COMPOSE["services"].items():
        for k in (svc.get("environment") or {}):
            if k.startswith("TIA_"):
                base = k[:-5] if k.endswith("_FILE") else k
                assert code_reads(base), (name, k)


# ---- Helm ----------------------------------------------------------------------------------------------------------------------------
def test_chart_metadata_and_values_parse():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert chart["apiVersion"] == "v2" and chart["name"] == "tia"
    assert values["existingSecret"] and values["api"]["replicas"] >= 2 and values["networkPolicy"]["enabled"] is True
    assert not any(SECRETISH.search(k) and isinstance(v, str) and v and k not in ("existingSecret", "tlsSecret") for k, v in _walk(values))


def _walk(d, prefix=""):
    for k, v in (d.items() if isinstance(d, dict) else []):
        yield k, v
        yield from _walk(v, k)


def test_pods_are_hardened():
    for key in ("runAsNonRoot: true", "runAsUser: 10001", "readOnlyRootFilesystem: true", "allowPrivilegeEscalation: false", "capabilities: {drop: [ALL]}",
                "seccompProfile: {type: RuntimeDefault}", "automountServiceAccountToken: false"):
        assert key in TEMPLATES, key


def test_probes_use_the_endpoints_the_code_serves():
    assert "path: /healthz" in TEMPLATES and "path: /readyz" in TEMPLATES
    api = (ROOT / "tia" / "api.py").read_text(encoding="utf-8")
    health = (ROOT / "tia" / "health.py").read_text(encoding="utf-8")
    assert '"/healthz"' in api and '"/readyz"' in api and "/healthz" in health


def test_the_migration_runs_before_install_and_upgrade_and_the_workspace_survives_uninstall():
    assert '"helm.sh/hook": pre-install,pre-upgrade' in TEMPLATES and 'args: ["tia.migrate"]' in TEMPLATES
    assert '"helm.sh/resource-policy": keep' in TEMPLATES


def test_required_values_fail_the_install_instead_of_starting_misconfigured():
    assert 'required "publicUrl is required"' in TEMPLATES and 'required "defaultTenantId is required"' in TEMPLATES


def test_every_setting_the_chart_passes_is_one_the_code_reads():
    for name in env_names(TEMPLATES):
        assert code_reads(name), name


def test_the_chart_reads_secrets_only_from_the_mounted_secret():
    for line in re.findall(r"name: (TIA_[A-Z_]+), value: ([^}]+)}", TEMPLATES):
        k, v = line
        if SECRETISH.search(k) and k not in ("TIA_AUTH_REQUIRED",):
            assert k.endswith("_FILE") and v.strip().startswith("/run/secrets/tia/"), line
    assert "defaultMode: 0400" in TEMPLATES


def test_the_deploy_readme_says_this_was_never_run():
    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "written and statically checked only" in text and "never" in text
