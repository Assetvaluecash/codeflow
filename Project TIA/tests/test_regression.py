"""Regression test selection and configuration / environment-variable changes (docs/MEETING-PAWAN.md, use case 1 and ask 1a).

Main branch + feature branch of one payments app with ten written functional test cases: for each change, which of the ten to run again
and why; a case naming nothing TIA can match is listed for review, never dropped; a new environment variable the change starts reading is
told to the tester with where it is (not) set and who else reads it; a changed configuration key reaches the code that reads it, and its
value never appears in the report."""
import json
import subprocess

import pytest

from fixtures.build_regression_fixture import build
from tia import configkeys, kfadapter
from tia.impact import AnalysisRequest, analyze
from tia.models import Function
from tia.report import redact, render_comment
from tia.reportpage import render_page

ALL = {f"TC-{i:02d}" for i in range(1, 11)}


@pytest.fixture(scope="module")
def reps(tmp_path_factory):
    fx = build(str(tmp_path_factory.mktemp("regression")))
    run = lambda b: analyze(AnalysisRequest(repos=fx["paths"], target="payments-app", base=fx["shas"]["base"], head=fx["shas"][b]))
    return {b: run(b) for b in ("feature/holds", "feature/min-balance", "feature/fx-rounding")}


def ids(rows):
    return [c["id"] for c in rows]


def test_holds_selects_four_of_ten_with_reasons(reps):
    reg = reps["feature/holds"]["test_scope"]["regression"]
    assert reg["summary"]["test_cases"] == 10 and reg["summary"]["selected"] == 4
    assert reg["summary"]["headline"].startswith("Regression-test 4 of your 10 test case(s)")
    assert set(ids(reg["selected"])) == {"TC-01", "TC-02", "TC-03", "TC-07"}
    assert ids(reg["unmapped"]) == ["TC-10"] and "review it by hand" in reg["unmapped"][0]["reason"]
    assert set(ids(reg["selected"])) | set(ids(reg["not_selected"])) | set(ids(reg["unmapped"])) == ALL     # every case in exactly one bucket
    by = {c["id"]: c for c in reg["selected"]}
    assert by["TC-07"]["reasons"][0]["entry_point"] == "GET /accounts/{account_id}/balance"
    assert by["TC-07"]["change_effect"] == "behaviour_change" and "balance_of" in by["TC-07"]["reasons"][0]["why"]
    assert by["TC-03"]["reasons"][0]["via_page"] == ["/pay"] and "on page /pay" in by["TC-03"]["reason"]   # a UI test case, through its screen
    assert [c["priority"] for c in reg["selected"]][:3] == ["High"] * 3                                    # stronger effect, then priority
    assert "which this change does not reach" in {c["id"]: c for c in reg["not_selected"]}["TC-04"]["reason"]


def test_holds_tells_the_tester_about_the_new_environment_variable(reps):
    rep = reps["feature/holds"]
    [chk] = rep["test_scope"]["config_checks"]
    assert chk["kind"] == "config_newly_read" and chk["key"] == "HOLDS_ENABLED" and chk["set_in"] == []
    assert chk["readers"][0]["function"] == "balance_of" and [o["function"] for o in chk["other_readers"]] == ["statement"]
    assert {e["entry_point"] for e in chk["entry_points"]} == {"GET /accounts/{account_id}/balance", "POST /payments"}
    assert "make sure it is set in Dev" in chk["check"] and "statement already reads the same key" in chk["check"]
    assert any(c["type"] == "config_key" and c["change"] == "newly_read" for c in rep["change"]["changed_components"])
    unk = {u["area"]: u for u in rep["unknowns"]}
    assert unk["configuration_outside_repository"]["items"] == [{"key": "HOLDS_ENABLED"}]
    assert unk["configuration_outside_repository"]["affects_confidence"] is False


def test_config_change_reaches_the_code_that_reads_it_and_hides_values(reps):
    rep = reps["feature/min-balance"]
    assert rep["change"]["change_type"] == "config_or_file_change"
    reg = rep["test_scope"]["regression"]
    assert set(ids(reg["selected"])) == {"TC-01", "TC-02", "TC-03"}
    assert [e["entry_point"] for e in rep["test_scope"]["entry_points"]] == ["POST /payments"]
    checks = {c["key"]: c for c in rep["test_scope"]["config_checks"]}
    mb = checks["MIN_BALANCE"]
    assert mb["change"] == "value_changed" and mb["readers"][0]["function"] == "can_pay"       # module constant followed to its users
    assert "No analysed code reads it" in checks["FX_PROVIDER"]["check"]
    areas = {u["area"] for u in rep["unknowns"]}
    assert "file_level_analysis" not in areas                                                  # the .env file was read, not file-level
    text = json.dumps(rep) + render_comment(rep) + render_page(rep, "/x")
    assert "7f3a9c" not in text and "MIN_BALANCE=100" not in text                              # values are never reported


def test_new_test_cases_of_the_change_are_not_regression(reps):
    rep = reps["feature/fx-rounding"]
    reg = rep["test_scope"]["regression"]
    assert set(ids(reg["selected"])) == {"TC-08", "TC-09"} and reg["summary"]["test_cases"] == 10
    assert reg["changed_by_this_change"] == [{**reg["changed_by_this_change"][0], "id": "TC-11", "change": "added"}]
    assert not any(u["area"] == "file_level_analysis" for u in rep["unknowns"])                # a test-case file is a test, not unknown


def test_renderers_and_knowledge_fabric_carry_the_selection(reps):
    rep = reps["feature/holds"]
    md = render_comment(rep)
    assert "**Regression-test 4 of your 10 test case(s)" in md and "`TC-07` Account balance" in md
    assert "Could not be mapped (review by hand): `TC-10`" in md and "**Configuration** - balance_of now reads" in md
    assert md.index("Regression-test") < md.index("What to test")
    page = render_page(rep, "/x")
    assert "Test case to run again" in page and "Not affected by this change (5)" in page and "HOLDS_ENABLED" in page
    out = kfadapter.to_query_response(rep, kfadapter.QueryRequest(analysis_id="x")).model_dump()
    cats = [c["category"] for c in out["reranked_chunks"]]
    assert "regression_summary" in cats and cats.count("regression_test") == 4 and "config_check" in cats
    first = next(c for c in out["reranked_chunks"] if c["category"] in ("regression_summary", "regression_test", "test_scope", "rule_gap"))
    assert first["category"] == "regression_summary"


def test_redaction(reps):
    rep = reps["feature/holds"]
    same = redact(rep, ["payments-app"])["test_scope"]
    assert same["regression"]["summary"]["selected"] == 4 and len(same["config_checks"]) == 1
    none = redact(rep, [])["test_scope"]
    assert none["regression"]["selected"] == [] and none["regression"]["unmapped"] == [] and none["config_checks"] == []
    assert none["regression"]["summary"]["test_cases"] == 0 and none["redacted"]["items"] >= 11


# ---- configuration parsing and reads (unit) ------------------------------------------------------------------------------------------
def test_keys_compare_with_relaxed_binding():
    assert len({configkeys.norm_key(k) for k in ("payments.base-url", "PAYMENTS_BASEURL", "payments:base_url", "Payments.BaseUrl")}) == 1


def test_parse_config_formats():
    assert configkeys.parse_config("a.properties", "# c\npayments.url=http://x\nmode: fast\n") == {"payments.url": "http://x", "mode": "fast"}
    assert set(configkeys.parse_config(".env", "export A=1\nB=2\n")) == {"A", "B"}
    compose = "services:\n  api:\n    image: x\n    environment:\n      - HOLDS_ENABLED=true\n      - MODE=x\n"
    assert {"HOLDS_ENABLED", "MODE", "services.api.image"} == set(configkeys.parse_config("docker-compose.yml", compose))
    k8s = "spec:\n  containers:\n    - env:\n        - name: HOLDS_ENABLED\n          value: 'true'\n"
    assert "HOLDS_ENABLED" in configkeys.parse_config("deploy/app.yaml", k8s)
    assert configkeys.parse_config("app.ini", "[db]\nhost = h\n") == {"db.host": "h"}
    assert configkeys.parse_config("appsettings.json", '{"Payments": {"Url": "u"}}') == {"Payments.Url": '"u"'}
    with pytest.raises(ValueError):
        configkeys.parse_config("x.yml", "a: [unclosed")
    assert configkeys.config_diff("x.yml", "a: [", "a: 1") is None                  # unreadable: left to file-level analysis


def test_which_files_are_configuration():
    yes = ["config/app.env", ".env", ".env.dev", "src/main/resources/application.yml", "app.properties", "appsettings.Development.json",
           "deploy/k8s.yaml", "settings.toml"]
    no = ["tia-services.yaml", "openapi.yaml", "package.json", "testcases/a.yaml", ".github/workflows/ci.yml", "pyproject.toml", "app.py"]
    assert all(configkeys.is_config_file(p) for p in yes) and not any(configkeys.is_config_file(p) for p in no)


def test_reads_in_each_language():
    lines = {
        'x = os.getenv("A")': ["A"], "y = os.environ['B']": ["B"], 'z = os.environ.get("C", "1")': ["C"],
        'String s = System.getenv("D");': ["D"], '@Value("${payments.url:http://x}")': ["payments.url"],
        'env.getProperty("e.key")': ["e.key"], "const f = process.env.F_KEY;": ["F_KEY"], 'process.env["G"]': ["G"],
        'h := os.Getenv("H")': ["H"], 'var i = Environment.GetEnvironmentVariable("I");': ["I"], 'Configuration["Sec:J"]': ["Sec:J"],
        'cfg.GetValue<int>("K")': ["K"], 'print("getenv")': [],
    }
    for text, want in lines.items():
        assert configkeys.keys_in_line(text) == want, text


def test_reads_outside_functions_are_followed_to_their_users(tmp_path):
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    java = ('class Client {\n  @Value("${payments.url}")\n  private String baseUrl;\n\n  String get() {\n    return baseUrl + "/x";\n  }\n'
            '  String other() {\n    return "y";\n  }\n}\n')
    (repo / "src" / "Client.java").write_text(java, encoding="utf-8")
    (repo / "m.py").write_text('import os\nLIMIT = int(os.getenv("LIMIT", "5"))\n\n\ndef check(x):\n    return x < LIMIT\n', encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=repo, check=True)
    fns = [Function("r:get", "r", "src/Client.java", "Client.get", 5, "h", end_line=7),
           Function("r:other", "r", "src/Client.java", "Client.other", 8, "h", end_line=10),
           Function("r:check", "r", "m.py", "check", 5, "h", end_line=6)]
    got = {(r["key"], r["function"], r["via"]) for r in configkeys.reads_at(str(repo), "HEAD", fns)}
    assert got == {("payments.url", "r:get", "baseUrl"), ("LIMIT", "r:check", "LIMIT")}
