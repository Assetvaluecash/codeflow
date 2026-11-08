"""FR-039 / AC-036: GitLab merge requests and Bitbucket pull requests behave as GitHub pull requests - webhook in, analysis of the real
revision, one advisory comment updated in place, a commit status, and the permission mirror - against real git remotes and REST stand-ins."""
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakescm import FakeScmHost  # noqa: E402
from tia import scmhosts  # noqa: E402
from tia.access import AccessStore  # noqa: E402
from tia.analysis_service import make_handlers  # noqa: E402
from tia.api import create_app  # noqa: E402
from tia.jobs import JobQueue  # noqa: E402
from tia.report import MARKER  # noqa: E402
from tia.repos import detect_provider  # noqa: E402
from tia.store import Store  # noqa: E402
from tia.worker import Worker  # noqa: E402

SECRET = "hook-secret-123"


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def host():
    f = FakeScmHost()
    yield f
    f.close()


def gl_event(remote, head, action="open", iid=7, state="opened", oldrev="0" * 40):
    return {"object_kind": "merge_request", "project": {"id": 42, "path_with_namespace": "payments/payments-service", "git_http_url": remote},
            "object_attributes": {"iid": iid, "state": state, "action": action, "source_branch": "feature", "target_branch": "main",
                                  "last_commit": {"id": head}, "draft": False, **({"oldrev": oldrev} if action == "update" else {})}}


def bb_event(remote, head, key="pr:opened", pr=9):
    return {"pullRequest": {"id": pr, "state": "OPEN", "fromRef": {"displayId": "feature", "latestCommit": head},
                            "toRef": {"displayId": "main", "repository": {"slug": "payments-service", "project": {"key": "PAY"},
                                                                          "links": {"clone": [{"name": "http", "href": remote}]}}}}}


# ---- pure parts -------------------------------------------------------------------------------------------------------------------------
def test_providers_are_detected_and_events_parsed(monkeypatch):
    assert detect_provider("https://gitlab.com/acme/x.git") == "gitlab" and detect_provider("https://bitbucket.org/acme/x.git") == "bitbucket"
    monkeypatch.setenv("TIA_GITLAB_URL", "https://code.bank.internal")
    assert detect_provider("https://code.bank.internal/team/x.git") == "gitlab"
    gl = scmhosts.HostConfig("gitlab", "https://gitlab.com", SECRET)
    assert scmhosts.parse_event(gl, {}, gl_event("https://gitlab.com/p/x.git", "a" * 40))["pr"] == 7
    assert scmhosts.parse_event(gl, {}, gl_event("u", "a" * 40, action="close", state="closed")) is None
    assert scmhosts.parse_event(gl, {}, {**gl_event("u", "a" * 40, action="update"), "object_attributes": {
        **gl_event("u", "a" * 40, action="update")["object_attributes"], "oldrev": None}}) is None                # a title edit: no new commits
    assert scmhosts.parse_event(gl, {}, {"object_kind": "push"}) is None
    with pytest.raises(ValueError):
        scmhosts.parse_event(gl, {}, {"object_kind": "merge_request", "object_attributes": {"state": "opened", "action": "open", "iid": 1}})
    bb = scmhosts.HostConfig("bitbucket", "https://bitbucket.org", SECRET)
    cloud = {"pullrequest": {"id": 3, "state": "OPEN", "source": {"branch": {"name": "f"}, "commit": {"hash": "abc123def456"}},
                             "destination": {"branch": {"name": "main"}}}, "repository": {"full_name": "acme/payments"}}
    ev = scmhosts.parse_event(bb, {"x-event-key": "pullrequest:updated"}, cloud)
    assert (ev["pr"], ev["full_name"], ev["remote_url"]) == (3, "acme/payments", "https://bitbucket.org/acme/payments.git")
    assert scmhosts.full_name(gl, "https://gitlab.com/group/sub/project.git") == "group/sub/project"
    assert scmhosts.full_name(scmhosts.HostConfig("bitbucket", "https://bb.bank"), "https://bb.bank/scm/PAY/payments-service.git") == "PAY/payments-service"
    assert scmhosts.full_name(gl, "https://evil.example/group/project.git") is None


def test_webhook_authentication():
    gl = scmhosts.HostConfig("gitlab", "https://gitlab.com", SECRET)
    assert scmhosts.verify(gl, {"x-gitlab-token": SECRET}, b"{}") and not scmhosts.verify(gl, {"x-gitlab-token": "nope"}, b"{}")
    bb = scmhosts.HostConfig("bitbucket", "https://bitbucket.org", SECRET)
    raw = b'{"a": 1}'
    good = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert scmhosts.verify(bb, {"x-hub-signature": good}, raw) and not scmhosts.verify(bb, {"x-hub-signature": good}, raw + b" ")
    assert not scmhosts.verify(scmhosts.HostConfig("gitlab", "https://gitlab.com"), {"x-gitlab-token": ""}, b"{}")    # not configured


def test_tokens_go_only_to_the_configured_host(host):
    cfg = scmhosts.HostConfig("gitlab", "https://gitlab.com", token="t")
    with pytest.raises(scmhosts.ScmHostError, match="refusing"):
        scmhosts._call(cfg, "GET", "https://evil.example/api/v4/projects", "t")
    with pytest.raises(scmhosts.ScmHostError, match="refusing"):
        scmhosts._call(scmhosts.HostConfig("gitlab", "http://gitlab.bank.example"), "GET", "http://gitlab.bank.example/api/v4/x", "t")


def test_bitbucket_cloud_calls(monkeypatch):
    calls = []

    def fake_call(cfg, method, url, token, payload=None, operation="publish"):
        calls.append((method, url, payload))
        return {"values": []} if method == "GET" else {}
    monkeypatch.setattr(scmhosts, "_call", fake_call)
    cfg = scmhosts.HostConfig("bitbucket", "https://bitbucket.org", token="t")
    rep = {"analysis_id": "a1", "status": "complete", "summary": {"material_findings": 2}}
    scmhosts.upsert_comment(cfg, "t", "acme/payments", 3, MARKER + " body")
    scmhosts.post_status(cfg, "t", "acme/payments", "a" * 40, rep, "https://tia.bank/analyses/a1")
    assert [(m, u) for m, u, _ in calls] == [("GET", "https://api.bitbucket.org/2.0/repositories/acme/payments/pullrequests/3/comments?pagelen=100"),
                                            ("POST", "https://api.bitbucket.org/2.0/repositories/acme/payments/pullrequests/3/comments"),
                                            ("POST", f"https://api.bitbucket.org/2.0/repositories/acme/payments/commit/{'a' * 40}/statuses/build")]
    assert calls[2][2]["state"] == "SUCCESSFUL" and "Advisory" in calls[2][2]["description"]


def test_permission_mirrors(host):
    gl = scmhosts.HostConfig("gitlab", host.url, token=host.token)
    assert scmhosts.list_readers(gl, "payments/payments-service", host.token) == [("alice", "push"), ("rita", "pull")]   # no guest, no blocked
    bb = scmhosts.HostConfig("bitbucket", host.url, token=host.token)
    assert scmhosts.list_readers(bb, "PAY/payments-service", host.token) == [("alice", "push"), ("carol", "admin")]      # strongest wins


# ---- end to end through the webhook, the worker and the stand-in ----------------------------------------------------------------------
@pytest.fixture
def world(fx, host, tmp_path, monkeypatch):
    monkeypatch.setenv("TIA_GITLAB_URL", host.url)
    monkeypatch.setenv("TIA_GITLAB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TIA_GITLAB_TOKEN", host.token)
    monkeypatch.setenv("TIA_BITBUCKET_URL", host.url)
    monkeypatch.setenv("TIA_BITBUCKET_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TIA_BITBUCKET_TOKEN", host.token)
    w = type("W", (), {})()
    w.fx, w.host, w.tmp = fx, host, tmp_path
    origins = tmp_path / "origins"
    origins.mkdir()
    w.bare = origins / "payments-service.git"
    git(origins, "clone", "-q", "--bare", fx["paths"]["payments-service"], str(w.bare))
    git(w.bare, "update-ref", "refs/merge-requests/7/head", fx["shas"]["breaking"])
    git(w.bare, "update-ref", "refs/pull-requests/9/from", fx["shas"]["breaking"])
    w.tenant = conftest.new_tenant()
    w.c = TestClient(create_app(conftest.PG.db, w.tenant, str(tmp_path / "ws"), allow_local=True))
    for n, p in fx["paths"].items():
        if n != "payments-service":
            assert w.c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    assert w.c.post("/api/v1/repositories", json={"url": w.bare.as_uri(), "name": "payments-service"}).status_code == 200
    return w


def as_provider(w, provider):
    st = Store(conftest.PG.db, w.tenant)
    r = next(x for x in st.list_repos() if x.name == "payments-service")
    r.provider = provider
    st.put_repo(r)


def drain(w):
    h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(w.tmp / "ws"))
    wk = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
    while wk.run_once():
        pass
    with conftest.PG.db.tx(w.tenant) as c:
        return c.execute("select status, error, result from jobs where tenant_id = %s order by created_at desc", (w.tenant,)).fetchall()


def test_a_gitlab_merge_request_gets_one_note_and_a_status(world):
    as_provider(world, "gitlab")
    post = lambda ev, token=SECRET: world.c.post("/api/v1/webhooks/gitlab", content=json.dumps(ev).encode(),     # noqa: E731
                                                 headers={"X-Gitlab-Token": token, "Content-Type": "application/json"})
    assert post(gl_event(world.bare.as_uri(), world.fx["shas"]["breaking"]), token="wrong").status_code == 401
    r = post(gl_event(world.bare.as_uri(), world.fx["shas"]["breaking"]))
    assert r.status_code == 202 and r.json()["pr"] == 7
    (status, error, result), = drain(world)
    assert status == "succeeded", error
    assert result["comment"] == "created" and result["commit_status"] == "success"
    (note,) = world.host.notes[("42", 7)]
    assert MARKER in note["body"] and "payments-service" in note["body"]
    st = world.host.statuses[-1]
    assert st["name"] == "tia/impact-analysis" and st["state"] == "success" and st["sha"] == world.fx["shas"]["breaking"]
    # a new push: the same note is updated in place
    git(world.bare, "update-ref", "refs/merge-requests/7/head", world.fx["shas"]["impl"])
    post(gl_event(world.bare.as_uri(), world.fx["shas"]["impl"], action="update"))
    rows = drain(world)
    assert rows[0][0] == "succeeded" and rows[0][2]["comment"] == "updated"
    assert len(world.host.notes[("42", 7)]) == 1
    rep = world.c.get(f"/api/v1/impact-analyses/{rows[0][2]['analysis_id']}").json()
    assert rep["change"]["pull_request"] == 7 and rep["change"]["proposed_revision"] == world.fx["shas"]["impl"]


def test_a_bitbucket_pull_request_gets_one_comment_and_a_build_status(world):
    as_provider(world, "bitbucket")
    ev = bb_event(world.bare.as_uri(), world.fx["shas"]["breaking"])
    raw = json.dumps(ev).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    hdr = {"X-Event-Key": "pr:opened", "X-Hub-Signature": sig, "Content-Type": "application/json"}
    assert world.c.post("/api/v1/webhooks/bitbucket", content=raw, headers={**hdr, "X-Hub-Signature": "sha256=00"}).status_code == 401
    assert world.c.post("/api/v1/webhooks/bitbucket", content=raw, headers=hdr).status_code == 202
    (status, error, result), = drain(world)
    assert status == "succeeded", error
    (c,) = world.host.comments[("PAY", "payments-service", 9)]
    assert MARKER in c["text"] and world.host.statuses[-1]["state"] == "SUCCESSFUL" and world.host.statuses[-1]["key"] == "tia-impact-analysis"
    world.c.post("/api/v1/webhooks/bitbucket", content=raw, headers={**hdr, "X-Event-Key": "pr:from_ref_updated"})
    drain(world)
    assert len(world.host.comments[("PAY", "payments-service", 9)]) == 1 and world.host.comments[("PAY", "payments-service", 9)][0]["version"] >= 0


def test_the_access_mirror_reads_gitlab_members(world, monkeypatch):
    st = Store(conftest.PG.db, world.tenant)
    r = next(x for x in st.list_repos() if x.name == "payments-service")
    r.provider, r.url = "gitlab", world.host.url + "/payments/payments-service.git"
    st.put_repo(r)
    acc = AccessStore(conftest.PG.db, world.tenant)
    res = acc.sync([r], lambda repo: world.host.token)
    assert res == [{"repo": "payments-service", "ok": True, "collaborators": 2, "private": None}]
    assert acc.mirror()[("gitlab", "payments-service")]["logins"] == {"alice": "push", "rita": "pull"}
    bad = acc.sync([r], lambda repo: None)
    assert not bad[0]["ok"] and "no gitlab token" in bad[0]["error"]
