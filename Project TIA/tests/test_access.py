"""UC1-19: per-repository access mirrored from the source-code host (AC-016), through the real API with real analyses.

Three people (ada, bob, carol) sign in with OIDC tokens; the source-code host (a fake GitHub REST API) says who can read which repository.
"""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakegithub import AccessGitHub  # noqa: E402
from fakeidp import FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import bearer, ids_for, make_auth_app, oidc_cfg, tenant_named, token_for  # noqa: E402
from test_auth_api import audit  # noqa: E402
from tia.access import AccessStore, default_mode  # noqa: E402
from tia.publish import comment_scope  # noqa: E402
from tia.store import Store  # noqa: E402
from tia.webhooks import ScmConfig  # noqa: E402

TARGET = "payments-service"


@pytest.fixture(autouse=True)
def loopback(monkeypatch):
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_IDP_HOSTS", "true")


@pytest.fixture
def idp():
    p = FakeIdp()
    yield p
    p.close()


@pytest.fixture
def gh(fx):
    g = AccessGitHub()
    names = list(fx["paths"])
    g.private = {f"acme/{n}": True for n in names}
    g.collaborators = {
        "acme/payments-service": [("ada", "push"), ("bob", "pull")],
        "acme/reconciliation-service": [("ada", "admin")],
        "acme/mobile-bff": [("bob", "write"), ("ada", "read")],               # GitHub's own role names: write -> push, read -> pull
        "acme/ledger-sync": [("bob", "triage")],
        "acme/audit-reporting": [], "acme/notifications": [("ada", "pull"), ("bob", "pull")],
    }
    yield g
    g.close()


class World:
    pass


@pytest.fixture
def world(fx, idp, gh, tmp_path):
    w = World()
    w.fx, w.idp, w.gh = fx, idp, gh
    w.tid, w.org = tenant_named()
    ids_for(w.tid).put_idp("corp", "oidc", oidc_cfg(idp, scm_login_claims={"github": "gh"}))
    w.scm = ScmConfig(github_token="tok", github_api_url=gh.url, comment_audience="target", public_url="https://tia.example.com")
    w.app, _ = make_auth_app(tmp_path, scm=w.scm)
    w.c = TestClient(w.app)
    w.store = Store(conftest.PG.db, w.tid)
    w.all = bearer(token_for(w.tid, ["engineer"], name="everything"))                      # scope "*": the whole tenant
    w.rm = bearer(token_for(w.tid, ["release_manager"], name="mirror-sync"))
    w.admin = bearer(token_for(w.tid, ["administrator"], name="root", allow_administrator=True))
    for n, p in fx["paths"].items():
        assert w.c.post("/api/v1/repositories", headers=w.all, json={"path": p, "name": n}).status_code == 200
        r = w.store.get_repo(n)
        r.provider, r.url = "github", f"https://github.com/acme/{n}"                       # as if registered from GitHub
        w.store.put_repo(r)

    def as_user(login, groups=("tia-engineers",), **claims):
        sub = claims.pop("sub", "u-" + (login or "nogh"))
        return bearer(idp.mint(sub=sub, groups=list(groups), **({"gh": login} if login else {}), **claims))
    w.user = as_user
    w.sync = lambda: w.c.post("/api/v1/admin/repo-access/sync", headers=w.rm)
    w.names = lambda h: sorted(r["name"] for r in w.c.get("/api/v1/repositories", headers=h).json())
    return w


def finding_repos(rep):
    return sorted({f["repository"] for f in rep["findings"]})


# =====================================================================================================================================
class TestMirror:
    def test_nothing_is_visible_until_the_host_has_confirmed_it(self, world):
        assert world.names(world.user("ada")) == []                                  # hosted repositories default to 'scm': fail closed
        assert world.names(world.all) == sorted(world.fx["paths"])                   # a token scoped '*' is the operator's own view

    def test_after_the_first_sync_each_person_sees_what_the_host_says(self, world):
        r = world.sync()
        assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"])
        assert world.names(world.user("ada")) == ["mobile-bff", "notifications", "payments-service", "reconciliation-service"]
        assert world.names(world.user("bob")) == ["ledger-sync", "mobile-bff", "notifications", "payments-service"]
        assert world.names(world.user(None)) == []                                   # no source-host login on the profile: nothing
        assert world.names(world.user("mallory")) == []                              # a login the host does not list
        assert world.names(world.user("ADA", sub="u-ada-upper")) == world.names(world.user("ada"))   # logins compare case-insensitively

    def test_collaborator_pagination(self, world):
        world.gh.collaborators["acme/payments-service"] = [(f"user{i}", "pull") for i in range(250)] + [("ada", "push")]
        assert world.sync().status_code == 200
        assert TARGET in world.names(world.user("ada")) and TARGET in world.names(world.user("user249", sub="u-249"))

    def test_roles_that_do_not_grant_reading_are_ignored(self, world):
        world.gh.collaborators["acme/payments-service"] = [("ada", "none"), ("bob", "pull")]
        world.sync()
        assert TARGET not in world.names(world.user("ada")) and TARGET in world.names(world.user("bob"))

    def test_removing_someone_on_the_host_removes_access_after_the_next_sync(self, world):
        world.sync()
        bob = world.user("bob")
        assert TARGET in world.names(bob)
        world.gh.collaborators["acme/payments-service"] = [("ada", "push")]
        world.sync()
        assert TARGET not in world.names(bob)

    def test_a_stale_mirror_grants_nothing(self, world):
        world.sync()
        ada = world.user("ada")
        assert world.names(ada)
        with conftest.PG.db.tx(world.tid) as c:
            c.execute("update scm_permissions set synced_at = now() - interval '3 hours' where tenant_id = %s", (world.tid,))
        world.c.app.state.auth.ctx_for(world.tid).access.forget()
        assert world.names(ada) == []
        assert world.names(world.all) != []                                           # the operator's token is not subject to the mirror
        world.sync()
        assert world.names(ada) != []

    def test_one_failing_repository_does_not_stop_the_others_and_is_reported(self, world):
        world.gh.fail["acme/reconciliation-service"] = 403
        r = world.sync().json()["results"]
        bad = [x for x in r if not x["ok"]]
        assert [x["repo"] for x in bad] == ["reconciliation-service"] and "push access" in bad[0]["error"]
        assert "reconciliation-service" not in world.names(world.user("ada")) and "payments-service" in world.names(world.user("ada"))
        listing = world.c.get("/api/v1/admin/repo-access", headers=world.admin).json()
        row = next(x for x in listing["repositories"] if x["name"] == "reconciliation-service")
        assert row["mirror"]["ok"] is False and "403" in row["mirror"]["error"]
        assert audit(world.tid, "repo_access_synced")[0]["failed"] == ["reconciliation-service"]

    def test_a_failed_resync_keeps_the_old_rows_but_they_expire(self, world):
        world.sync()
        world.gh.fail["acme/payments-service"] = 500
        world.sync()
        assert TARGET in world.names(world.user("ada"))                              # still within the freshness limit
        with conftest.PG.db.tx(world.tid) as c:
            c.execute("update scm_permissions set synced_at = now() - interval '2 hours' where tenant_id = %s and repo = 'payments-service'", (world.tid,))
        world.c.app.state.auth.ctx_for(world.tid).access.forget()
        assert TARGET not in world.names(world.user("ada"))

    def test_sync_needs_a_token_and_the_right_role(self, world, tmp_path):
        assert world.c.post("/api/v1/admin/repo-access/sync", headers=world.user("ada")).status_code == 403              # engineer
        assert world.c.post("/api/v1/admin/repo-access/sync", headers=world.all).status_code == 403
        app2, _ = make_auth_app(tmp_path / "second", scm=ScmConfig(github_token=None))
        r = TestClient(app2).post("/api/v1/admin/repo-access/sync", headers=bearer(token_for(world.tid, ["release_manager"])))
        assert r.status_code == 503 and "token" in r.json()["detail"]

    def test_a_tenant_cannot_see_anothers_mirror(self, world):
        world.sync()
        other = conftest.new_tenant()
        for table in ("repo_access", "repo_grants", "scm_permissions", "scm_sync_status"):
            with conftest.PG.db.tx(other) as c:
                assert c.execute(f"select count(*) from {table} where tenant_id = %s", (world.tid,)).fetchone()[0] == 0
        with conftest.PG.db.plain() as c:
            assert c.execute("select count(*) from scm_permissions").fetchone()[0] == 0


# =====================================================================================================================================
class TestEnforcement:
    @pytest.fixture
    def synced(self, world):
        world.sync()
        return world

    def analysis(self, w, h=None):
        r = w.c.post("/api/v1/impact-analyses", headers=h or w.all, json={"target": TARGET, "head": "breaking"})
        assert r.status_code == 200, r.text
        return r.json()

    def test_a_report_shows_only_what_the_reader_may_see_and_says_how_much_was_left_out(self, synced):
        w = synced
        full = self.analysis(w)["report"]
        assert len(finding_repos(full)) >= 2
        for who in ("ada", "bob"):
            h = w.user(who)
            rep = self.analysis(w, h)["report"]
            allowed = set(w.names(h))
            assert set(finding_repos(rep)) <= allowed, who
            hidden = len(full["findings"]) - len(rep["findings"])
            assert rep["redacted"]["findings"] == hidden and (hidden > 0 or not rep["redacted"]["note"])
            text = str(rep)
            for other in set(w.fx["paths"]) - allowed:
                assert other not in text, (who, other)                                   # not named anywhere in what they are given
        assert finding_repos(self.analysis(w, w.user("ada"))["report"]) != finding_repos(self.analysis(w, w.user("bob"))["report"])

    def test_someone_who_cannot_read_the_target_gets_404_everywhere(self, synced):
        w = synced
        aid = self.analysis(w)["analysis_id"]
        carol = w.user("carol-no-access")
        assert w.c.post("/api/v1/impact-analyses", headers=carol, json={"target": TARGET, "head": "breaking"}).status_code == 404
        job = w.c.post("/api/v1/impact-analyses?async=true", headers=w.user("ada"), json={"target": TARGET, "head": "impl"}).json()
        for method, path in (("get", f"/api/v1/impact-analyses/{aid}"), ("get", f"/api/v1/impact-analyses/{aid}/evidence"),
                             ("get", f"/api/v1/impact-analyses/{aid}/comment"), ("get", f"/api/v1/impact-analyses/{aid}/graph"),
                             ("get", f"/api/v1/impact-analyses/{aid}/paths"), ("get", f"/api/v1/impact-analyses/{aid}/unknowns"),
                             ("get", f"/analyses/{aid}"), ("post", f"/api/v1/impact-analyses/{aid}/reproduce"),
                             ("get", f"/api/v1/jobs/{job['job_id']}"), ("delete", f"/api/v1/jobs/{job['job_id']}"),
                             ("get", f"/api/v1/repositories/{TARGET}/revisions"), ("post", f"/api/v1/repositories/{TARGET}/sync")):
            r = getattr(w.c, method)(path, headers={**carol, "Accept": "application/json"})
            assert r.status_code == 404, (method, path, r.status_code)
        assert w.c.post(f"/api/v1/impact-analyses/{aid}/publish", headers=carol, json={"repository": TARGET, "pr": 1}).status_code == 404
        # being an administrator does not open a repository: deleting needs the role AND access to the repository
        boss = w.user("boss-no-access", groups=["tia-admins"])
        assert w.c.delete(f"/api/v1/repositories/{TARGET}", headers=carol).status_code == 403
        assert w.c.delete(f"/api/v1/repositories/{TARGET}", headers=boss).status_code == 404
        assert TARGET in w.names(w.all)

    def test_the_other_repositories_are_not_even_confirmed_to_exist(self, synced):
        w = synced
        bob = w.user("bob")
        r = w.c.get("/api/v1/repositories/reconciliation-service/revisions", headers=bob)
        r2 = w.c.get("/api/v1/repositories/does-not-exist/revisions", headers=bob)
        assert r.status_code == r2.status_code == 404 and r.json()["detail"].replace("reconciliation-service", "X") == r2.json()["detail"].replace("does-not-exist", "X")

    def test_the_graph_and_the_statistics_are_restricted_too(self, synced):
        w = synced
        bob = w.user("bob")
        allowed = set(w.names(bob))
        stats = w.c.get("/api/v1/graph/stats", headers=bob).json()
        assert {r["repo"] for r in stats} <= allowed
        g = w.c.get("/api/v1/graph?level=service", headers=bob).json()
        shown = {n.get("repo") or n.get("name") for n in g["nodes"]}
        assert not (shown & (set(w.fx["paths"]) - allowed))
        rel = w.c.get("/api/v1/graph/relationships", headers=bob).json()["relationships"]
        assert all(x["consumer_repo"] in allowed and x["provider_repo"] in allowed for x in rel)
        aid = self.analysis(w)["analysis_id"]
        ov = w.c.get(f"/api/v1/impact-analyses/{aid}/graph", headers=bob).json()
        assert not (str(ov) and any(n in str(ov) for n in set(w.fx["paths"]) - allowed))

    def test_the_header_can_only_narrow_never_widen(self, synced):
        w = synced
        ada = w.user("ada")
        rep = w.c.post("/api/v1/impact-analyses", headers={**ada, "X-TIA-Readable-Repos": "payments-service,ledger-sync"},
                       json={"target": TARGET, "head": "breaking"}).json()["report"]
        assert set(finding_repos(rep)) <= {"payments-service"}                           # ledger-sync is bob's, not hers; the header cannot add it
        full = w.c.post("/api/v1/impact-analyses", headers={**w.all, "X-TIA-Readable-Repos": "payments-service"},
                        json={"target": TARGET, "head": "breaking"}).json()["report"]
        assert set(finding_repos(full)) <= {"payments-service"}                          # and it narrows even the operator's token

    def test_uploads_to_an_existing_repository_need_access(self, synced):
        w = synced
        import io, zipfile
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("a.py", "x = 1\n")
        assert w.c.put(f"/api/v1/repositories/{TARGET}/upload", headers=w.user("carol-no-access"), content=b.getvalue()).status_code == 404
        r = w.c.post("/api/v1/uploads", headers=w.user("carol-no-access"), json={"name": TARGET})
        assert r.status_code == 404

    def test_service_tokens_are_scoped_to_their_repositories(self, synced):
        w = synced
        h = bearer(token_for(w.tid, ["engineer"], repos=[TARGET, "mobile-bff"]))
        assert w.names(h) == ["mobile-bff", "payments-service"]
        assert w.c.post("/api/v1/impact-analyses", headers=h, json={"target": "ledger-sync", "head": "main"}).status_code == 404
        r = w.c.post("/api/v1/impact-analyses", headers=h, json={"target": TARGET, "head": "breaking"})
        assert r.status_code == 200, r.text
        assert set(finding_repos(r.json()["report"])) <= {TARGET, "mobile-bff"}

    def test_every_decision_is_made_for_the_tenant_of_the_credential(self, synced):
        w = synced
        other = conftest.new_tenant()
        h = bearer(token_for(other, ["engineer"]))
        assert w.c.get("/api/v1/repositories", headers=h).json() == []
        r = w.c.post("/api/v1/impact-analyses", headers=h, json={"target": TARGET, "head": "breaking"})
        assert r.status_code in (400, 404) and "payments" not in r.text.replace(TARGET + "' is not", "")      # nothing of the other tenant leaks


# =====================================================================================================================================
class TestGoverning:
    def test_default_modes(self):
        assert default_mode("github") == "scm" and default_mode("local") == "tenant" and default_mode("upload") == "tenant" and default_mode("git") == "restricted" and default_mode("ado") == "restricted"

    def test_tenant_mode_opens_a_repository_to_every_member(self, world):
        world.sync()
        carol = world.user("carol-no-access")
        assert "notifications" not in world.names(carol)
        r = world.c.put("/api/v1/admin/repo-access/notifications", headers=world.admin, json={"mode": "tenant"})
        assert r.status_code == 200 and r.json()["mode"] == "tenant"
        assert "notifications" in world.names(carol)                                      # effective at once
        assert audit(world.tid, "repo_access_changed")[0]["repo"] == "notifications"

    def test_restricted_mode_with_grants(self, world):
        world.sync()
        ada, bob = world.user("ada"), world.user("bob")
        world.c.get("/api/v1/me", headers=ada)
        world.c.get("/api/v1/me", headers=bob)
        users = {u["subject"]: u["id"] for u in ids_for(world.tid).list_users()}
        r = world.c.put("/api/v1/admin/repo-access/payments-service", headers=world.admin, json={"mode": "restricted", "grants": [users["u-bob"]]})
        assert r.status_code == 200 and r.json()["grants"] == [users["u-bob"]]
        assert TARGET in world.names(bob) and TARGET not in world.names(ada)              # the host's opinion no longer matters
        world.c.put("/api/v1/admin/repo-access/payments-service", headers=world.admin, json={"grants": []})
        assert TARGET not in world.names(bob)

    @pytest.mark.parametrize("body,code", [({"mode": "everyone"}, 422), ({}, 422), ({"grants": [str(uuid.uuid4())]}, 422)])
    def test_invalid_changes(self, world, body, code):
        assert world.c.put("/api/v1/admin/repo-access/payments-service", headers=world.admin, json=body).status_code == code

    def test_unknown_repository_and_non_administrators(self, world):
        assert world.c.put("/api/v1/admin/repo-access/nope", headers=world.admin, json={"mode": "tenant"}).status_code == 404
        for h in (world.user("ada", groups=["tia-engineers"]), world.rm, world.all):
            assert world.c.put("/api/v1/admin/repo-access/notifications", headers=h, json={"mode": "tenant"}).status_code == 403
            assert world.c.get("/api/v1/admin/repo-access", headers=h).status_code == 403

    def test_listing(self, world):
        world.sync()
        world.c.put("/api/v1/admin/repo-access/notifications", headers=world.admin, json={"mode": "tenant"})
        j = world.c.get("/api/v1/admin/repo-access", headers=world.admin).json()
        assert j["freshness_limit_seconds"] >= 60
        rows = {r["name"]: r for r in j["repositories"]}
        assert rows["notifications"]["mode"] == "tenant" and rows["notifications"]["explicit"] is True
        assert rows[TARGET]["mode"] == "scm" and rows[TARGET]["explicit"] is False and rows[TARGET]["mirror"]["collaborators"] == 2
        assert rows[TARGET]["mirror"]["private"] is True and rows[TARGET]["mirror"]["fresh"] is True


# =====================================================================================================================================
class TestPullRequestComment:
    """The comment on a pull request is visible to everyone who can see that pull request on the host (AC-016)."""

    def repos(self, world):
        return world.store.list_repos()

    def test_another_repository_is_named_only_if_all_the_readers_of_the_pull_request_may_read_it(self, world):
        world.sync()
        acc, repos = AccessStore(conftest.PG.db, world.tid), self.repos(world)
        # readers of payments-service: ada, bob. mobile-bff: bob, ada (both). reconciliation: ada only. ledger-sync: bob only.
        assert acc.comment_repos(TARGET, repos) == [TARGET, "mobile-bff", "notifications"]

    def test_a_public_target_names_nothing_else(self, world):
        world.gh.private["acme/payments-service"] = False
        world.sync()
        assert AccessStore(conftest.PG.db, world.tid).comment_repos(TARGET, self.repos(world)) == [TARGET]

    def test_an_unknown_visibility_is_treated_as_public(self, world):
        del world.gh.private["acme/payments-service"]                                    # the host's metadata call answers 404
        world.sync()
        assert AccessStore(conftest.PG.db, world.tid).comment_repos(TARGET, self.repos(world)) == [TARGET]

    def test_without_a_fresh_mirror_only_the_target(self, world):
        acc = AccessStore(conftest.PG.db, world.tid)
        assert acc.comment_repos(TARGET, self.repos(world)) == [TARGET]
        world.sync()
        with conftest.PG.db.tx(world.tid) as c:
            c.execute("update scm_permissions set synced_at = now() - interval '5 hours' where tenant_id = %s", (world.tid,))
        assert acc.comment_repos(TARGET, self.repos(world)) == [TARGET]

    def test_tenant_wide_or_restricted_repositories_cannot_be_proved_and_are_left_out(self, world):
        world.sync()
        world.c.put("/api/v1/admin/repo-access/mobile-bff", headers=world.admin, json={"mode": "tenant"})
        assert "mobile-bff" not in AccessStore(conftest.PG.db, world.tid).comment_repos(TARGET, self.repos(world))

    def test_a_target_with_no_readers_names_nothing_else(self, world):
        world.gh.collaborators["acme/payments-service"] = []
        world.sync()
        assert AccessStore(conftest.PG.db, world.tid).comment_repos(TARGET, self.repos(world)) == [TARGET]

    def test_audience_selection(self, world):
        world.sync()
        acc, svc = AccessStore(conftest.PG.db, world.tid), type("S", (), {"list": lambda s: self.repos(world)})()
        mk = lambda a: ScmConfig(comment_audience=a)
        assert comment_scope(acc, svc, mk("target"), TARGET, True) is None
        assert comment_scope(acc, svc, mk("all"), TARGET, False) is None                  # legacy single-team setting, no access control
        assert comment_scope(acc, svc, mk("all"), TARGET, True) == [TARGET, "mobile-bff", "notifications"]   # with authentication: "all" means "readers"
        assert comment_scope(acc, svc, mk("readers"), TARGET, False) == [TARGET, "mobile-bff", "notifications"]

    def _publish(self, w, audience):
        aid = w.c.post("/api/v1/impact-analyses", headers=w.all, json={"target": TARGET, "head": "breaking"}).json()["analysis_id"]
        w.c.put("/api/v1/tenant/config", headers=w.rm, json={"config": {"comments": {"audience": audience}}})
        sha = w.store.get_analysis(aid)["change"]["proposed_revision"]
        r = w.c.post(f"/api/v1/impact-analyses/{aid}/publish", headers=w.all, json={"repository": TARGET, "pr": 7, "head_sha": sha, "check_run": False})
        assert r.status_code == 200, r.text
        return "\n".join(w.gh.comment_bodies("acme/payments-service", 7))

    def test_the_published_comment_follows_the_rule(self, world):
        world.sync()
        full = world.store.get_analysis(world.c.post("/api/v1/impact-analyses", headers=world.all, json={"target": TARGET, "head": "breaking"}).json()["analysis_id"])
        repos_in_full = {f["repository"] for f in full["findings"]}
        assert len(repos_in_full) > 1                                                     # the analysis really spans several repositories
        target_only = self._publish(world, "target")
        for other in set(world.fx["paths"]) - {TARGET}:
            assert other not in target_only, other
        world.gh.comments.clear()
        readers = self._publish(world, "readers")
        provable = {"mobile-bff", "notifications"}
        for other in set(world.fx["paths"]) - {TARGET} - provable:
            assert other not in readers, other                                           # ada-only and bob-only repositories never appear
        world.gh.comments.clear()
        as_all = self._publish(world, "all")
        for other in set(world.fx["paths"]) - {TARGET} - provable:
            assert other not in as_all, other                                            # "all" cannot override access control once it is on
