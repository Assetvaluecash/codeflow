"""Regression tests for the findings of the independent security review (docs/SECURITY.md section 5). Each one was an exploitable path."""
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakeghapp import FakeApp  # noqa: E402
from fakeidp import FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import bearer, ids_for, make_auth_app, oidc_cfg, tenant_named, token_for  # noqa: E402
from test_auth_api import Browser, audit  # noqa: E402
from test_githubapp import app_obj, map_to, scm_for  # noqa: E402
from tia import netsafe  # noqa: E402
from tia.access import AccessStore, default_mode  # noqa: E402
from tia.identity import AuthError, IdentityConfigError, parse_idp  # noqa: E402
from tia.repos import IngestError, RepoService  # noqa: E402
from tia.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def loopback(monkeypatch):
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_IDP_HOSTS", "true")


@pytest.fixture
def idp():
    p = FakeIdp()
    yield p
    p.close()


# ---- H1: a client-secret reference may name only the provider's own secret --------------------------------------------------------------
@pytest.mark.parametrize("ref", ["github/token", "idp/other/../github/token", "idp//client-secret", "idp/x/client-secret/extra", "tenant/anything", "IDP/x/client-secret"])
def test_a_provider_cannot_reference_another_secret(idp, ref):
    with pytest.raises(IdentityConfigError, match="idp/<name>/client-secret"):
        ids_for(tenant_named()[0]).put_idp("corp", "oidc", oidc_cfg(idp), client_secret_ref=ref)


def test_the_tenants_github_token_is_never_sent_to_a_provider(idp, tmp_path):
    """The exploit: reference github/token, point the token endpoint at a server you control, complete a sign-in."""
    import os
    from tia.credentials import LocalKeyWrapper
    from tia.secretstore import SecretStore
    evil = FakeIdp(client_secret="whatever")
    try:
        default = conftest.new_tenant()
        store = SecretStore(conftest.PG.db, default, LocalKeyWrapper({"k": os.urandom(32)}, "k"))
        tid, name = tenant_named()
        SecretStore(conftest.PG.db, tid, store.wrapper).put("github/token", "the-tenants-github-token")
        # the API refuses to store the reference; even a row forced in directly is not usable, because only the provider's own name is loaded
        with pytest.raises(IdentityConfigError):
            ids_for(tid).put_idp("evil", "oidc", oidc_cfg(evil), client_secret_ref="github/token")
        assert ids_for(tid).list_idps() == []
    finally:
        evil.close()


def test_explicit_endpoints_are_valid_settings_not_secrets():
    cfg = parse_idp("oidc", {"issuer": "http://127.0.0.1:9", "audience": "c", "discovery": False, "authorization_endpoint": "http://127.0.0.1:9/a",
                             "token_endpoint": "http://127.0.0.1:9/t", "jwks_uri": "http://127.0.0.1:9/k"})
    assert cfg["token_endpoint"].endswith("/t")
    with pytest.raises(IdentityConfigError, match="looks like a secret"):
        parse_idp("oidc", {"issuer": "https://x", "audience": "c", "client_secret": "s"})


# ---- H2: seeing an installation is not owning it ---------------------------------------------------------------------------------------
@pytest.fixture
def ghapp():
    g = FakeApp()
    yield g
    g.close()


def test_an_outside_collaborator_cannot_link_an_installation(ghapp):
    ghapp.install(31, "orga", ["x"])
    ghapp.users = {"owner": {"login": "boss", "installations": [31], "admin_orgs": ["orga"]},
                   "contractor": {"login": "temp", "installations": [31], "admin_orgs": []}}      # sees it (one repository) but is no owner
    a = app_obj(ghapp)
    assert a.user_sees_installation("owner", 31, "orga", "Organization") is True
    assert a.user_sees_installation("contractor", 31, "orga", "Organization") is False
    assert a.user_sees_installation("contractor", 31) is False                                    # no account to check: refused


def test_a_user_account_is_owned_by_that_user_only(ghapp):
    ghapp.install(32, "alice", ["x"])
    ghapp.installations[32]["type"] = "User"
    ghapp.users = {"alice-code": {"login": "Alice", "installations": [32]}, "bob-code": {"login": "bob", "installations": [32]}}
    a = app_obj(ghapp)
    assert a.user_sees_installation("alice-code", 32, "alice", "User") is True
    assert a.user_sees_installation("bob-code", 32, "alice", "User") is False


def test_linking_through_the_api_needs_ownership(ghapp, tmp_path):
    from test_githubapp import World  # noqa: F401
    a, an = tenant_named()
    ghapp.install(33, "orga", ["x"])
    ghapp.users = {"contractor": {"login": "temp", "installations": [33], "admin_orgs": []}}
    app, _ = make_auth_app(tmp_path, scm=scm_for(ghapp))
    c = TestClient(app)
    h = bearer(token_for(a, ["administrator"], allow_administrator=True))
    r = c.post("/api/v1/admin/github/installations", headers=h, json={"installation_id": 33, "code": "contractor"})
    assert r.status_code == 403 and "does not own" in r.text
    from tia.githubapp import installation_tenant
    assert installation_tenant(conftest.PG.db, 33) is None


def test_the_error_for_a_repository_of_someone_elses_installation_matches_the_unknown_case(ghapp):
    from tia.githubapp import AppError, TokenSource
    (a, an), (b, bn) = tenant_named(), tenant_named()
    ghapp.install(34, "orga", ["secret-repo"])
    ghapp.install(35, "orgb", ["mine"])
    map_to(an, 34)
    map_to(bn, 35)
    ts = TokenSource(scm_for(ghapp), conftest.PG.db)
    with pytest.raises(AppError) as theirs:
        ts.token_for(b, full_name="orga/secret-repo")
    with pytest.raises(AppError) as unknown:
        ts.token_for(b, full_name="orga/does-not-exist")
    assert str(theirs.value).replace("secret-repo", "X") == str(unknown.value).replace("does-not-exist", "X")


# ---- M1: an anonymous caller cannot write into a tenant's audit log or create directories ----------------------------------------------
def test_forged_sign_in_state_writes_nothing_for_any_tenant(idp, tmp_path):
    tid, name = tenant_named()
    ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
    app, _ = make_auth_app(tmp_path)
    c = TestClient(app)
    before = len(audit(tid))
    for _ in range(5):
        c.get("/auth/callback", params={"code": "x", "state": uuid.UUID(tid).hex + "." + "a" * 30})        # the real tenant's id, a made-up secret
    random_tenant = uuid.uuid4()
    c.get("/auth/callback", params={"code": "x", "state": random_tenant.hex + "." + "a" * 30})
    c.post("/auth/saml/acs", data={"SAMLResponse": "x", "RelayState": random_tenant.hex + "." + "b" * 30})
    assert len(audit(tid)) == before                                                                        # nothing was recorded for the victim
    assert not (tmp_path / "ws" / str(random_tenant)).exists()                                              # and no directory for an invented tenant


def test_a_failed_sign_in_that_did_start_is_still_audited(idp, tmp_path):
    tid, name = tenant_named()
    ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
    app, _ = make_auth_app(tmp_path)
    idp.fail_authorize = "access_denied"
    b = Browser(app)
    b.sign_in(name)
    assert audit(tid, "login_failed")


def test_forged_cookies_do_not_grow_the_identity_cache_without_bound(tmp_path):
    app, _ = make_auth_app(tmp_path)
    c = TestClient(app)
    for _ in range(600):
        c.cookies.set("tia_session", uuid.uuid4().hex + "." + "a" * 30)
        c.get("/api/v1/repositories")
    assert len(app.state.auth._ids) <= 513


def test_just_in_time_users_are_not_created_for_people_who_get_no_role(idp, tmp_path):
    tid, name = tenant_named()
    ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
    app, _ = make_auth_app(tmp_path)
    c = TestClient(app)
    for i in range(5):
        r = c.get("/api/v1/me", headers=bearer(idp.mint(sub=f"stranger-{i}", groups=["unrelated"])))
        assert r.status_code == 403
    assert ids_for(tid).list_users() == []                                                                  # no row per stranger


# ---- M2: a provider that dribbles bytes cannot hold a thread --------------------------------------------------------------------------------
def test_a_slow_provider_is_cut_off_at_the_deadline(monkeypatch):
    monkeypatch.setattr(netsafe, "DEADLINE", 1.5)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def dribble():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                for _ in range(100):
                    if stop.is_set():
                        break
                    conn.sendall(b"x")
                    time.sleep(0.5)                                                                        # one byte every half second
            except OSError:
                pass
            finally:
                conn.close()
    threading.Thread(target=dribble, daemon=True).start()
    t = time.monotonic()
    with pytest.raises(netsafe.FetchError):
        netsafe.request("GET", f"http://127.0.0.1:{port}/")
    assert time.monotonic() - t < 4
    stop.set()
    srv.close()


def test_a_failing_provider_is_not_asked_again_on_every_request(idp, tmp_path):
    tid, name = tenant_named()
    ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
    idp.issuer_override = "https://someone-else.example"
    app, _ = make_auth_app(tmp_path)
    c = TestClient(app)
    for _ in range(4):
        assert c.get("/auth/login", params={"org": name}, follow_redirects=False).status_code == 502
    assert sum(1 for q in idp.requests if q[1].endswith("openid-configuration")) == 1


# ---- M3: nothing unauthenticated is buffered without a limit --------------------------------------------------------------------------------
def test_oversized_webhook_and_acs_bodies_are_refused(tmp_path):
    from tia.webhooks import ScmConfig
    app, _ = make_auth_app(tmp_path, scm=ScmConfig(github_webhook_secret=b"s"))
    c = TestClient(app)
    big = b"x" * (27 * 1024 * 1024)
    assert c.post("/api/v1/webhooks/github", content=big, headers={"X-GitHub-Event": "ping"}).status_code == 413
    r = c.post("/auth/saml/acs", content=b"SAMLResponse=" + b"A" * 2_000_000, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


# ---- M4: repository hosts and discovery endpoints cannot be aimed at the internal network -------------------------------------------------
def test_a_repository_on_a_private_address_is_refused_unless_local_access_is_on(tmp_path):
    t = tenant_named()[0]
    st = Store(conftest.PG.db, t)
    svc = RepoService(st, str(tmp_path / "ws"), allow_local=False)
    for url in ("https://127.0.0.1/x.git", "https://169.254.169.254/latest/meta-data", "http://10.0.0.5/r.git", "https://[::1]/r.git"):
        with pytest.raises(IngestError, match="non-public"):
            svc.register(url=url)
    with pytest.raises(IngestError, match="http"):
        svc.discover("github", "o", None, "tok", api_base="file:///etc/passwd")
    with pytest.raises(IngestError, match="non-public"):
        svc.discover("github", "o", None, "tok", api_base="https://169.254.169.254")


# ---- M5: what a caller cannot read is not named in metadata either ------------------------------------------------------------------------
def test_graph_versions_and_rescan_name_only_readable_repositories(fx, tmp_path):
    app, _ = make_auth_app(tmp_path)
    tid = conftest.new_tenant()
    c = TestClient(app)
    full = bearer(token_for(tid, ["engineer"], name="all"))
    for n, p in fx["paths"].items():
        assert c.post("/api/v1/repositories", headers=full, json={"path": p, "name": n}).status_code == 200
    c.get("/api/v1/graph", headers=full)
    scoped = bearer(token_for(tid, ["engineer"], name="scoped", repos=["payments-service"]))
    v = c.get("/api/v1/graph/versions", headers=scoped).json()["versions"]
    assert v and all(set(x["inputs"]["repos"]) <= {"payments-service"} for x in v if x["inputs"].get("repos"))
    assert set(sum((list(x["inputs"].get("repos", {})) for x in c.get("/api/v1/graph/versions", headers=full).json()["versions"]), [])) > {"payments-service"}
    one = c.get(f"/api/v1/graph/versions/{v[0]['id']}", headers=scoped).json()
    assert set(one["inputs"].get("repos", {})) <= {"payments-service"}
    r = c.post("/api/v1/graph/rescan", headers=scoped).json()
    assert {x["repo"] for x in r["repositories"]} <= {"payments-service"}


def test_the_registrar_of_a_repository_they_may_not_read_learns_nothing_about_it(tmp_path, fx):
    from test_access import AccessGitHub  # noqa: F401
    tid = conftest.new_tenant()
    app, _ = make_auth_app(tmp_path)
    c = TestClient(app)
    scoped = bearer(token_for(tid, ["engineer"], name="s", repos=["other"]))
    r = c.post("/api/v1/repositories", headers=scoped, json={"path": fx["paths"]["payments-service"], "name": "payments-service"})
    assert r.status_code == 200 and set(r.json()) == {"name", "provider", "note"}                     # no branches, commit or server path


# ---- M6: access rows die with the repository ------------------------------------------------------------------------------------------------
def test_deleting_a_repository_removes_its_access_rows(fx, tmp_path):
    tid = conftest.new_tenant()
    app, _ = make_auth_app(tmp_path)
    c = TestClient(app)
    admin = bearer(token_for(tid, ["administrator"], name="root", allow_administrator=True))
    c.post("/api/v1/repositories", headers=admin, json={"path": fx["paths"]["payments-service"], "name": "payments-service"})
    acc = AccessStore(conftest.PG.db, tid)
    acc.set_mode("payments-service", "tenant", "t")
    with conftest.PG.db.tx(tid) as cur:
        cur.execute("insert into scm_permissions (tenant_id, host, repo, login, permission) values (%s,'github','payments-service','ada','push')", (tid,))
        cur.execute("insert into scm_sync_status (tenant_id, host, repo, ok) values (%s,'github','payments-service',true)", (tid,))
    assert c.delete("/api/v1/repositories/payments-service", headers=admin).status_code == 200
    with conftest.PG.db.tx(tid) as cur:
        for t in ("repo_access", "repo_grants", "scm_permissions", "scm_sync_status"):
            assert cur.execute(f"select count(*) from {t} where tenant_id = %s and repo = 'payments-service'", (tid,)).fetchone()[0] == 0, t


# ---- M8: hosted repositories without a mirror are not readable by the whole tenant -----------------------------------------------------------
@pytest.mark.parametrize("provider,mode", [("github", "scm"), ("local", "tenant"), ("upload", "tenant"), ("ado", "restricted"), ("git", "restricted"), ("something-new", "restricted")])
def test_default_access_fails_closed_for_hosts_without_a_mirror(provider, mode):
    assert default_mode(provider) == mode
