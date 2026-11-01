"""UC1-04: the GitHub App connector - App JWT, installation tokens, tenant routing of webhooks, linking installations, permission failures.

A GitHub platform stand-in (tests/fakeghapp.py) verifies the App's JWT with the App's real public key and only accepts check runs from
installation tokens, as GitHub does. Real PostgreSQL, real git remotes, the real app and worker handlers.
"""
import dataclasses
import json
import subprocess
import sys
import urllib.error
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakeghapp import FakeApp, new_key  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import bearer, make_auth_app, tenant_named, token_for  # noqa: E402
from test_auth_api import audit  # noqa: E402
from test_webhooks import SECRET, sign  # noqa: E402
from tia import githubapp, jwtverify  # noqa: E402
from tia.analysis_service import make_handlers  # noqa: E402
from tia.githubapp import AppError, GitHubApp, TokenSource, installation_tenant, my_installations  # noqa: E402
from tia.jobs import JobQueue  # noqa: E402
from tia.store import Store  # noqa: E402
from tia.tenants import Tenants  # noqa: E402
from tia.webhooks import ScmConfig  # noqa: E402
from tia.worker import Worker  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_installations():
    """Installation ids are global across tenants; these tests use small literals, so start each one with none of them mapped."""
    import psycopg
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("delete from tenant_installations where provider = 'github' and installation_id ~ '^[0-9]{1,3}$'")
    yield


@pytest.fixture
def gh():
    g = FakeApp()
    yield g
    g.close()


def scm_for(gh, **kw):
    return ScmConfig(github_app_id=gh.app_id, github_app_key=gh.pem, github_api_url=gh.url, github_web_url=gh.url, github_app_client_id=gh.client_id,
                     github_app_client_secret=gh.client_secret, github_webhook_secret=SECRET, public_url="https://tia.example.com", **kw)


def app_obj(gh, clock=None):
    kw = {"clock": clock} if clock else {}
    return GitHubApp(gh.app_id, gh.pem, gh.url, gh.url, gh.client_id, gh.client_secret, **kw)


def map_to(tenant_ref, iid, account="acme"):
    Tenants(conftest.PG.admin).map_installation(tenant_ref, "github", str(iid), account)


# =====================================================================================================================================
class TestApp:
    def test_the_jwt_is_a_short_lived_rs256_token_for_the_app(self, gh):
        t = [10_000.0]
        a = app_obj(gh, lambda: t[0])
        header, claims, si, sig = jwtverify.split(a.jwt())
        assert header == {"alg": "RS256", "typ": "JWT"} and claims["iss"] == gh.app_id
        assert claims["exp"] - claims["iat"] == 600 and claims["iat"] == 10_000 - 60               # GitHub allows at most ten minutes
        jwtverify.verify_signature(header, si, sig, {**gh.jwk, "use": "sig"})                      # verifies with the App's public key

    @pytest.mark.parametrize("pem,why", [("not a key", "usable"), ("", "usable")])
    def test_a_bad_private_key_is_reported_at_start(self, pem, why):
        with pytest.raises(AppError, match=why):
            GitHubApp("1", pem)

    def test_weak_and_non_rsa_keys_are_refused(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        for k in (rsa.generate_private_key(65537, 1024), ec.generate_private_key(ec.SECP256R1())):
            pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            with pytest.raises(AppError) as e:
                GitHubApp("1", pem)
            assert e.value.code == "bad_key"

    def test_the_wrong_key_is_rejected_by_github(self, gh):
        other_pem, _ = new_key()
        with pytest.raises(AppError) as e:
            GitHubApp(gh.app_id, other_pem, gh.url).installation(1)
        assert e.value.code == "not_configured" and "App credentials" in str(e.value)

    def test_installation_tokens_are_cached_and_refreshed_before_they_expire(self, gh):
        gh.install(7, "acme", ["pay"])
        import time
        t = [time.time()]
        gh.clock = lambda: t[0]                                                                    # GitHub's clock moves with ours
        a = app_obj(gh, lambda: t[0])
        first = a.installation_token(7)
        assert a.installation_token(7) == first and gh.token_calls == 1
        t[0] += 3400                                                                               # inside the five-minute refresh window
        second = a.installation_token(7)
        assert second != first and gh.token_calls == 2
        a.forget(7)
        a.installation_token(7)
        assert gh.token_calls == 3

    @pytest.mark.parametrize("iid,code,text", [(404, "not_installed", "no such installation"), ])
    def test_unknown_installation(self, gh, iid, code, text):
        with pytest.raises(AppError) as e:
            app_obj(gh).installation(iid)
        assert e.value.code == code and text in str(e.value).lower()

    def test_a_removed_installation_says_so(self, gh):
        gh.install(7, "acme", ["pay"])
        a = app_obj(gh)
        a.installation_token(7)
        gh.installations[7]["gone"] = True
        a.forget()
        with pytest.raises(AppError) as e:
            a.installation_token(7)
        assert e.value.code == "installation_gone" and "no longer exists" in str(e.value)

    def test_a_suspended_installation_says_so(self, gh):
        gh.install(7, "acme", ["pay"])
        gh.installations[7]["suspended"] = True
        with pytest.raises(AppError) as e:
            app_obj(gh).installation(7)
        assert e.value.code == "installation_gone" and "suspended" in str(e.value)

    def test_github_unreachable(self, gh):
        a = app_obj(gh)
        gh.close()
        with pytest.raises(AppError) as e:
            a.installation(7)
        assert e.value.code == "unreachable"

    def test_the_installation_of_a_repository(self, gh):
        gh.install(7, "acme", ["pay"])
        a = app_obj(gh)
        assert a.installation_for_repo("acme/pay") == "7"
        with pytest.raises(AppError) as e:
            a.installation_for_repo("acme/unknown")
        assert e.value.code == "not_installed"

    def test_repository_listing_is_paginated(self, gh):
        gh.install(7, "acme", [f"r{i:03d}" for i in range(230)])
        repos = app_obj(gh).repositories(7)
        assert len(repos) == 230 and repos[0]["clone_url"] == "https://github.com/acme/r000.git" and repos[0]["private"] is True

    def test_a_user_authorisation_proves_visibility_of_an_installation(self, gh):
        gh.install(7, "acme", ["pay"])
        gh.install(8, "other", ["x"])
        gh.users = {"code-alice": [7], "code-bob": [8]}
        a = app_obj(gh)
        assert a.user_sees_installation("code-alice", 7, "acme") is True
        assert a.user_sees_installation("code-alice", 8, "other") is False                                  # knowing an id is not enough
        assert a.user_sees_installation("garbage", 7) is False
        with pytest.raises(AppError, match="CLIENT_ID"):
            GitHubApp(gh.app_id, gh.pem, gh.url, gh.url).user_sees_installation("code-alice", 7)

    def test_scm_config_builds_an_app_only_when_fully_configured(self, gh):
        assert githubapp.from_scm(ScmConfig()) is None
        assert githubapp.from_scm(ScmConfig(github_app_id="1")) is None
        assert githubapp.from_scm(scm_for(gh)).app_id == gh.app_id


# =====================================================================================================================================
class TestInstallationMapping:
    def test_routing_only_for_active_owned_installations(self, gh):
        tid, name = tenant_named()
        map_to(name, 101)
        assert installation_tenant(conftest.PG.db, 101) == tid
        assert installation_tenant(conftest.PG.db, 102) is None and installation_tenant(conftest.PG.db, "x' or '1'='1") is None
        Tenants(conftest.PG.admin).suspend(name, "test")
        assert installation_tenant(conftest.PG.db, 101) is None                                    # a suspended tenant receives nothing

    def test_claiming_is_first_come_and_never_reveals_the_owner(self, gh):
        (a, an), (b, bn) = tenant_named(), tenant_named()
        with conftest.PG.db.tx(a) as c:
            assert c.execute("select tia_map_installation('github', '201', 'acme')").fetchone()[0] == "mapped"
            assert c.execute("select tia_map_installation('github', '201', 'acme')").fetchone()[0] == "mapped"      # idempotent for the owner
        with conftest.PG.db.tx(b) as c:
            assert c.execute("select tia_map_installation('github', '201', 'acme')").fetchone()[0] == "taken"
        assert installation_tenant(conftest.PG.db, 201) == a
        assert my_installations(conftest.PG.db, b) == [] and my_installations(conftest.PG.db, a) == [{"installation_id": "201", "account": "acme"}]

    def test_a_tenant_can_release_only_its_own(self, gh):
        (a, an), (b, bn) = tenant_named(), tenant_named()
        map_to(an, 301)
        with conftest.PG.db.tx(b) as c:
            assert c.execute("select tia_unmap_installation('github', '301')").fetchone()[0] is False
        assert installation_tenant(conftest.PG.db, 301) == a
        with conftest.PG.db.tx(a) as c:
            assert c.execute("select tia_unmap_installation('github', '301')").fetchone()[0] is True
        assert installation_tenant(conftest.PG.db, 301) is None

    def test_no_tenant_no_mapping(self):
        with conftest.PG.db.plain() as c:
            assert c.execute("select tia_map_installation('github', '401', 'x')").fetchone()[0] == "no_tenant"
            assert c.execute("select count(*) from tia_my_installations('github')").fetchone()[0] == 0

    def test_the_table_itself_is_not_readable_by_the_runtime_role(self, gh):
        tid, name = tenant_named()
        map_to(name, 501)
        import psycopg
        with conftest.PG.db.tx(tid) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select * from tenant_installations")

    def test_mapping_and_unmapping_are_recorded_as_lifecycle_events(self, gh):
        tid, name = tenant_named()
        with conftest.PG.db.tx(tid) as c:
            c.execute("select tia_map_installation('github', '601', 'acme')")
            c.execute("select tia_unmap_installation('github', '601')")
        ev = [e["event"] for e in Tenants(conftest.PG.admin).events(name)]
        assert "installation_mapped" in ev and "installation_unmapped" in ev


# =====================================================================================================================================
class TestTokenSource:
    def test_without_an_app_the_interim_token_is_used(self):
        ts = TokenSource(ScmConfig(github_token="pat"), conftest.PG.db)
        assert not ts.uses_app and ts.token_for(conftest.new_tenant(), full_name="a/b") == "pat" and ts.token_for("x", fallback="stored") == "stored"

    def test_an_installation_token_for_a_repository_of_the_tenant(self, gh):
        tid, name = tenant_named()
        gh.install(7, "acme", ["pay"])
        map_to(name, 7)
        ts = TokenSource(scm_for(gh), conftest.PG.db)
        t = ts.token_for(tid, repo_url="https://github.com/acme/pay.git")
        assert t.startswith("ghs_") and gh.tokens[t] == 7
        assert ts.token_for(tid, full_name="acme/pay", hint=7) == t                               # the hint avoids the lookup
        assert ts.token_for(tid, full_name="acme/pay", hint=999) == t                             # a hint that is not the tenant's is ignored

    def test_a_tenant_without_an_installation(self, gh):
        with pytest.raises(AppError, match="no GitHub App installation is linked"):
            TokenSource(scm_for(gh), conftest.PG.db).token_for(conftest.new_tenant(), full_name="acme/pay")

    def test_a_repository_of_another_tenants_installation_is_refused(self, gh):
        (a, an), (b, bn) = tenant_named(), tenant_named()
        gh.install(7, "acme", ["pay"])
        gh.install(8, "other", ["x"])
        map_to(an, 7)
        map_to(bn, 8)
        ts = TokenSource(scm_for(gh), conftest.PG.db)
        with pytest.raises(AppError, match="not installed on acme/pay for this organisation"):
            ts.token_for(b, full_name="acme/pay")                                                 # tenant B cannot act on tenant A's repository
        with pytest.raises(AppError, match="not installed on acme/pay for this organisation"):
            ts.token_for(b, full_name="acme/pay", hint=7)

    def test_a_repository_the_app_is_not_installed_on(self, gh):
        tid, name = tenant_named()
        gh.install(7, "acme", ["pay"])
        map_to(name, 7)
        with pytest.raises(AppError, match="not installed on acme/other"):
            TokenSource(scm_for(gh), conftest.PG.db).token_for(tid, full_name="acme/other")


# =====================================================================================================================================
class World:
    pass


@pytest.fixture
def world(fx, gh, tmp_path):
    """An authenticated deployment with the App configured, two organisations with one installation each."""
    w = World()
    w.fx, w.gh, w.tmp = fx, gh, tmp_path
    w.scm = scm_for(gh)
    w.a, w.an = tenant_named()
    w.b, w.bn = tenant_named()
    gh.install(7, "acme", [n for n in fx["paths"]])
    gh.install(8, "other", ["theirs"])
    map_to(w.an, 7)
    map_to(w.bn, 8)
    w.app, w.default = make_auth_app(tmp_path, scm=w.scm, gate_ttl=0)
    w.c = TestClient(w.app)
    w.ha = bearer(token_for(w.a, ["engineer"], name="eng"))
    w.hadmin = bearer(token_for(w.a, ["administrator"], name="root", allow_administrator=True))
    w.hb = bearer(token_for(w.b, ["engineer"], name="eng"))
    w.hbadmin = bearer(token_for(w.b, ["administrator"], name="root", allow_administrator=True))
    return w


def register(w, tenant, headers, name="payments-service"):
    """The repository, registered from its local path and presented as the GitHub repository it would be."""
    assert w.c.post("/api/v1/repositories", headers=headers, json={"path": w.fx["paths"][name], "name": name}).status_code == 200
    st = Store(conftest.PG.db, tenant)
    r = st.get_repo(name)
    r.provider, r.url = "github", f"https://github.com/acme/{name}.git"
    st.put_repo(r)


def event(pr=5, sha="a" * 40, installation=7, full="acme/payments-service", url="https://github.com/acme/payments-service.git", action="opened"):
    p = {"action": action, "number": pr, "pull_request": {"number": pr, "draft": False, "head": {"sha": sha}, "base": {"ref": "main"}},
         "repository": {"full_name": full, "clone_url": url}}
    if installation is not None:
        p["installation"] = {"id": installation}
    return p


def deliver(w, payload, secret=SECRET, ev="pull_request"):
    body = json.dumps(payload).encode()
    return w.c.post("/api/v1/webhooks/github", content=body, headers={"X-GitHub-Event": ev, "X-GitHub-Delivery": uuid.uuid4().hex,
                                                                      "X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"})


def queued(tenant):
    with conftest.PG.db.tx(tenant) as c:
        return [(r[0], r[1]) for r in c.execute("select kind, payload from jobs where tenant_id = %s", (tenant,))]


class TestWebhookRouting:
    def test_an_event_is_processed_for_the_tenant_that_owns_the_installation(self, world):
        w = world
        register(w, w.a, w.ha)
        r = deliver(w, event(installation=7))
        assert r.status_code == 202 and r.json().get("job_id"), r.text
        jobs = queued(w.a)
        assert len(jobs) == 1 and jobs[0][0] == "github_pull_request" and jobs[0][1]["installation_id"] == 7 and jobs[0][1]["repo"] == "payments-service"
        assert queued(w.b) == [] and queued(w.default) == []
        assert any(e["event"] == "webhook_accepted" for e in audit(w.a)) and not audit(w.b, "webhook_accepted")

    def test_another_organisations_repository_with_my_installation_id_is_not_found(self, world):
        """B's installation id, but the repository is registered only by A: B has no such repository, so nothing is queued anywhere."""
        w = world
        register(w, w.a, w.ha)
        r = deliver(w, event(installation=8))
        assert r.status_code == 202 and "not registered" in r.json()["ignored"]
        assert queued(w.a) == [] and queued(w.b) == []

    def test_unlinked_missing_and_suspended(self, world):
        w = world
        register(w, w.a, w.ha)
        for payload in (event(installation=999), event(installation=None)):
            r = deliver(w, payload)
            assert r.status_code == 202 and "not linked" in r.json()["ignored"]
        Tenants(conftest.PG.admin).suspend(w.an, "test")
        r = deliver(w, event(installation=7))
        assert r.status_code == 202 and "not linked" in r.json()["ignored"]
        assert queued(w.a) == []

    def test_signature_is_still_required(self, world):
        w = world
        register(w, w.a, w.ha)
        assert deliver(w, event(), secret=b"wrong").status_code == 401
        assert queued(w.a) == []

    def test_a_redelivery_is_deduplicated_per_tenant(self, world):
        w = world
        register(w, w.a, w.ha)
        j1, j2 = deliver(w, event()).json(), deliver(w, event()).json()
        assert j2["deduplicated"] is True and j1["job_id"] == j2["job_id"]

    def test_the_same_event_shape_reaches_the_other_tenant_when_it_has_the_repository(self, world):
        w = world
        register(w, w.a, w.ha)
        w.gh.install(9, "acme", ["payments-service"])                                              # a second installation, owned by B
        map_to(w.bn, 9)
        assert w.c.post("/api/v1/repositories", headers=w.hb, json={"path": w.fx["paths"]["payments-service"], "name": "payments-service"}).status_code == 200
        st = Store(conftest.PG.db, w.b)
        r = st.get_repo("payments-service")
        r.provider, r.url = "github", "https://github.com/acme/payments-service.git"
        st.put_repo(r)
        assert deliver(w, event(installation=9)).status_code == 202
        assert len(queued(w.b)) == 1 and queued(w.a) == []


# =====================================================================================================================================
class TestLinking:
    def test_the_administrator_links_an_installation_with_proof(self, world):
        w = world
        w.gh.install(21, "newco", ["x"])
        w.gh.users = {"code-ok": [21], "code-other": [8]}
        r = w.c.post("/api/v1/admin/github/installations", headers=w.hadmin, json={"installation_id": 21, "code": "code-ok"})
        assert r.status_code == 200 and r.json() == {"installation_id": "21", "account": "newco"}
        assert installation_tenant(conftest.PG.db, 21) == w.a
        assert audit(w.a, "installation_linked")[0]["installation_id"] == "21"
        listing = w.c.get("/api/v1/admin/github/installations", headers=w.hadmin).json()
        assert listing["app_configured"] and listing["linking_available"] and {i["installation_id"] for i in listing["installations"]} == {"7", "21"}

    def test_knowing_an_installation_id_is_not_enough(self, world):
        w = world
        w.gh.install(21, "victim", ["x"])
        w.gh.users = {"attacker-code": [8]}                                                        # the attacker's GitHub account cannot see 21
        r = w.c.post("/api/v1/admin/github/installations", headers=w.hadmin, json={"installation_id": 21, "code": "attacker-code"})
        assert r.status_code == 403 and installation_tenant(conftest.PG.db, 21) is None
        assert audit(w.a, "installation_link_refused")
        assert w.c.post("/api/v1/admin/github/installations", headers=w.hadmin, json={"installation_id": 21, "code": "forged"}).status_code == 403

    def test_an_installation_that_belongs_to_another_organisation_cannot_be_taken(self, world):
        w = world
        w.gh.users = {"code": [8]}
        r = w.c.post("/api/v1/admin/github/installations", headers=w.hadmin, json={"installation_id": 8, "code": "code"})
        assert r.status_code == 409 and w.bn not in r.text and installation_tenant(conftest.PG.db, 8) == w.b

    def test_nonexistent_and_removed_installations(self, world):
        w = world
        w.gh.users = {"code": [555]}
        assert w.c.post("/api/v1/admin/github/installations", headers=w.hadmin, json={"installation_id": 555, "code": "code"}).status_code == 424

    def test_only_administrators_and_a_well_formed_request(self, world):
        w = world
        for h in (w.ha, bearer(token_for(w.a, ["release_manager"]))):
            assert w.c.post("/api/v1/admin/github/installations", headers=h, json={"installation_id": 1, "code": "c"}).status_code == 403
            assert w.c.get("/api/v1/admin/github/installations", headers=h).status_code == 403
        assert w.c.post("/api/v1/admin/github/installations", headers=w.hadmin, json={"installation_id": "x"}).status_code == 422

    def test_unlinking(self, world):
        w = world
        assert w.c.delete("/api/v1/admin/github/installations/7", headers=w.hadmin).status_code == 200
        assert installation_tenant(conftest.PG.db, 7) is None and audit(w.a, "installation_unlinked")
        assert w.c.delete("/api/v1/admin/github/installations/7", headers=w.hadmin).status_code == 404
        assert w.c.delete("/api/v1/admin/github/installations/8", headers=w.hadmin).status_code == 404       # not this organisation's
        assert installation_tenant(conftest.PG.db, 8) == w.b

    def test_without_the_app_configured(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        h = bearer(token_for(tid, ["administrator"], allow_administrator=True))
        assert c.get("/api/v1/admin/github/installations", headers=h).json()["app_configured"] is False
        assert c.post("/api/v1/admin/github/installations", headers=h, json={"installation_id": 1, "code": "c"}).status_code == 503


# =====================================================================================================================================
class TestRepositories:
    def test_discovering_the_repositories_of_the_organisations_installations(self, world):
        w = world
        r = w.c.post("/api/v1/repositories/discover", headers=w.ha, json={"provider": "github-app", "ingest": False})
        assert r.status_code == 200
        names = sorted(x["name"] for x in r.json()["discovered"])
        assert names == sorted(w.fx["paths"]) and all(x["installation"] == "7" for x in r.json()["discovered"])
        assert "theirs" not in str(r.json())                                                       # the other organisation's installation is not listed

    def test_ingesting_clones_with_an_installation_token(self, world, monkeypatch):
        w = world
        calls = []
        from tia.repos import RepoService
        real = RepoService.register

        def spy(self, url=None, path=None, name=None, token=None, branch=None):
            calls.append((url, token))
            raise __import__("tia.repos", fromlist=["IngestError"]).IngestError("clone skipped in this test")
        monkeypatch.setattr(RepoService, "register", spy)
        r = w.c.post("/api/v1/repositories/discover", headers=w.ha, json={"provider": "github-app", "names": ["payments-service"]})
        assert r.status_code == 200 and len(calls) == 1
        assert calls[0][0] == "https://github.com/acme/payments-service.git" and calls[0][1].startswith("ghs_") and w.gh.tokens[calls[0][1]] == 7
        assert r.json()["failed"][0]["name"] == "payments-service" and calls[0][1] not in r.text

    def test_registering_a_github_url_uses_the_app_not_a_pasted_token(self, world, monkeypatch):
        w = world
        calls = []
        from tia.repos import RepoService, IngestError

        def spy(self, url=None, path=None, name=None, token=None, branch=None):
            calls.append(token)
            raise IngestError("clone skipped in this test")
        monkeypatch.setattr(RepoService, "register", spy)
        w.c.post("/api/v1/repositories", headers=w.ha, json={"url": "https://github.com/acme/payments-service.git"})
        assert calls and calls[0].startswith("ghs_")

    def test_discovery_without_a_linked_installation(self, world):
        w = world
        h = bearer(token_for(conftest.new_tenant(), ["engineer"]))
        assert w.c.post("/api/v1/repositories/discover", headers=h, json={"provider": "github-app"}).status_code == 424

    def test_discovery_without_the_app(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        r = TestClient(app).post("/api/v1/repositories/discover", headers=bearer(token_for(tid, ["engineer"])), json={"provider": "github-app"})
        assert r.status_code == 503

    def test_a_removed_installation_is_reported_as_what_it_is(self, world):
        w = world
        register(w, w.a, w.ha)
        w.gh.installations[7]["gone"] = True
        r = w.c.post("/api/v1/repositories/payments-service/sync", headers=w.ha, json={})
        assert r.status_code in (424, 502) and ("no longer" in r.text or "not installed" in r.text), r.text
        assert "Traceback" not in r.text and "HTTP 404" not in r.text


# =====================================================================================================================================
def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def origin(fx, tmp_path):
    bare = tmp_path / "origin" / "payments-service.git"
    bare.parent.mkdir()
    git(tmp_path, "clone", "-q", "--bare", fx["paths"]["payments-service"], str(bare))
    git(bare, "update-ref", "refs/pull/5/head", fx["shas"]["breaking"])
    return bare


class TestWorker:
    """The pull-request job, end to end: webhook -> queue -> worker -> installation token -> comment and a REAL check run."""

    def run(self, w, origin, tenant, headers):
        assert w.c.post("/api/v1/repositories", headers=headers, json={"url": origin.as_uri(), "name": "payments-service"}).status_code == 200
        payload = event(sha=w.fx["shas"]["breaking"], url=origin.as_uri())
        assert deliver(w, payload).status_code == 202
        h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(w.tmp / "worker-ws"), scm=w.scm)
        wk = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
        while wk.run_once():
            pass
        with conftest.PG.db.tx(tenant) as c:
            return c.execute("select status, error, result from jobs where tenant_id = %s", (tenant,)).fetchone()

    def test_comment_and_check_run_with_the_installation_token(self, world, origin):
        w = world
        status, error, result = self.run(w, origin, w.a, w.ha)
        assert status == "succeeded", (status, error)
        assert result["comment"] == "created" and result["check_run"] == "created"                 # a personal token could not create the check run
        assert w.gh.comments[("acme/payments-service", 5)] and w.gh.check_runs[0]["conclusion"] == "neutral"
        kinds = {k for _, p, k in w.gh.requests if "/repos/acme/payments-service/issues" in p or "check-runs" in p}
        assert kinds == {"installation"}
        assert w.gh.check_runs[0]["head_sha"] == w.fx["shas"]["breaking"]

    def test_tokens_and_the_key_never_reach_the_database_or_the_audit_trail(self, world, origin):
        w = world
        self.run(w, origin, w.a, w.ha)
        secrets_ = list(w.gh.tokens) + [w.gh.pem, w.gh.client_secret]
        with conftest.PG.db.tx(w.a) as c:
            dump = json.dumps([list(map(str, r)) for r in c.execute("select payload, result, error from jobs")]) + json.dumps(audit(w.a)) + \
                   json.dumps([list(map(str, r)) for r in c.execute("select payload from audit_log")])
        for s in secrets_:
            assert s and s not in dump

    def test_the_other_organisation_is_untouched(self, world, origin):
        w = world
        self.run(w, origin, w.a, w.ha)
        assert queued(w.b) == [] and audit(w.b, "webhook_accepted") == []

    def test_a_removed_installation_fails_the_job_permanently_with_a_clear_reason(self, world, origin):
        w = world
        assert w.c.post("/api/v1/repositories", headers=w.ha, json={"url": origin.as_uri(), "name": "payments-service"}).status_code == 200
        assert deliver(w, event(sha=w.fx["shas"]["breaking"], url=origin.as_uri())).status_code == 202
        w.gh.installations[7]["gone"] = True
        h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(w.tmp / "worker-ws"), scm=w.scm)
        wk = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
        while wk.run_once():
            pass
        with conftest.PG.db.tx(w.a) as c:
            status, error, attempts = c.execute("select status, error, attempts from jobs where tenant_id = %s", (w.a,)).fetchone()
        assert status == "failed" and attempts == 1 and "no longer exists" in error                # permanent: no pointless retries
        assert not w.gh.comments

    def test_github_being_down_is_retried(self, world, origin):
        w = world
        assert w.c.post("/api/v1/repositories", headers=w.ha, json={"url": origin.as_uri(), "name": "payments-service"}).status_code == 200
        assert deliver(w, event(sha=w.fx["shas"]["breaking"], url=origin.as_uri())).status_code == 202
        w.gh.fail_token_status = 502
        h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(w.tmp / "worker-ws"), scm=w.scm)
        wk = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
        wk.run_once()
        with conftest.PG.db.tx(w.a) as c:
            status, attempts, error = c.execute("select status, attempts, error from jobs where tenant_id = %s", (w.a,)).fetchone()
        assert status == "queued" and attempts == 1 and "HTTP 502" in error                      # transient: back in the queue for another try

    def test_publishing_by_hand_uses_the_installation_token_too(self, world, origin, tmp_path):
        w = world
        register(w, w.a, w.ha)
        aid = w.c.post("/api/v1/impact-analyses", headers=w.ha, json={"target": "payments-service", "head": "breaking"}).json()["analysis_id"]
        sha = w.fx["shas"]["breaking"]
        r = w.c.post(f"/api/v1/impact-analyses/{aid}/publish", headers=w.ha, json={"repository": "payments-service", "pr": 9, "head_sha": sha})
        assert r.status_code == 200 and r.json()["comment"] == "created" and r.json()["check_run"] == "created", r.text
        assert all(k == "installation" for _, p, k in w.gh.requests if "/issues/9" in p or "check-runs" in p)

    def test_the_mirror_of_who_may_read_uses_installation_tokens(self, world):
        w = world
        register(w, w.a, w.ha, "payments-service")
        register(w, w.a, w.ha, "mobile-bff")
        w.gh.collaborators = {"acme/payments-service": [("ada", "push")], "acme/mobile-bff": [("ada", "pull")]}
        rm = bearer(token_for(w.a, ["release_manager"]))
        r = w.c.post("/api/v1/admin/repo-access/sync", headers=rm)
        assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.text
        assert {k for _, p, k in w.gh.requests if p.endswith("/collaborators")} == {"installation"}
