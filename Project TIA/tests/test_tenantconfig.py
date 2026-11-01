"""UC1-24: per-tenant configuration - limits, gateway prefixes, severity policy, criticality, materiality, comment audience - versioned,
validated strictly, deterministic, and transparent. Real PostgreSQL, real git."""
import json
import threading
import uuid
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import make_app
from fixtures.build_fixture import _commit, _run, _write, build
from test_webhooks import TOKEN, env, gh, origins, set_pr  # noqa: F401  (fixtures)
from tia import CONFIG_VERSION
from tia.jobs import JobQueue
from tia.severity import apply_override, compute, compute_data, is_material
from tia.store import Store
from tia.tenantconfig import (DEFAULT, ConfigError, TenantConfig, diff, get_version, latest, list_versions, parse, put_version)
from tia.tenants import TenantError, Tenants, main as tenants_cli


def bad(raw):
    with pytest.raises(ConfigError) as e:
        parse(raw)
    return e.value.problems


# =====================================================================================================================
# validation
# =====================================================================================================================
def test_the_empty_configuration_is_the_defaults_and_round_trips():
    assert parse({}) == DEFAULT and DEFAULT.is_default and DEFAULT.id == "default" and DEFAULT.ident(CONFIG_VERSION) == CONFIG_VERSION
    assert (DEFAULT.max_depth, DEFAULT.max_results, DEFAULT.time_budget_seconds, DEFAULT.max_concurrent_jobs) == (8, 200, 60, 4)
    full = DEFAULT.to_dict()
    assert parse(full) == DEFAULT and parse(parse(full).to_dict()) == DEFAULT                      # normalised form is a fixed point
    assert set(full) == {"limits", "paths", "tests", "severity", "materiality", "criticality", "comments"} and full["comments"] == {"audience": None}


def test_a_full_configuration_is_normalised():
    c = parse({"limits": {"max_depth": 10, "max_results": 300, "time_budget_seconds": 90.5, "max_concurrent_jobs": 2},
               "paths": {"strip_prefixes": ["/Gateway/", "/API/v1"]}, "tests": {"naming_convention": False},
               "severity": {"overrides": {"SEV-LOCAL": "medium"}, "inferred_cap": "high", "stale_cap": "none"},
               "materiality": {"severities": ["medium", "critical"], "breaking_on_path": False},
               "criticality": {"payments-service": "critical", "legacy": "none"}, "comments": {"audience": "all"}})
    assert c.strip_prefixes == ("/gateway", "/api/v1") and c.material_severities == ("critical", "medium") and c.time_budget_seconds == 90.5
    assert c.severity_overrides == {"SEV-LOCAL": "medium"} and (c.inferred_cap, c.stale_cap) == ("high", "none") and c.test_naming is False
    assert c.criticality == {"payments-service": "critical", "legacy": "none"} and c.comment_audience == "all" and c.breaking_is_material is False
    assert parse(c.to_dict()) == c


def test_every_problem_is_reported_at_once_with_suggestions():
    problems = bad({"limts": {}, "limits": {"max_dept": 5, "max_depth": 0, "max_results": "many", "time_budget_seconds": 99999,
                                            "max_concurrent_jobs": True},
                    "severity": {"overrides": {"SEV-LOCL": "low", "SEV-LOCAL": "urgent"}, "inferred_cap": "huge"}, "criticality": {"bad name": "critical",
                                                                                                                          "ok": "vital"},
                    "comments": {"audience": "everyone"}, "tests": {"naming_convention": "yes"}, "materiality": {"severities": ["low", "low"]}})
    text = "\n".join(problems)
    assert "unknown setting 'limts' (did you mean 'limits'?)" in text and "unknown setting 'limits.max_dept' (did you mean 'max_depth'?)" in text
    assert "limits.max_depth must be between 1 and 50 (got 0)" in text and "limits.max_results must be a whole number" in text
    assert "limits.time_budget_seconds must be between 1 and 3600" in text and "limits.max_concurrent_jobs must be a whole number" in text
    assert "unknown rule 'SEV-LOCL' (did you mean 'SEV-LOCAL'?)" in text and "severity.overrides.SEV-LOCAL must be one of" in text
    assert "severity.inferred_cap must be one of none, low, medium, high, critical" in text and "'none' removes the cap" in text
    assert "criticality: 'bad name' is not a valid service name" in text and "criticality.ok must be one of" in text
    assert "comments.audience must be one of target, readers, all or null" in text and "tests.naming_convention must be true or false" in text
    assert "materiality.severities must be a list of distinct severities" in text and len(problems) >= 12


@pytest.mark.parametrize("raw,fragment", [
    ([], "must be a JSON object"), ("x", "must be a JSON object"), ({"limits": 5}, "limits must be an object"), ({"limits": {"max_depth": 1.5}}, "whole number"),
    ({"limits": {"max_depth": None}}, "whole number"), ({"limits": {"time_budget_seconds": "5"}}, "must be a number"),
    ({"paths": {"strip_prefixes": "/a"}}, "must be a list of strings"), ({"paths": {"strip_prefixes": [1]}}, "must be a list of strings"),
    ({"paths": {"strip_prefixes": ["api"]}}, "is not a path prefix"), ({"paths": {"strip_prefixes": ["/"]}}, "is not a path prefix"),
    ({"paths": {"strip_prefixes": ["/a b"]}}, "is not a path prefix"), ({"paths": {"strip_prefixes": ["/a/{id}"]}}, "is not a path prefix"),
    ({"paths": {"strip_prefixes": ["/a?x=1"]}}, "is not a path prefix"), ({"paths": {"strip_prefixes": ["/a", "/A/"]}}, "is listed twice"),
    ({"paths": {"strip_prefixes": [f"/p{i}" for i in range(51)]}}, "at most 50 prefixes"), ({"severity": {"overrides": []}}, "must be an object"),
    ({"criticality": []}, "must be an object"), ({"materiality": {"breaking_on_path": "no"}}, "must be true or false"),
    ({"materiality": {"severities": ["urgent"]}}, "distinct severities")])
def test_invalid_values_are_refused(raw, fragment):
    assert any(fragment in p for p in bad(raw)), bad(raw)


def test_limits_accept_their_bounds_and_nothing_beyond():
    assert parse({"limits": {"max_depth": 1, "max_results": 1, "time_budget_seconds": 1, "max_concurrent_jobs": 1}}).max_depth == 1
    assert parse({"limits": {"max_depth": 50, "max_results": 5000, "time_budget_seconds": 3600, "max_concurrent_jobs": 16}}).max_results == 5000
    assert parse({"limits": {"max_depth": 8.0}}).max_depth == 8                                 # a whole float is a whole number
    for k, v in (("max_depth", 51), ("max_results", 5001), ("time_budget_seconds", 3601), ("max_concurrent_jobs", 17), ("max_depth", -1)):
        assert bad({"limits": {k: v}})


def test_identity_is_stable_for_equal_settings_and_changes_with_any_setting():
    a = parse({"limits": {"max_depth": 9}, "criticality": {"a": "critical", "b": "high"}})
    b = parse({"criticality": {"b": "high", "a": "critical"}, "limits": {"max_depth": 9}})        # key order is irrelevant
    assert a.settings_digest() == b.settings_digest() and a.stamped(3).id == b.stamped(3).id and a.stamped(3).id.startswith("tenant-v3:")
    assert a.stamped(3).ident(CONFIG_VERSION) == f"{CONFIG_VERSION};{a.stamped(3).id}" and not a.stamped(1).is_default
    variants = [{"limits": {"max_depth": 10}}, {"criticality": {"a": "critical"}}, {"comments": {"audience": "all"}},
                {"severity": {"inferred_cap": "none"}}, {"paths": {"strip_prefixes": ["/x"]}}, {"tests": {"naming_convention": False}}]
    digests = {parse(v).settings_digest() for v in variants} | {DEFAULT.settings_digest(), a.settings_digest()}
    assert len(digests) == len(variants) + 2


def test_diff_reports_setting_by_setting_changes():
    a, b = DEFAULT.to_dict(), parse({"limits": {"max_depth": 12}, "criticality": {"x": "critical"}, "paths": {"strip_prefixes": ["/g"]},
                                      "severity": {"overrides": {"SEV-LOCAL": "high"}}}).to_dict()
    d = {x["path"]: (x["from"], x["to"]) for x in diff(a, b)}
    assert d == {"limits.max_depth": (8, 12), "criticality.x": (None, "critical"), "paths.strip_prefixes": ([], ["/g"]),
                 "severity.overrides.SEV-LOCAL": (None, "high")}
    assert diff(a, a) == []


# =====================================================================================================================
# the severity policy
# =====================================================================================================================
def test_the_default_policy_changes_no_rule():
    for kind in ("breaking", "nonbreaking", "unclassified"):
        for status in ("source_confirmed", "contract_confirmed", "inferred", "stale"):
            for cross in (True, False):
                for crit in (None, "critical"):
                    assert compute(kind, status, cross, crit) == compute(kind, status, cross, crit, DEFAULT)
    assert compute("breaking", "inferred", True, "critical") == ("medium", "SEV-INFERRED-CAP")
    assert compute("breaking", "stale", True, "critical") == ("medium", "SEV-STALE-CAP")
    assert is_material("medium", False) and is_material("low", True)
    assert is_material("low", False) is False and is_material("low", False, DEFAULT) is False                  # the policy default is the old rule


@pytest.mark.parametrize("cap,expect", [("low", ("low", "SEV-INFERRED-CAP")), ("medium", ("medium", "SEV-INFERRED-CAP")),
                                        ("high", ("high", "SEV-INFERRED-CAP")), ("critical", ("critical", "SEV-INFERRED-CAP")),
                                        ("none", ("critical", "SEV-BREAKING-CRITICAL"))])
def test_the_inferred_cap_is_configurable_and_removable(cap, expect):
    assert compute("breaking", "inferred", True, "critical", parse({"severity": {"inferred_cap": cap}})) == expect
    assert compute("breaking", "inferred", True, "critical", parse({"severity": {"stale_cap": cap}})) == ("medium", "SEV-INFERRED-CAP")   # separate caps


def test_a_cap_never_raises_a_severity_the_rules_did_not_give():
    p = parse({"severity": {"inferred_cap": "critical"}})
    assert compute("nonbreaking", "inferred", True, None, p) == ("medium", "SEV-INFERRED-CAP")      # medium stays medium under a cap of critical
    assert compute("breaking", "inferred", True, None, parse({"severity": {"inferred_cap": "low"}})) == ("low", "SEV-INFERRED-CAP")


def test_overrides_and_materiality_policy():
    p = parse({"severity": {"overrides": {"SEV-BREAKING-CRITICAL": "medium"}}, "materiality": {"severities": ["critical"], "breaking_on_path": False}})
    assert apply_override("critical", "SEV-BREAKING-CRITICAL", p) == ("medium", {"rule": "SEV-BREAKING-CRITICAL", "default": "critical", "configured": "medium"})
    assert apply_override("high", "SEV-BREAKING-CONFIRMED", p) == ("high", None) and apply_override("medium", "SEV-BREAKING-CRITICAL", p) == ("medium", None)
    assert is_material("critical", False, p) and not is_material("high", False, p) and not is_material("low", True, p)
    assert is_material("low", True, parse({"materiality": {"severities": []}})) and not is_material("high", False, parse({"materiality": {"severities": []}}))
    assert compute_data("breaking", "source_confirmed", False, False, True, "critical", parse({"severity": {"inferred_cap": "none"}})) == ("critical", "SEV-BREAKING-CRITICAL")


# =====================================================================================================================
# persistence: versioned, immutable, isolated
# =====================================================================================================================
def new_store():
    t = conftest.new_tenant()
    return Store(conftest.PG.db, t), t


def test_versions_are_immutable_history_and_identical_saves_are_no_ops():
    s, t = new_store()
    assert s.effective_config() == DEFAULT and s.list_config_versions() == [] and s.get_config_version(1) is None
    r1 = s.put_config({"limits": {"max_depth": 12}}, "raise depth", "alice")
    assert (r1["version"], r1["created"], r1["id"].startswith("tenant-v1:")) == (1, True, True)
    assert [c["path"] for c in r1["changes"]] == ["limits.max_depth"] and s.effective_config().max_depth == 12 and s.effective_config().version == 1
    again = s.put_config({"limits": {"max_depth": 12}}, "same", "bob")
    assert again["created"] is False and again["version"] == 1 and again["changes"] == []                     # nothing to version
    r2 = s.put_config({"limits": {"max_depth": 15}}, "again", "bob")
    assert r2["version"] == 2 and [(c["path"], c["from"], c["to"]) for c in r2["changes"]] == [("limits.max_depth", 12, 15)]
    hist = s.list_config_versions()
    assert [v["version"] for v in hist] == [2, 1] and hist[1]["actor"] == "alice" and hist[1]["comment"] == "raise depth" and "config" not in hist[0]
    assert s.get_config_version(1)["config"]["limits"]["max_depth"] == 12 and s.effective_config().max_depth == 15


def test_a_revert_to_defaults_is_a_new_version_not_a_deletion():
    s, _ = new_store()
    s.put_config({"limits": {"max_depth": 12}})
    r = s.put_config({})
    assert r["version"] == 2 and r["created"] and s.effective_config().version == 2 and not s.effective_config().is_default       # stored, explicit
    assert s.effective_config().max_depth == 8 and s.effective_config().ident(CONFIG_VERSION) != CONFIG_VERSION


def test_the_runtime_role_cannot_rewrite_history():
    s, t = new_store()
    s.put_config({"limits": {"max_depth": 12}})
    with conftest.PG.db.tx(t) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update tenant_config_versions set config = '{}'")
    with conftest.PG.db.tx(t) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from tenant_config_versions")


def test_configuration_is_isolated_between_tenants():
    a, ta = new_store()
    b, tb = new_store()
    a.put_config({"criticality": {"payments-service": "critical"}})
    assert b.effective_config() == DEFAULT and b.list_config_versions() == []
    with conftest.PG.db.tx(tb) as c:
        assert c.execute("select count(*) from tenant_config_versions").fetchone()[0] == 0
    b.put_config({"limits": {"max_depth": 3}})
    assert a.effective_config().max_depth == 8 and a.effective_config().version == 1 and b.effective_config().version == 1


def test_concurrent_saves_get_distinct_consecutive_versions():
    s, _ = new_store()
    results, errors = [], []

    def save(i):
        try:
            results.append(Store(conftest.PG.db, s.tenant_id).put_config({"limits": {"max_results": 100 + i}})["version"])
        except Exception as e:                                                                          # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert not errors and sorted(results) == list(range(1, 9)) and len(s.list_config_versions()) == 8


def test_comment_and_actor_are_bounded_and_scrubbed():
    s, _ = new_store()
    s.put_config({"limits": {"max_depth": 9}}, "rotated token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8 " + "x" * 600, "a" * 100)
    v = s.get_config_version(1)
    assert "ghp_A1b2" not in v["comment"] and "[REDACTED:github_token]" in v["comment"] and len(v["comment"]) <= 520 and len(v["actor"]) == 64


def test_invalid_configuration_stores_nothing():
    s, _ = new_store()
    with pytest.raises(ConfigError):
        s.put_config({"limits": {"max_depth": 0}})
    assert s.list_config_versions() == []


def test_offboarding_deletes_configuration_history_too():
    s, t = new_store()
    s.put_config({"limits": {"max_depth": 9}})
    name = _name(t)
    cert = Tenants(conftest.PG.admin).offboard(name, name)
    assert cert["tables"]["tenant_config_versions"] == {"rows_before": 1, "rows_deleted": 1, "rows_remaining": 0}


def _name(tenant_id):
    with psycopg.connect(conftest.PG.admin) as c:
        return c.execute("select name from tenants where id = %s", (tenant_id,)).fetchone()[0]


def test_the_admin_cli_manages_configuration(tmp_path, capsys):
    name = "cfg-" + uuid.uuid4().hex[:8]
    Tenants(conftest.PG.admin).create(name)
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"limits": {"max_depth": 11}, "criticality": {"billing": "critical"}}))

    def run(*argv):
        code = tenants_cli(["--admin-dsn", conftest.PG.admin, *argv])
        cap = capsys.readouterr()
        return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err
    code, out, _ = run("config-show", name)
    assert code == 0 and out["version"] == 0 and out["config"]["limits"]["max_depth"] == 8
    code, out, _ = run("config-set", name, "--file", str(f), "--comment", "provisioned", "--actor", "iac")
    assert code == 0 and out["version"] == 1 and out["created"] is True
    assert run("config-set", name, "--file", str(f))[1]["created"] is False
    code, out, _ = run("config-show", name)
    assert out["version"] == 1 and out["config"]["criticality"] == {"billing": "critical"}
    f.write_text(json.dumps({"limits": {"max_depth": 0}}))
    code, _, err = run("config-set", name, "--file", str(f))
    assert code == 1 and "invalid configuration" in err and "limits.max_depth must be between" in err
    code, _, err = run("config-set", name, "--file", str(tmp_path / "missing.json"))
    assert code == 1 and "error" in err
    ev = [e["event"] for e in Tenants(conftest.PG.admin).events(name)]
    assert ev.count("config_changed") == 1
    Tenants(conftest.PG.admin).offboard(name, name)
    code, _, err = run("config-set", name, "--file", str(f))
    assert code == 1 and "offboarded" in err


# =====================================================================================================================
# the API
# =====================================================================================================================
@pytest.fixture(scope="module")
def fxm(tmp_path_factory):
    return build(str(tmp_path_factory.mktemp("cfg-landscape")))


@pytest.fixture
def app(fxm, tmp_path):
    tenant = conftest.new_tenant()
    c = TestClient(make_app(tmp_path / "ws", tenant=tenant, allow_local=True, gate_ttl=0))
    c.tenant, c.fx, c.ws = tenant, fxm, tmp_path / "ws"
    for n, p in fxm["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    return c


CFG = "/api/v1/tenant/config"


def analyse(c, head="breaking", **kw):
    r = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": c.fx["shas"].get(head, head), "base": "main", **kw})
    assert r.status_code == 200, r.text
    return r.json()["report"]


def save(c, config, comment=None, **headers):
    r = c.put(CFG, json={"config": config, "comment": comment}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def by(rep, component, node_type=None):
    return [f for f in rep["findings"] if f["component"] == component and (node_type is None or f["node_type"] == node_type)]


def test_the_configuration_api(app):
    g = app.get(CFG).json()
    assert g["is_default"] and g["version"] == 0 and g["id"] == "default" and g["config"] == g["defaults"] == DEFAULT.to_dict()
    v = app.post(CFG + "/validate", json={"config": {"limits": {"max_depth": 12}}}).json()
    assert v["valid"] and v["config"]["limits"]["max_depth"] == 12 and v["changes"] == [{"path": "limits.max_depth", "from": 8, "to": 12}]
    nv = app.post(CFG + "/validate", json={"config": {"limts": {}, "limits": {"max_depth": 0}}}).json()
    assert nv["valid"] is False and len(nv["problems"]) == 2 and app.get(CFG).json()["version"] == 0                 # a dry run saves nothing
    r = app.put(CFG, json={"config": {"limits": {"max_depth": 0}, "bogus": 1}})
    assert r.status_code == 422 and r.json()["detail"]["message"] == "invalid configuration" and len(r.json()["detail"]["problems"]) == 2
    assert app.get(CFG).json()["version"] == 0

    saved = save(app, {"limits": {"max_depth": 12}}, "raise the depth", **{"X-TIA-Actor": "alice@bank"})
    assert saved["version"] == 1 and saved["created"] and saved["changes"] == [{"path": "limits.max_depth", "from": 8, "to": 12}]
    g = app.get(CFG).json()
    assert g["version"] == 1 and not g["is_default"] and g["config"]["limits"]["max_depth"] == 12 and g["actor"] == "alice@bank" and g["comment"] == "raise the depth"
    assert save(app, {"limits": {"max_depth": 12}})["created"] is False
    save(app, {"limits": {"max_depth": 15}}, None, **{"X-TIA-Actor": "bad;actor\nname"})
    vers = app.get(CFG + "/versions").json()
    assert [x["version"] for x in vers] == [2, 1] and vers[0]["actor"] == "api" and vers[1]["actor"] == "alice@bank"           # unsafe actor replaced
    assert app.get(CFG + "/versions/1").json()["config"]["limits"]["max_depth"] == 12 and app.get(CFG + "/versions/9").status_code == 404
    d = app.get(CFG + "/diff", params={"from_version": 1, "to_version": 2}).json()
    assert d["changes"] == [{"path": "limits.max_depth", "from": 12, "to": 15}]
    assert app.get(CFG + "/diff", params={"from_version": 0, "to_version": 1}).json()["changes"][0]["path"] == "limits.max_depth"
    assert app.get(CFG + "/diff", params={"from_version": 0, "to_version": 7}).status_code == 404
    audit = [a for a in app.get("/api/v1/audit?limit=50").json() if a["event"] == "tenant_config_changed"]
    assert len(audit) == 2 and audit[0]["version"] == 2 and audit[0]["changes"] == ["limits.max_depth"] and {a["actor"] for a in audit} == {"alice@bank", "api"}


def test_restore_rolls_back_by_adding_a_version(app):
    save(app, {"limits": {"max_depth": 12}}, "one")
    save(app, {"limits": {"max_depth": 20}}, "two")
    r = app.post(CFG + "/versions/1/restore", headers={"X-TIA-Actor": "ops"}).json()
    assert r["version"] == 3 and r["created"] and app.get(CFG).json()["config"]["limits"]["max_depth"] == 12
    assert app.get(CFG + "/versions/2").json()["config"]["limits"]["max_depth"] == 20                                        # history intact
    assert app.get(CFG + "/versions/3").json()["comment"] == "restored from version 1"
    assert app.post(CFG + "/versions/3/restore").json()["created"] is False and app.post(CFG + "/versions/9/restore").status_code == 404
    assert [a["comment"] for a in app.get("/api/v1/audit?limit=20").json() if a["event"] == "tenant_config_changed"][0] == "restored from version 1"


def test_configuration_cannot_be_changed_while_the_tenant_is_suspended(app):
    name = _name(app.tenant)
    Tenants(conftest.PG.admin).suspend(name, "x")
    assert app.put(CFG, json={"config": {}}).status_code == 403 and app.get(CFG).status_code == 403
    Tenants(conftest.PG.admin).resume(name)


# ---- identity: nothing changes until a tenant configures something --------------------------------------------------------------------
def test_without_a_configuration_every_identity_is_exactly_as_before(app):
    rep = analyse(app)
    assert rep["configuration_version"] == CONFIG_VERSION and "configuration" not in rep["coverage"]
    assert not any(u["area"] == "severity_overridden" for u in rep["unknowns"]) and not any("severity_override" in f for f in rep["findings"])
    snap = app.get(f"/api/v1/evidence-snapshots/{rep['evidence_snapshot_id']}").json()
    assert snap["config_version"] == CONFIG_VERSION and snap["tenant_config"] == {"version": 0, "digest": "default", "settings": DEFAULT.to_dict()}
    assert analyse(app)["analysis_id"] == rep["analysis_id"]                                                                   # still deterministic


def test_a_saved_configuration_becomes_part_of_the_report_identity(app):
    base = analyse(app)
    cfg = save(app, {"criticality": {"mobile-bff": "critical"}})
    rep = analyse(app)
    assert rep["configuration_version"] == f"{CONFIG_VERSION};{cfg['id']}" and rep["coverage"]["configuration"]["id"] == cfg["id"]
    assert rep["coverage"]["configuration"]["settings_changed"] == ["criticality.mobile-bff"]
    assert rep["analysis_id"] != base["analysis_id"] and rep["evidence_snapshot_id"] != base["evidence_snapshot_id"]


# ---- limits -----------------------------------------------------------------------------------------------------------------------------------
def test_limits_are_the_defaults_and_the_ceilings(app):
    save(app, {"limits": {"max_depth": 2, "max_results": 3}})
    rep = analyse(app)                                                                                                         # nothing requested: the tenant's limit
    assert rep["coverage"]["traversal"]["max_depth"] == 2 and rep["coverage"]["traversal"]["max_results"] == 3
    big = analyse(app, max_depth=50, max_results=5000)                                                                         # asking for more is clamped
    assert big["coverage"]["traversal"]["max_depth"] == 2 and big["coverage"]["traversal"]["max_results"] == 3
    low = analyse(app, max_depth=1)                                                                                            # asking for less is honoured
    assert low["coverage"]["traversal"]["max_depth"] == 1 and low["coverage"]["traversal"]["max_results"] == 3
    save(app, {"limits": {"max_depth": 12, "max_results": 400}})
    rep = analyse(app)
    assert rep["coverage"]["traversal"]["max_depth"] == 12 and rep["coverage"]["traversal"]["max_results"] == 400            # a raised default applies
    assert rep["status"] == "complete_with_gaps" and not rep["coverage"]["traversal"]["truncated"]


def test_a_tight_limit_truncates_and_says_so(app):
    save(app, {"limits": {"max_results": 1}})
    rep = analyse(app)
    assert rep["status"] == "partial" and rep["coverage"]["traversal"]["truncated"] is True
    assert any(u["area"] == "truncated_traversal" for u in rep["unknowns"])


def test_the_time_budget_reaches_the_engine(app, monkeypatch):
    seen = {}
    import tia.analysis_service as svc
    real = svc.analyze

    def spy(req, store=None, persist=True):
        seen["budget"], seen["cfg"] = req.time_budget_s, req.config.id
        return real(req, store, persist)
    monkeypatch.setattr(svc, "analyze", spy)
    save(app, {"limits": {"time_budget_seconds": 7}})
    analyse(app)
    assert seen["budget"] == 7 and seen["cfg"].startswith("tenant-v1:")


# ---- gateway prefixes -------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def gateway(tmp_path):
    """A provider that serves /v1/items/{id} and a consumer that calls it through a gateway at /gateway/v1/items/{id}."""
    def repo(name, files):
        p = tmp_path / name
        p.mkdir()
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        return p, _commit(p, "init")
    prov, _ = repo("items-api", {"app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/v1/items/{item_id}')\ndef get_item(item_id: str):\n    return {'id': item_id, 'name': 'x'}\n",
                                 "tia-services.yaml": "service: items-api\n"})
    cons, _ = repo("storefront", {"client.py": "import requests\n\ndef fetch(i):\n    return requests.get(f'https://api.internal/gateway/v1/items/{i}').json()\n",
                                  "tia-services.yaml": "service: storefront\n"})
    _run(prov, "checkout", "-q", "-b", "change", "main")
    _write(prov, {"app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/v1/items/{item_id}')\ndef get_item(item_id: str):\n    return {'id': item_id, 'label': 'x'}\n"})
    _commit(prov, "change")
    _run(prov, "checkout", "-q", "main")
    return {"items-api": str(prov), "storefront": str(cons)}


def test_gateway_prefixes_decide_what_resolves_and_the_cache_respects_them(gateway, tmp_path):
    c = TestClient(make_app(tmp_path / "ws", allow_local=True, gate_ttl=0))
    for n, p in gateway.items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200

    def run():
        r = c.post("/api/v1/impact-analyses", json={"target": "items-api", "head": "change", "base": "main"})
        assert r.status_code == 200, r.text
        return r.json()["report"]
    before = run()
    assert not by(before, "storefront")                                                    # '/gateway/v1/...' is not the provider's path
    assert before["coverage"]["outbound_call_sites_resolved"] == 0
    save(c, {"paths": {"strip_prefixes": ["/gateway"]}})
    after = run()                                                                          # same commits: the cache must not serve the old paths
    f = by(after, "storefront", "callsite")
    assert f and f[0]["evidence_status"] == "inferred" and f[0]["resolution_strategy"] == "R5"
    assert after["coverage"]["outbound_call_sites_resolved"] == 1
    save(c, {})                                                                            # back to no prefixes: back to unresolved
    assert not by(run(), "storefront")
    save(c, {"paths": {"strip_prefixes": ["/gateway"]}})
    assert by(run(), "storefront", "callsite")                                              # and again


def test_the_landscape_graph_uses_the_same_prefixes(gateway, tmp_path):
    c = TestClient(make_app(tmp_path / "ws", allow_local=True, gate_ttl=0))
    for n, p in gateway.items():
        c.post("/api/v1/repositories", json={"path": p, "name": n})
    edges = lambda: [e for e in c.get("/api/v1/graph", params={"level": "service"}).json()["edges"] if e["rel"] == "CONSUMES_API"]     # noqa: E731
    assert edges() == []
    save(c, {"paths": {"strip_prefixes": ["/gateway"]}})
    assert len(edges()) == 1 and edges()[0]["source"] == "svc:storefront"                                                              # graph view and analysis agree
    assert c.post("/api/v1/graph/rescan").json()["graph_drift"] is False


def test_the_extract_cache_is_keyed_by_the_prefixes_it_was_built_with():
    s, _ = new_store()
    assert Store._key("r", "c", "v") == "r@c#v" and Store._key("r", "c", "v", ()) == "r@c#v"                                # unchanged when there are none
    assert Store._key("r", "c", "v", ("/a", "/b")) == "r@c#v|strip=/a,/b" and Store._key("r", "c", "v", ("/a",)) != Store._key("r", "c", "v", ("/b",))
    from tia.models import RepoExtract
    ex = RepoExtract(repo="r", service="r", commit="c", extractor_version="v", strip_prefixes=["/g"])
    s.put_extract(ex)
    assert s.get_extract("r", "c", "v", ("/g",)).strip_prefixes == ["/g"] and s.get_extract("r", "c", "v") is None and s.get_extract("r", "c", "v", ("/h",)) is None


# ---- severity policy through the engine ------------------------------------------------------------------------------------------------------
def test_a_severity_override_is_applied_and_never_silent(app):
    default = analyse(app)
    crit = [f for f in default["findings"] if f["severity"] == "critical"]
    assert crit and all(f["severity_rule"] == "SEV-BREAKING-CRITICAL" for f in crit)
    cfg = save(app, {"severity": {"overrides": {"SEV-BREAKING-CRITICAL": "medium"}}})
    rep = analyse(app)
    changed = [f for f in rep["findings"] if f.get("severity_override")]
    assert {f["node"] for f in changed} == {f["node"] for f in crit}
    for f in changed:
        assert f["severity"] == "medium" and f["severity_rule"] == "SEV-BREAKING-CRITICAL"                                         # the rule that fired stays visible
        assert f["severity_override"] == {"rule": "SEV-BREAKING-CRITICAL", "default": "critical", "configured": "medium"}
        assert any(f"Severity set by tenant configuration {cfg['id']} (critical under the default rules)" == x for x in f["limitations"])
    u = next(u for u in rep["unknowns"] if u["area"] == "severity_overridden")
    assert u["count"] == len(crit) and u["items"] == [{"rule": "SEV-BREAKING-CRITICAL", "count": len(crit)}] and not u["affects_confidence"]
    assert cfg["id"] in u["reason"] and rep["summary"]["by_severity"]["critical"] == 0
    assert [f["severity"] for f in rep["findings"]] == sorted([f["severity"] for f in rep["findings"]], key=lambda s: -["unknown", "low", "medium", "high", "critical"].index(s))
    assert default["summary"]["by_severity"]["critical"] == len(crit)                                                              # the old report is untouched


def test_the_inferred_cap_through_the_engine(app):
    default = analyse(app)
    inferred = [f for f in default["findings"] if f["evidence_status"] == "inferred"]
    assert inferred and all(f["severity"] == "medium" and f["severity_rule"] == "SEV-INFERRED-CAP" for f in inferred)
    save(app, {"severity": {"inferred_cap": "none"}})
    rep = analyse(app)
    again = [f for f in rep["findings"] if f["node"] in {i["node"] for i in inferred}]
    assert again and all(f["severity"] == "high" and f["severity_rule"] == "SEV-BREAKING-CONFIRMED" and f["evidence_status"] == "inferred" for f in again)
    assert not any(f.get("severity_override") for f in rep["findings"])                                                           # a cap is not an override


def test_materiality_thresholds_change_what_is_material(app):
    default = analyse(app)
    assert default["summary"]["material_findings"] == 5
    save(app, {"materiality": {"severities": ["critical"], "breaking_on_path": False}})
    rep = analyse(app)
    assert rep["summary"]["material_findings"] == rep["summary"]["by_severity"]["critical"] == 2
    assert {f["severity"] for f in rep["findings"] if f["material"]} == {"critical"} and len(rep["findings"]) == 5
    comment = app.get(f"/api/v1/impact-analyses/{rep['analysis_id']}/comment").text
    assert comment.count("| CRITICAL |") == 2 and "| HIGH |" not in comment and "| MEDIUM |" not in comment
    save(app, {"materiality": {"severities": [], "breaking_on_path": True}})
    assert all(f["material"] == (f["change_nature"] == "breaking") for f in analyse(app)["findings"])


def test_criticality_in_the_tenant_registry_wins_over_the_repository_file(app):
    default = analyse(app)
    assert by(default, "reconciliation-service", "callsite")[0]["severity"] == "critical"                                       # tia-services.yaml says critical
    save(app, {"criticality": {"reconciliation-service": "none"}})
    rep = analyse(app)
    f = by(rep, "reconciliation-service", "callsite")[0]
    assert f["severity"] == "high" and f["severity_rule"] == "SEV-BREAKING-CONFIRMED"                                           # the registry removed it
    assert rep["coverage"]["criticality_configured"] is True and "criticality.reconciliation-service" in rep["coverage"]["configuration"]["settings_changed"]
    save(app, {"criticality": {"mobile-bff": "critical"}})                                                                       # and can add it
    assert by(analyse(app), "mobile-bff")[0]["severity"] == "critical"


# ---- determinism ---------------------------------------------------------------------------------------------------------------------------------
def test_reproduce_uses_the_configuration_the_report_was_made_under(app):
    save(app, {"severity": {"overrides": {"SEV-BREAKING-CRITICAL": "medium"}}}, "v1 policy")
    first = analyse(app)
    snap = app.get(f"/api/v1/evidence-snapshots/{first['evidence_snapshot_id']}").json()
    assert snap["tenant_config"]["version"] == 1 and snap["tenant_config"]["settings"]["severity"]["overrides"] == {"SEV-BREAKING-CRITICAL": "medium"}
    save(app, {"severity": {"overrides": {"SEV-BREAKING-CRITICAL": "low"}}, "limits": {"max_depth": 3}}, "v2 policy")             # the tenant moved on
    rep = app.post(f"/api/v1/impact-analyses/{first['analysis_id']}/reproduce").json()
    assert rep["identical"] is True and rep["differences"] == [] and rep["versions_match"] is True                               # judged by ITS config
    assert rep["report"]["configuration_version"] == first["configuration_version"]
    second = analyse(app)
    assert second["analysis_id"] != first["analysis_id"] and second["configuration_version"] != first["configuration_version"]
    assert {f["severity"] for f in second["findings"] if f.get("severity_override")} == {"low"}                                  # new analyses use the new policy
    assert app.get(f"/api/v1/impact-analyses/{first['analysis_id']}").json()["findings"] == first["findings"]                    # the old report is immutable
    assert app.post(f"/api/v1/impact-analyses/{second['analysis_id']}/reproduce").json()["identical"] is True


def test_job_identity_includes_the_configuration(app):
    body = {"target": "payments-service", "head": app.fx["shas"]["impl"], "base": "main"}
    a = app.post("/api/v1/impact-analyses?async=true", json=body).json()
    assert app.post("/api/v1/impact-analyses?async=true", json=body).json()["job_id"] == a["job_id"]                                # same request, same config: one job
    save(app, {"limits": {"max_depth": 5}})
    b = app.post("/api/v1/impact-analyses?async=true", json=body).json()
    assert b["job_id"] != a["job_id"] and b["deduplicated"] is False                                                                # a new configuration is a new analysis
    assert app.post("/api/v1/impact-analyses?async=true", json=body).json()["job_id"] == b["job_id"]


def test_the_queue_carries_the_configuration_into_the_worker(env):
    from tia.analysis_service import make_handlers
    from tia.worker import Worker
    cfg = Store(conftest.PG.db, env.tenant)
    cfg.put_config({"severity": {"overrides": {"SEV-BREAKING-CRITICAL": "low"}}, "limits": {"max_depth": 4}})
    r = env.c.post("/api/v1/impact-analyses?async=true", json={"target": "payments-service", "head": env.fx["shas"]["breaking"], "base": "main"})
    h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(env.ws), scm=env.scm)
    w = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
    while w.run_once():
        pass
    job = env.c.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    rep = env.c.get(f"/api/v1/impact-analyses/{job['result']['analysis_id']}").json()
    assert rep["coverage"]["traversal"]["max_depth"] == 4 and any(f.get("severity_override") for f in rep["findings"])


def test_a_pull_request_event_uses_the_tenant_configuration_and_audience(env):
    Store(conftest.PG.db, env.tenant).put_config({"comments": {"audience": "all"}, "severity": {"overrides": {"SEV-BREAKING-CRITICAL": "medium"}}})
    breaking = env.fx["shas"]["breaking"]
    set_pr(env.origins, 91, breaking)
    first = env.post(env.event(91, breaking)).json()
    env.drain()                                                                         # the deployment default is 'target' only; the tenant said 'all'
    body = env.gh.comments[91][0]["body"]
    assert "reconciliation-service" in body and "mobile-bff" in body and "MEDIUM" in body and "CRITICAL" not in body
    Store(conftest.PG.db, env.tenant).put_config({"comments": {"audience": None}, "severity": {"overrides": {"SEV-BREAKING-CRITICAL": "medium"}}})
    again = env.post(env.event(91, breaking, action="synchronize")).json()
    assert again["job_id"] != first["job_id"] and again["deduplicated"] is False                                                     # a configuration change re-analyses
    env.drain()
    assert "reconciliation-service" not in env.gh.comments[91][0]["body"]                                                            # back to the deployment default


# ---- the page, the comment and the UI show what is configured -------------------------------------------------------------------------------------------
def test_the_report_page_and_footer_name_the_configuration(app):
    cfg = save(app, {"severity": {"overrides": {"SEV-BREAKING-CRITICAL": "medium"}}, "paths": {"strip_prefixes": ["/gateway"]}})
    rep = analyse(app)
    page = app.get(f"/analyses/{rep['analysis_id']}").text
    assert cfg["id"] in page and "severity_overridden" in page and "Severity set by tenant configuration" in page
    assert cfg["id"] in app.get(f"/api/v1/impact-analyses/{rep['analysis_id']}/comment").text


def test_the_ui_has_a_configuration_panel_that_shows_problems_and_history():
    html = open(__file__.replace("tests\\test_tenantconfig.py", "tia\\web\\index.html").replace("tests/test_tenantconfig.py", "tia/web/index.html"), encoding="utf-8").read()
    for needle in ('id="cfgtext"', 'id="cfgvalidate"', 'id="cfgsave"', 'id="cfghist"', '/api/v1/tenant/config/validate', 'X-TIA-Actor', 'data-restore', "cfgProblems(d && d.problems"):
        assert needle in html, needle


# ---- per-tenant concurrency ---------------------------------------------------------------------------------------------------------------------------
def test_a_tenants_job_limit_lowers_what_the_worker_runs_for_it():
    kind = "kind-" + uuid.uuid4().hex[:8]
    s_limited, t1 = new_store()
    t2 = conftest.new_tenant()
    s_limited.put_config({"limits": {"max_concurrent_jobs": 1}})
    q, w = JobQueue(conftest.PG.db), JobQueue(conftest.PG.worker_db)
    for i in range(3):
        q.enqueue(t1, kind, {"i": i}, key=f"a{i}")
        q.enqueue(t2, kind, {"i": i}, key=f"b{i}")
    claimed = [w.claim("w", kinds=[kind], per_tenant_cap=4) for _ in range(8)]
    claimed = [j for j in claimed if j]
    by_tenant = {t1: sum(1 for j in claimed if j.tenant_id == t1), t2: sum(1 for j in claimed if j.tenant_id == t2)}
    assert by_tenant == {t1: 1, t2: 3}                                                           # limited tenant: 1 at a time; the other: all 3 (cap 4)
    for j in claimed:
        w.succeed(j, {})
    assert w.claim("w", kinds=[kind], per_tenant_cap=4) is not None                               # finishing one frees the slot
    s_limited.put_config({"limits": {"max_concurrent_jobs": 16}})
    q.enqueue(t1, kind, {}, key="more1")
    q.enqueue(t1, kind, {}, key="more2")
    got = [w.claim("w", kinds=[kind], per_tenant_cap=2) for _ in range(4)]
    assert sum(1 for j in got if j and j.tenant_id == t1) <= 2                                    # a tenant can lower the deployment's cap, never raise it


def test_job_cap_function_privileges():
    s, t = new_store()
    s.put_config({"limits": {"max_concurrent_jobs": 2}})
    with psycopg.connect(conftest.PG.app_dsn) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select tia_tenant_job_cap(%s)", (t,))                                    # the API role cannot read other tenants' limits
    with psycopg.connect(conftest.PG.worker_dsn) as c:
        assert c.execute("select tia_tenant_job_cap(%s)", (t,)).fetchone()[0] == 2
        assert c.execute("select tia_tenant_job_cap(%s)", (str(uuid.uuid4()),)).fetchone()[0] is None
