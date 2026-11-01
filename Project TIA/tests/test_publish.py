"""UC1-14: advisory check run + the product endpoint that posts the PR comment, against real PostgreSQL, real git remotes and a local
fake of the GitHub REST API (check runs need a GitHub App on github.com: see summary.md for what is live-verified)."""
import json
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import make_app
from test_webhooks import SECRET, TOKEN, FakeGitHub, env, gh, git, origins, set_pr  # noqa: F401  (fixtures)
from tia import github
from tia.impact import AnalysisRequest, analyze
from tia.github import CHECK_NAME, check_run_annotations, check_run_payload
from tia.report import MARKER, permalink, render_comment
from tia.webhooks import github_full_name

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def analyse(env, head="breaking"):
    r = env.c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": env.fx["shas"].get(head, head), "base": "main"})
    assert r.status_code == 200, r.text
    return r.json()["report"]


def failed_report(env):
    """An analysis whose proposed revision cannot be read (the API refuses unknown refs up front, the engine still must cope)."""
    return analyze(AnalysisRequest(repos=env.fx["paths"], target="payments-service", base="main", head="0" * 40), None)


def TestClientFor(env, scm):
    """A second app over the same workspace with a different connector configuration."""
    return TestClient(make_app(env.ws, tenant=env.tenant, allow_local=True, scm=scm))


def publishable(env, monkeypatch):
    """The fixture remotes are file:// URLs; give the repository the GitHub identity the endpoint publishes to."""
    monkeypatch.setattr("tia.api.github_full_name", lambda url: "acme/payments-service" if url else None)


# ---- pure helpers -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://github.com/acme/payments-service.git", "acme/payments-service"),
    ("https://github.com/Acme/Payments/", "Acme/Payments"),
    ("git@github.com:acme/payments-service.git", "acme/payments-service"),
    ("https://ghe.corp.example/org/team/repo.git", "team/repo"),
    ("file:///tmp/x/payments-service.git", None), ("/tmp/x", None), (None, None), ("", None), ("https://github.com/onlyone", None)])
def test_github_full_name(url, expected):
    assert github_full_name(url) == expected


def test_permalink_and_comment_footer_link():
    assert permalink("https://tia.example.com/", "tia-1") == "https://tia.example.com/analyses/tia-1" and permalink(None, "x") is None
    rep = {"analysis_id": "tia-1", "status": "failed", "failure_reason": "x", "change": {"repository": "r"}, "configuration_version": "c"}
    assert "[Full report](https://t/analyses/tia-1)" in render_comment(rep, link="https://t/analyses/tia-1")
    assert "Full report" not in render_comment(rep)


# ---- the check run itself ---------------------------------------------------------------------------------------------------
def test_check_run_is_always_neutral_and_never_blocks(env):
    breaking = analyse(env)                                                    # complete_with_gaps, critical findings
    partial = env.c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": env.fx["shas"]["breaking"],
                                                          "base": "main", "max_results": 2}).json()["report"]
    clean = analyse(env, "docs_only")
    failed = failed_report(env)
    assert {breaking["status"], partial["status"], failed["status"]} >= {"complete_with_gaps", "partial", "failed"}
    for rep in (breaking, partial, clean, failed):
        p = check_run_payload(rep, "a" * 40, "https://t/analyses/x")
        assert p["conclusion"] == "neutral" and p["status"] == "completed" and p["name"] == CHECK_NAME      # advisory: never failure
        assert p["external_id"] == rep["analysis_id"] and p["details_url"] == "https://t/analyses/x"
        assert p["completed_at"].endswith("Z") and len(p["output"]["title"]) <= 255
        assert "never blocks a merge" in p["output"]["summary"] and MARKER in p["output"]["text"]
    assert "critical" in check_run_payload(breaking, "a" * 40)["output"]["title"]
    assert "not proof" in check_run_payload(clean, "a" * 40)["output"]["title"]                          # never a quiet 'clean'
    assert "failed" in check_run_payload(failed, "a" * 40)["output"]["title"].lower()
    assert "not a clean impact assessment" in check_run_payload(breaking, "a" * 40)["output"]["summary"]


def test_check_run_annotations_point_at_the_changed_lines(env):
    rep = analyse(env)
    notes = check_run_annotations(rep)
    assert notes and all(n["path"] and n["start_line"] == n["end_line"] > 0 for n in notes)
    level = {n["message"]: n["annotation_level"] for n in notes}
    assert level["PaymentStatus.status removed"] == "warning"                  # breaking -> warning
    assert level["PaymentStatus.state added (required)"] == "notice"           # not breaking for any endpoint request -> notice
    assert len(level) == 2
    assert check_run_payload(rep, "a" * 40)["output"]["annotations"] == notes
    many = {"change": {"changed_components": [{"type": "api_schema", "file": f"f{i}.py", "line": i + 1, "breaking": i % 2 == 0,
                                               "change": "schema_field_added", "name": "n", "detail": "d"} for i in range(80)]}}
    assert len(check_run_annotations(many)) == 50                                                          # GitHub's limit per request
    assert not check_run_annotations({"change": {"changed_components": [{"type": "function", "name": "f", "change": "modified"}]}})
    assert "annotations" not in check_run_payload(analyse(env, "impl"), "a" * 40)["output"]                 # no contract change, nothing to annotate


def test_data_changes_are_annotated_too():
    rep = {"analysis_id": "a", "status": "complete", "summary": {"findings": 0, "material_findings": 0, "by_severity": {}},
           "change": {"repository": "r", "changed_components": [{"type": "db_column", "name": "t.c", "change": "column_dropped", "breaking": True,
                                                                  "detail": "column t.c dropped", "file": "db/migration/V2__x.sql", "line": 3}]},
           "findings": [], "unknowns": [], "configuration_version": "c"}
    [n] = check_run_annotations(rep)
    assert n == {"path": "db/migration/V2__x.sql", "start_line": 3, "end_line": 3, "annotation_level": "warning",
                 "title": "TIA: column dropped", "message": "column t.c dropped"}


# ---- webhook worker publishes comment AND check run ------------------------------------------------------------------------------
def test_pull_request_event_publishes_comment_and_neutral_check_run_in_place(env):
    breaking, impl = env.fx["shas"]["breaking"], env.fx["shas"]["impl"]
    set_pr(env.origins, 21, breaking)
    job = env.post(env.event(21, breaking)).json()
    env.drain()
    done = env.job(job["job_id"])
    assert done["status"] == "succeeded" and done["result"]["comment"] == "created" and done["result"]["check_run"] == "created"
    [run] = env.gh.check_runs
    assert run["head_sha"] == breaking and run["conclusion"] == "neutral" and run["status"] == "completed"
    assert run["external_id"] == done["result"]["analysis_id"] and run["output"]["annotations"]
    # the check run text is the same audience-limited comment: downstream repositories are not named
    assert "reconciliation-service" not in json.dumps(run) and "withheld" in run["output"]["text"]

    again = env.post(env.event(21, breaking, action="synchronize")).json()                              # redelivery: same commit
    assert again["deduplicated"] is True
    set_pr(env.origins, 21, impl)
    new = env.post(env.event(21, impl, action="synchronize")).json()
    env.drain()
    assert env.job(new["job_id"])["result"]["check_run"] == "created"                                  # a new commit gets its own check
    assert [c["head_sha"] for c in env.gh.check_runs] == [breaking, impl] and len(env.gh.comments[21]) == 1
    events = [a["event"] for a in env.c.get("/api/v1/audit").json()]
    assert events.count("check_run_published") == 2 and events.count("pr_comment_published") == 2


def test_a_personal_access_token_cannot_create_check_runs_so_it_is_skipped_not_failed(env):
    env.gh.check_status = 403
    set_pr(env.origins, 22, env.fx["shas"]["breaking"])
    job = env.post(env.event(22, env.fx["shas"]["breaking"])).json()
    env.drain()
    j = env.job(job["job_id"])
    assert j["status"] == "succeeded" and j["result"]["comment"] == "created"                          # the comment is the contract
    skipped = j["result"]["check_run"]["skipped"]
    assert "HTTP 403" in skipped and "GitHub App" in skipped and TOKEN not in skipped
    assert env.gh.check_runs == [] and len(env.gh.comments[22]) == 1
    audit = env.c.get("/api/v1/audit").json()
    assert "check_run_skipped" in [a["event"] for a in audit] and TOKEN not in json.dumps(audit)


def test_check_run_can_be_switched_off(env):
    set_pr(env.origins, 23, env.fx["shas"]["breaking"])
    env.post(env.event(23, env.fx["shas"]["breaking"]))
    env.drain(replace(env.scm, check_run=False))
    assert env.gh.check_runs == [] and len(env.gh.comments[23]) == 1


def test_permalink_is_linked_from_the_comment_and_the_check_run(env):
    set_pr(env.origins, 24, env.fx["shas"]["breaking"])
    env.post(env.event(24, env.fx["shas"]["breaking"]))
    env.drain(replace(env.scm, public_url="https://tia.example.com"))
    aid = env.gh.check_runs[0]["external_id"]
    link = f"https://tia.example.com/analyses/{aid}"
    assert f"[Full report]({link})" in env.gh.comments[24][0]["body"]
    assert env.gh.check_runs[0]["details_url"] == link and link in env.gh.check_runs[0]["output"]["summary"]


# ---- POST /impact-analyses/{id}/publish ---------------------------------------------------------------------------------------------
def test_publish_endpoint_posts_comment_and_check_run_and_updates_in_place(env, monkeypatch):
    publishable(env, monkeypatch)
    rep = analyse(env)
    r = env.c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/publish", json={"repository": "payments-service", "pr": 31})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["comment"] == "created" and out["check_run"] == "created" and out["head_sha"] == rep["change"]["proposed_revision"]
    assert out["permalink"] is None and SHA_RE.match(out["head_sha"])
    assert MARKER in env.gh.comments[31][0]["body"] and env.gh.check_runs[0]["conclusion"] == "neutral"
    again = env.c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/publish", json={"repository": "payments-service", "pr": 31}).json()
    assert again["comment"] == "updated" and again["check_run"] == "updated"                           # in place, not duplicated
    assert len(env.gh.comments[31]) == 1 and len(env.gh.check_runs) == 1
    events = [a["event"] for a in env.c.get("/api/v1/audit").json()]
    assert events.count("pr_comment_published") == 2 and events.count("check_run_published") == 2
    assert TOKEN not in json.dumps(env.c.get("/api/v1/audit").json()) and env.c.get("/api/v1/audit/verify").json()["ok"] is True


def test_publish_endpoint_options_and_audience(env, monkeypatch):
    publishable(env, monkeypatch)
    rep = analyse(env)
    base = f"/api/v1/impact-analyses/{rep['analysis_id']}/publish"
    env.c.post(base, json={"repository": "payments-service", "pr": 32, "check_run": False})
    assert env.gh.check_runs == [] and len(env.gh.comments[32]) == 1
    env.c.post(base, json={"repository": "payments-service", "pr": 33, "comment": False})
    assert 33 not in env.gh.comments and len(env.gh.check_runs) == 1
    for other in ("reconciliation-service", "mobile-bff"):                                              # default audience: target only
        assert other not in env.gh.comments[32][0]["body"] and other not in json.dumps(env.gh.check_runs[0])
    full = TestClientFor(env, replace(env.scm, comment_audience="all"))
    assert full.post(base, json={"repository": "payments-service", "pr": 34}).status_code == 200
    assert "reconciliation-service" in env.gh.comments[34][0]["body"]


def test_publish_endpoint_guards(env, monkeypatch):
    rep = analyse(env)
    base = f"/api/v1/impact-analyses/{rep['analysis_id']}/publish"
    body = {"repository": "payments-service", "pr": 1}
    r = env.c.post(base, json=body)                                                                      # file:// remote: no GitHub identity
    assert r.status_code == 400 and "no GitHub remote" in r.json()["detail"]
    publishable(env, monkeypatch)
    assert env.c.post("/api/v1/impact-analyses/nope/publish", json=body).status_code == 404
    assert env.c.post(base, json={**body, "repository": "ghost"}).status_code == 404
    r = env.c.post(base, json={**body, "repository": "mobile-bff"})
    assert r.status_code == 400 and "is of 'payments-service'" in r.json()["detail"]
    assert env.c.post(base, json={**body, "head_sha": "main"}).status_code == 400
    assert env.c.post(base, json={"repository": "payments-service"}).status_code == 422
    assert env.gh.comments == {} and env.gh.check_runs == []                                             # nothing was published by a refused call


def test_publish_endpoint_without_token_or_with_a_rejected_token(env, monkeypatch):
    publishable(env, monkeypatch)
    rep = analyse(env)
    base = f"/api/v1/impact-analyses/{rep['analysis_id']}/publish"
    body = {"repository": "payments-service", "pr": 2}
    no_token = TestClientFor(env, replace(env.scm, github_token=None))
    r = no_token.post(base, json=body)
    assert r.status_code == 503 and "TIA_GITHUB_TOKEN" in r.json()["detail"]
    env.gh.fail_status = 403
    r = env.c.post(base, json=body)
    assert r.status_code == 502 and "HTTP 403" in r.json()["detail"] and TOKEN not in r.text
    env.gh.fail_status = 500
    r = env.c.post(base, json=body)
    assert r.status_code == 502 and "HTTP 500" in r.json()["detail"]


def test_a_failed_analysis_is_published_as_failed_never_as_clean(env):
    from tia.publish import publish_to_github
    rep = failed_report(env)
    assert rep["status"] == "failed"
    out = publish_to_github(rep, target_repo="payments-service", full_name="acme/payments-service", pr=3, head_sha="c" * 40, scm=env.scm)
    assert out["comment"] == "created" and out["check_run"] == "created"
    assert "No impact conclusions" in env.gh.comments[3][0]["body"] and env.gh.check_runs[0]["conclusion"] == "neutral"
    assert "failed" in env.gh.check_runs[0]["output"]["title"].lower()
