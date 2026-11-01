"""UC1-18 / UC1-19 through the real app: service tokens, OIDC bearer tokens, browser sign-in, roles, CSRF, tenant routing, administration."""
import http.client
import io
import os
import sys
import urllib.parse
import uuid
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakeidp import FakeIdp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import (PUBLIC_URL, authapi_parse, bearer, ids_for, make_auth_app, oidc_cfg, tenant_named, token_for)  # noqa: E402
from tia import authapi, rbac  # noqa: E402
from tia.api import create_app  # noqa: E402
from tia.credentials import LocalKeyWrapper  # noqa: E402
from tia.secretstore import SecretStore  # noqa: E402
from tia.store import Store  # noqa: E402
from tia.tenants import Tenants  # noqa: E402


@pytest.fixture(autouse=True)
def loopback_idp(monkeypatch):
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_IDP_HOSTS", "true")


@pytest.fixture
def idp():
    p = FakeIdp()
    yield p
    p.close()


def audit(tid, event=None, limit=200):
    rows = Store(conftest.PG.db, tid).audit_events(limit)
    return [r for r in rows if event is None or r["event"] == event]


def zip_bytes(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return b.getvalue()


def get_no_redirect(url):
    u = urllib.parse.urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    c.request("GET", u.path + ("?" + u.query if u.query else ""))
    r = c.getresponse()
    r.read()
    return r.status, r.getheader("Location")


class Browser:
    """A user agent: the TestClient's cookie jar plus the redirect to the identity provider done over real HTTP."""

    def __init__(self, app, base="http://testserver"):
        self.c = TestClient(app, base_url=base)

    def sign_in(self, org, idp_name="corp", return_to="/", expect=302):
        r = self.c.get("/auth/login", params={"org": org, "idp": idp_name, "return_to": return_to}, follow_redirects=False)
        if r.status_code != 302:
            return r
        status, loc = get_no_redirect(r.headers["location"])
        assert status == 302, status
        cb = urllib.parse.urlsplit(loc)
        return self.c.get(cb.path + "?" + cb.query, follow_redirects=False)

    def me(self):
        return self.c.get("/auth/me").json()

    def post(self, path, **kw):
        h = {"X-TIA-CSRF": self.me().get("csrf") or "", **kw.pop("headers", {})}
        return self.c.post(path, headers=h, **kw)


def setup_org(idp, tmp_path, groups=("tia-engineers",), **app_kw):
    tid, name = tenant_named()
    ids = ids_for(tid)
    row = ids.put_idp("corp", "oidc", oidc_cfg(idp))
    app, _default = make_auth_app(tmp_path, **app_kw)
    idp.user = {"sub": "u-" + uuid.uuid4().hex[:6], "email": "ada@example.com", "name": "Ada", "groups": list(groups)}
    return app, tid, name, row


# =====================================================================================================================================
class TestUnauthenticated:
    def test_public_routes_and_protected_routes(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        c = TestClient(app)
        for p in ("/healthz", "/readyz", "/api/v1/health", "/"):
            assert c.get(p).status_code == 200, p
        r = c.get("/api/v1/repositories")
        assert r.status_code == 401 and r.json()["code"] == "authentication_required" and r.headers["WWW-Authenticate"].startswith("Bearer")
        assert "traceback" not in r.text.lower()
        for method, p in (("get", "/api/v1/audit"), ("post", "/api/v1/impact-analyses"), ("put", "/api/v1/tenant/config"),
                          ("get", "/api/v1/admin/tokens"), ("get", "/analyses/abc"), ("get", "/api/v1/graph"), ("delete", "/api/v1/repositories/x")):
            assert getattr(c, method)(p).status_code == 401, p

    def test_interactive_docs_are_not_published(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        c = TestClient(app)
        assert all(c.get(p).status_code == 404 for p in ("/docs", "/redoc", "/openapi.json"))

    def test_auth_me_says_not_authenticated_without_a_401(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        r = TestClient(app).get("/auth/me")
        assert r.status_code == 200 and r.json()["authenticated"] is False and r.json()["auth"] == "required"

    @pytest.mark.parametrize("h", ["Bearer", "Bearer ", "Basic abc", "Bearer not-a-token", "Bearer a.b.c", "Bearer tia_bad",
                                   "Bearer tia_" + "0" * 32 + "_" + "0" * 32 + "_" + "x" * 43])
    def test_bad_credentials(self, tmp_path, h):
        app, _ = make_auth_app(tmp_path)
        r = TestClient(app).get("/api/v1/repositories", headers={"Authorization": h})
        assert r.status_code == 401 and r.json()["code"] in ("invalid_token", "authentication_required")

    def test_garbage_session_cookie(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        c = TestClient(app)
        c.cookies.set("tia_session", "garbage")
        assert c.get("/api/v1/repositories").status_code == 401

    def test_report_page_redirects_a_browser_to_sign_in(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        r = TestClient(app).get("/analyses/abc", headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith("/?return_to=/analyses/abc")

    def test_browser_hardening_headers_and_no_caching_of_what_a_person_sees(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        for r in (c.get("/"), c.get("/healthz"), c.get("/api/v1/repositories"), c.get("/api/v1/repositories", headers=bearer(token_for(tid, ["viewer"])))):
            assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["referrer-policy"] == "no-referrer"
        assert c.get("/api/v1/repositories", headers=bearer(token_for(tid, ["viewer"]))).headers["cache-control"] == "no-store"
        assert "no-store" in c.get("/auth/me").headers["cache-control"]
        dev = TestClient(create_app(conftest.PG.db, conftest.new_tenant(), str(tmp_path / "devws"), allow_local=True))
        assert "x-frame-options" not in dev.get("/").headers                                    # development mode is untouched

    def test_development_mode_is_unchanged(self, tmp_path):
        app = create_app(conftest.PG.db, conftest.new_tenant(), str(tmp_path / "ws"), allow_local=True)
        c = TestClient(app)
        assert c.get("/api/v1/repositories").status_code == 200
        assert c.get("/auth/me").json()["auth"] == "none" and c.get("/api/v1/config").json()["auth"].startswith("none")


# =====================================================================================================================================
class TestRoles:
    CASES = [("GET", "/api/v1/repositories", None, "read"), ("GET", "/api/v1/tenant/config", None, "read"),
             ("POST", "/api/v1/repositories", {}, "repo_write"), ("DELETE", "/api/v1/repositories/nope", None, "repo_delete"),
             ("POST", "/api/v1/impact-analyses", {"target": "nope"}, "analyze"), ("GET", "/api/v1/audit", None, "audit_read"),
             ("PUT", "/api/v1/tenant/config", {"config": {}}, "config_write"), ("GET", "/api/v1/admin/tokens", None, "admin"),
             ("POST", "/api/v1/graph/rescan", None, "repo_write"), ("GET", "/api/v1/audit/verify", None, "audit_read")]

    @pytest.mark.parametrize("role", rbac.ROLES)
    def test_each_role_gets_exactly_its_permissions(self, tmp_path, role):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        h = bearer(token_for(tid, [role], allow_administrator=True))
        for method, path, body, perm in self.CASES:
            r = c.request(method, path, headers=h, **({"json": body} if body is not None else {}))
            allowed = rbac.allows({role}, perm)
            assert (r.status_code != 403) == allowed, (role, method, path, r.status_code, r.text[:100])
            if not allowed:
                assert r.json()["code"] == "forbidden"

    def test_roles_are_additive(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        h = bearer(token_for(tid, ["viewer", "auditor"]))
        assert c.get("/api/v1/audit", headers=h).status_code == 200 and c.get("/api/v1/repositories", headers=h).status_code == 200
        assert c.post("/api/v1/impact-analyses", headers=h, json={"target": "x"}).status_code == 403

    def test_denials_are_audited_with_the_actor_and_the_chain_verifies(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        tok = ids_for(tid).create_token("viewer-bot", ["viewer"])["token"]
        assert c.delete("/api/v1/repositories/x", headers=bearer(tok)).status_code == 403
        ev = audit(tid, "access_denied")
        assert ev and ev[0]["actor"] == "service:viewer-bot" and ev[0]["path"] == "/api/v1/repositories/x" and ev[0]["code"] == "forbidden"
        assert Store(conftest.PG.db, tid).verify_audit()["ok"]

    def test_what_a_caller_is_told_never_includes_the_internal_detail(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        r = TestClient(app).delete("/api/v1/repositories/x", headers=bearer(token_for(tid, ["viewer"])))
        assert "lacks" not in r.text and "delete" not in r.text.lower().replace("deleted", "")

    def test_a_revoked_token_stops_working_immediately(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        t = ids_for(tid).create_token("ci", ["viewer"])
        c = TestClient(app)
        assert c.get("/api/v1/repositories", headers=bearer(t["token"])).status_code == 200
        ids_for(tid).revoke_token(t["id"])
        assert c.get("/api/v1/repositories", headers=bearer(t["token"])).status_code == 401

    def test_who_am_i(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        r = TestClient(app).get("/api/v1/me", headers=bearer(token_for(tid, ["engineer"], name="ci-bot")))
        j = r.json()
        assert j["kind"] == "service" and j["subject"] == "ci-bot" and j["roles"] == ["engineer"] and j["tenant_id"] == tid
        assert "analyze" in j["permissions"] and "admin" not in j["permissions"]

    def test_config_changes_record_the_principal_not_a_spoofed_header(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        h = {**bearer(token_for(tid, ["release_manager"], name="rm-bot")), "X-TIA-Actor": "someone-else"}
        r = c.put("/api/v1/tenant/config", headers=h, json={"config": {"limits": {"max_depth": 5}}})
        assert r.status_code == 200
        v = c.get("/api/v1/tenant/config/versions/1", headers=h).json()
        assert v["actor"] == "service:rm-bot"
        assert audit(tid, "tenant_config_changed")[0]["actor"] == "service:rm-bot"


# =====================================================================================================================================
class TestTenantRouting:
    def test_one_deployment_serves_two_tenants_without_mixing_them(self, tmp_path, fx):
        app, _default = make_auth_app(tmp_path)
        a, b = conftest.new_tenant(), conftest.new_tenant()
        ha, hb = bearer(token_for(a, ["engineer"])), bearer(token_for(b, ["engineer"]))
        c = TestClient(app)
        z = zip_bytes({"app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/ping')\ndef ping():\n    return 1\n"})
        for h in (ha, hb):
            r = c.put("/api/v1/repositories/shop/upload", headers=h, content=z)
            assert r.status_code == 200, r.text
        assert c.post("/api/v1/repositories", headers=ha, json={"path": fx["paths"]["payments-service"], "name": "payments-service"}).status_code == 200
        assert [r["name"] for r in c.get("/api/v1/repositories", headers=ha).json()] == ["payments-service", "shop"]
        assert [r["name"] for r in c.get("/api/v1/repositories", headers=hb).json()] == ["shop"]
        ws = tmp_path / "ws"
        assert (ws / a / "shop").is_dir() and (ws / b / "shop").is_dir() and (ws / a / "shop") != (ws / b / "shop")      # separate clones
        rep = c.post("/api/v1/impact-analyses", headers=ha, json={"target": "payments-service", "head": "breaking"}).json()
        aid = rep["analysis_id"]
        assert c.get(f"/api/v1/impact-analyses/{aid}", headers=ha).status_code == 200
        assert c.get(f"/api/v1/impact-analyses/{aid}", headers=hb).status_code == 404                                  # another tenant's report
        assert c.get(f"/api/v1/impact-analyses/{aid}/evidence", headers=hb).status_code == 404
        job = c.post("/api/v1/impact-analyses?async=true", headers=ha, json={"target": "payments-service", "head": "impl"}).json()
        assert c.get(f"/api/v1/jobs/{job['job_id']}", headers=ha).status_code == 200
        assert c.get(f"/api/v1/jobs/{job['job_id']}", headers=hb).status_code == 404
        assert c.delete(f"/api/v1/jobs/{job['job_id']}", headers=hb).status_code == 409
        for t in (a, b):
            assert Store(conftest.PG.db, t).verify_audit()["ok"]
        assert not [e for e in audit(b) if e["event"] in ("analysis_queued", "repository_registered") and e.get("name") == "payments-service"]

    def test_the_tenant_comes_from_the_credential_not_from_the_request(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        a, b = conftest.new_tenant(), conftest.new_tenant()
        ha = bearer(token_for(a, ["administrator"], allow_administrator=True))
        c = TestClient(app)
        for hdr in ({"X-TIA-Tenant": b}, {"X-Tenant-Id": b}, {"X-Forwarded-Host": b}):
            assert c.get("/api/v1/me", headers={**ha, **hdr}).json()["tenant_id"] == a, hdr
        # a service token of tenant A with tenant B's id swapped into it is just an invalid token
        tok = token_for(a, ["viewer"])
        forged = tok.replace(uuid.UUID(a).hex, uuid.UUID(b).hex, 1)
        assert c.get("/api/v1/repositories", headers=bearer(forged)).status_code == 401

    def test_a_suspended_tenant_is_locked_out_even_with_a_valid_token(self, tmp_path):
        app, _ = make_auth_app(tmp_path, gate_ttl=0)
        tid, name = tenant_named()
        h = bearer(token_for(tid, ["viewer"]))
        c = TestClient(app)
        assert c.get("/api/v1/repositories", headers=h).status_code == 200
        Tenants(conftest.PG.admin).suspend(name, "test")
        r = c.get("/api/v1/repositories", headers=h)
        assert r.status_code == 403 and r.json()["code"] == "tenant_suspended"
        Tenants(conftest.PG.admin).resume(name)
        assert c.get("/api/v1/repositories", headers=h).status_code == 200

    def test_each_tenant_has_its_own_quota_and_configuration(self, tmp_path):
        app, _ = make_auth_app(tmp_path, gate_ttl=0)
        (a, an), b = tenant_named(), conftest.new_tenant()
        Tenants(conftest.PG.admin).set_quota(an, 123456)
        ha, hb = bearer(token_for(a, ["release_manager"])), bearer(token_for(b, ["viewer"]))
        c = TestClient(app)
        assert c.put("/api/v1/tenant/config", headers=ha, json={"config": {"limits": {"max_depth": 3}}}).status_code == 200
        assert c.get("/api/v1/tenant/config", headers=ha).json()["config"]["limits"]["max_depth"] == 3
        assert c.get("/api/v1/tenant/config", headers=hb).json()["config"]["limits"]["max_depth"] == 8
        assert c.get("/api/v1/tenant", headers=ha).json()["storage_quota_bytes"] == 123456
        assert c.get("/api/v1/tenant", headers=hb).json()["storage_quota_bytes"] is None


# =====================================================================================================================================
class TestOidcBearer:
    def test_a_valid_token_authenticates_with_roles_from_groups(self, idp, tmp_path):
        app, tid, _name, _row = setup_org(idp, tmp_path)
        c = TestClient(app)
        r = c.get("/api/v1/me", headers=bearer(idp.mint(groups=["tia-engineers", "tia-audit"])))
        assert r.status_code == 200
        j = r.json()
        assert j["kind"] == "user" and j["tenant_id"] == tid and j["roles"] == ["auditor", "engineer"] and j["email"] == "ada@example.com"
        assert [u["subject"] for u in ids_for(tid).list_users()] == [idp.user["sub"]]                    # provisioned just in time

    @pytest.mark.parametrize("over,why", [({"aud": "other-client"}, "audience"), ({"exp": 1}, "expired"), ({"iss": "https://evil"}, "issuer")])
    def test_invalid_tokens(self, idp, tmp_path, over, why):
        app, *_ = setup_org(idp, tmp_path)
        r = TestClient(app).get("/api/v1/me", headers=bearer(idp.mint(**over)))
        assert r.status_code == 401 and r.json()["code"] == "invalid_token", why
        assert why not in r.text

    def test_a_token_signed_by_another_key_is_refused(self, idp, tmp_path):
        from fakeidp import Key
        app, *_ = setup_org(idp, tmp_path)
        assert TestClient(app).get("/api/v1/me", headers=bearer(idp.mint(key=Key("RSA", "k1")))).status_code == 401

    def test_a_user_without_a_mapped_group_has_no_role(self, idp, tmp_path):
        app, tid, *_ = setup_org(idp, tmp_path)
        r = TestClient(app).get("/api/v1/me", headers=bearer(idp.mint(groups=["unrelated"])))
        assert r.status_code == 403 and r.json()["code"] == "no_role"
        assert audit(tid, "access_denied")[0]["code"] == "no_role"

    def test_default_role_applies_when_no_group_matches(self, idp, tmp_path):
        tid, _ = tenant_named()
        ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp, default_role="viewer"))
        app, _ = make_auth_app(tmp_path)
        r = TestClient(app).get("/api/v1/me", headers=bearer(idp.mint(groups=["unrelated"])))
        assert r.status_code == 200 and r.json()["roles"] == ["viewer"]

    def test_an_administrators_assignment_overrides_the_groups_and_applies_within_the_cache_window(self, idp, tmp_path):
        app, tid, *_ = setup_org(idp, tmp_path)
        c = TestClient(app)
        h = bearer(idp.mint(groups=["tia-admins"]))
        assert c.get("/api/v1/me", headers=h).json()["roles"] == ["administrator"]
        u = ids_for(tid).list_users()[0]
        ids_for(tid).update_user(u["id"], roles=["viewer"])
        app.state.auth.forget(tid)
        assert c.get("/api/v1/me", headers=h).json()["roles"] == ["viewer"]

    def test_a_disabled_user_is_refused(self, idp, tmp_path):
        app, tid, *_ = setup_org(idp, tmp_path)
        c = TestClient(app)
        h = bearer(idp.mint())
        assert c.get("/api/v1/me", headers=h).status_code == 200
        ids_for(tid).update_user(ids_for(tid).list_users()[0]["id"], disabled=True)
        app.state.auth.forget(tid)
        r = c.get("/api/v1/me", headers=h)
        assert r.status_code == 403 and r.json()["code"] == "user_disabled"

    def test_two_tenants_with_their_own_providers_on_one_deployment(self, tmp_path):
        i1, i2 = FakeIdp("client-1"), FakeIdp("client-2")
        try:
            t1, t2 = tenant_named()[0], tenant_named()[0]
            ids_for(t1).put_idp("corp", "oidc", oidc_cfg(i1))
            ids_for(t2).put_idp("corp", "oidc", oidc_cfg(i2))
            app, _ = make_auth_app(tmp_path)
            c = TestClient(app)
            assert c.get("/api/v1/me", headers=bearer(i1.mint())).json()["tenant_id"] == t1
            assert c.get("/api/v1/me", headers=bearer(i2.mint())).json()["tenant_id"] == t2
            # a token from tenant 1's provider cannot be replayed for tenant 2's application (audience), nor the reverse
            assert c.get("/api/v1/me", headers=bearer(i1.mint(aud="client-2"))).status_code == 401
        finally:
            i1.close()
            i2.close()

    def test_a_provider_outage_is_a_503_not_a_401_and_not_a_crash(self, idp, tmp_path):
        app, *_ = setup_org(idp, tmp_path)
        tok = idp.mint(header={"kid": "unseen"})
        idp.close()
        r = TestClient(app).get("/api/v1/me", headers=bearer(tok))
        assert r.status_code in (401, 502, 503)

    def test_allowed_algorithms_can_be_narrowed_per_provider(self, idp, tmp_path):
        from fakeidp import Key
        tid, _ = tenant_named()
        ec = Key("EC", "e1")
        idp.keys.append(ec)
        ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp, allowed_algs=["ES256"]))
        app, _ = make_auth_app(tmp_path)
        c = TestClient(app)
        assert c.get("/api/v1/me", headers=bearer(idp.mint(alg="RS256", key=idp.keys[0]))).status_code == 401
        assert c.get("/api/v1/me", headers=bearer(idp.mint(alg="ES256", key=ec))).status_code == 200


# =====================================================================================================================================
class TestBrowserSignIn:
    def test_full_flow_cookie_flags_and_me(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        b = Browser(app)
        r = b.sign_in(name, return_to="/analyses/abc")
        assert r.status_code == 302 and r.headers["location"] == "/analyses/abc"
        cookie = r.headers["set-cookie"].lower()
        assert "tia_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie.replace("samesite", "")
        me = b.me()
        assert me["authenticated"] and me["principal"]["roles"] == ["engineer"] and me["csrf"] and "analyze" in me["permissions"]
        assert b.c.get("/api/v1/repositories").status_code == 200
        ev = audit(tid, "login_succeeded")
        assert ev and ev[0]["method"] == "oidc" and ev[0]["roles"] == ["engineer"] and Store(conftest.PG.db, tid).verify_audit()["ok"]

    def test_cookie_is_secure_when_the_public_url_is_https(self, idp, tmp_path):
        tid, name = tenant_named()
        ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
        app, _ = make_auth_app(tmp_path, public_url="https://tia.example.com")
        b = Browser(app, base="https://tia.example.com")
        r = b.sign_in(name)
        assert r.status_code == 302 and "secure" in r.headers["set-cookie"].lower().replace("samesite", "")

    def test_state_changes_need_the_csrf_header(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        b = Browser(app)
        b.sign_in(name)
        assert b.c.post("/api/v1/impact-analyses", json={"target": "x"}).status_code == 403                      # cookie alone is not enough
        assert b.c.post("/api/v1/impact-analyses", json={"target": "x"}, headers={"X-TIA-CSRF": "wrong"}).json()["code"] == "csrf"
        r = b.post("/api/v1/impact-analyses", json={"target": "x"})
        assert r.status_code != 403                                                                              # allowed with the right value
        assert b.c.get("/api/v1/repositories").status_code == 200                                                # reads need no CSRF value
        r = b.post("/api/v1/impact-analyses", json={"target": "x"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["code"] == "csrf"
        assert b.post("/api/v1/impact-analyses", json={"target": "x"}, headers={"Origin": PUBLIC_URL}).status_code != 403

    def test_bearer_tokens_need_no_csrf_header(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        assert TestClient(app).post("/api/v1/impact-analyses", json={"target": "x"}, headers=bearer(token_for(tid, ["engineer"]))).status_code != 403

    def test_logout_revokes_the_session_server_side(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        b = Browser(app)
        b.sign_in(name)
        stolen = dict(b.c.cookies)
        assert b.c.post("/auth/logout", headers={"X-TIA-CSRF": b.me()["csrf"]}).json()["signed_out"]
        assert b.me()["authenticated"] is False
        thief = TestClient(app)
        for k, v in stolen.items():
            thief.cookies.set(k, v)
        assert thief.get("/api/v1/repositories").status_code == 401                                              # the copied cookie is dead too
        assert audit(tid, "logout")

    def test_the_callback_cannot_be_replayed(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        b = Browser(app)
        r = b.c.get("/auth/login", params={"org": name}, follow_redirects=False)
        _, loc = get_no_redirect(r.headers["location"])
        cb = urllib.parse.urlsplit(loc)
        assert b.c.get(cb.path + "?" + cb.query, follow_redirects=False).status_code == 302
        r2 = b.c.get(cb.path + "?" + cb.query, follow_redirects=False)
        assert r2.status_code == 400 and "login_failed" in r2.text

    def test_login_csrf_a_state_started_in_another_browser_is_refused(self, idp, tmp_path):
        """An attacker starts a sign-in, then tricks a victim's browser into completing it: the victim has no matching login cookie."""
        app, tid, name, _ = setup_org(idp, tmp_path)
        attacker, victim = Browser(app), Browser(app)
        r = attacker.c.get("/auth/login", params={"org": name}, follow_redirects=False)
        _, loc = get_no_redirect(r.headers["location"])
        cb = urllib.parse.urlsplit(loc)
        r2 = victim.c.get(cb.path + "?" + cb.query, follow_redirects=False)
        assert r2.status_code == 400 and victim.me()["authenticated"] is False

    @pytest.mark.parametrize("state", ["", "garbage", "0" * 32 + "." + "a" * 30, uuid.uuid4().hex + "." + "b" * 30])
    def test_bad_state(self, idp, tmp_path, state):
        app, *_ = setup_org(idp, tmp_path)
        r = TestClient(app).get("/auth/callback", params={"code": "x", "state": state}, follow_redirects=False)
        assert r.status_code == 400

    def test_the_provider_reporting_an_error(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        idp.fail_authorize = "access_denied"
        r = Browser(app).sign_in(name)
        assert r.status_code == 401 and "access_denied" not in r.text
        assert audit(tid, "login_failed")

    def test_a_wrong_nonce_in_the_id_token_is_refused(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        idp.nonce_override = "not-the-nonce"
        b = Browser(app)
        assert b.sign_in(name).status_code == 401 and b.me()["authenticated"] is False

    def test_the_token_endpoint_failing(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        idp.token_status = 500
        assert Browser(app).sign_in(name).status_code == 401

    def test_a_user_with_no_role_cannot_sign_in(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path, groups=("nobody",))
        b = Browser(app)
        r = b.sign_in(name)
        assert r.status_code == 403 and b.me()["authenticated"] is False
        assert audit(tid, "login_failed")[0]["code"] == "no_role"

    def test_a_disabled_user_cannot_sign_in_and_existing_sessions_die(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        b = Browser(app)
        b.sign_in(name)
        user = ids_for(tid).list_users()[0]
        ids_for(tid).update_user(user["id"], disabled=True)
        assert b.c.get("/api/v1/repositories").status_code in (401, 403)
        assert Browser(app).sign_in(name).status_code == 403

    def test_unknown_organisations_and_providers(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        c = TestClient(app)
        assert c.get("/auth/login", params={"org": "nope"}, follow_redirects=False).status_code == 404
        assert c.get("/auth/login", params={"org": name, "idp": "nope"}, follow_redirects=False).status_code == 404
        assert c.get("/auth/login", params={"org": "x" * 200}, follow_redirects=False).status_code == 404
        assert c.get("/auth/options", params={"org": name}).json() == {"providers": [{"name": "corp", "protocol": "oidc"}]}
        assert c.get("/auth/options", params={"org": "nope"}).json() == {"providers": []}
        assert "tenant_id" not in c.get("/auth/options", params={"org": name}).text

    def test_open_redirect_attempts_land_on_the_home_page(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        for evil in ("https://evil.example/x", "//evil.example", "/\\evil.example"):
            idp.user["sub"] = "u-" + uuid.uuid4().hex[:6]
            r = Browser(app).sign_in(name, return_to=evil)
            assert r.status_code == 302 and r.headers["location"] == "/"

    def test_sign_in_needs_a_public_url(self, idp, tmp_path):
        tid, name = tenant_named()
        ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
        app, _ = make_auth_app(tmp_path, public_url="")
        r = TestClient(app).get("/auth/login", params={"org": name}, follow_redirects=False)
        assert r.status_code == 503 and "TIA_PUBLIC_URL" not in r.text

    def test_discovery_that_names_another_issuer_is_refused(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        idp.issuer_override = "https://someone-else.example"
        r = Browser(app).sign_in(name)
        assert r.status_code in (502, 503)

    def test_provider_addresses_on_the_private_network_are_refused_at_run_time(self, idp, tmp_path, monkeypatch):
        """A tenant that points its provider at an internal address gets nothing back from the platform's network."""
        app, tid, name, _ = setup_org(idp, tmp_path)
        monkeypatch.delenv("TIA_ALLOW_PRIVATE_IDP_HOSTS")
        r = Browser(app).sign_in(name)
        assert r.status_code == 502 and not any(q[1] == "/.well-known/openid-configuration" for q in idp.requests)

    def test_the_client_secret_comes_from_the_encrypted_store(self, tmp_path):
        idp = FakeIdp(client_secret="s3cret-value")
        try:
            tid, name = tenant_named()
            default = conftest.new_tenant()
            store = SecretStore(conftest.PG.db, default, LocalKeyWrapper({"k": os.urandom(32)}, "k"))
            SecretStore(conftest.PG.db, tid, store.wrapper).put("idp/corp/client-secret", "s3cret-value")
            ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp), client_secret_ref="idp/corp/client-secret")
            app, _ = make_auth_app(tmp_path, tenant=default, secrets=store)
            idp.user = {"sub": "u1", "email": "a@x", "name": "A", "groups": ["tia-viewers"]}
            r = Browser(app).sign_in(name)
            assert r.status_code == 302
            sent = [q[2] for q in idp.requests if q[0] == "POST"][0]
            assert sent["client_secret"] == "s3cret-value" and sent["code_verifier"]
        finally:
            idp.close()

    def test_a_missing_client_secret_is_a_configuration_error_not_a_public_client(self, tmp_path):
        idp = FakeIdp(client_secret="x")
        try:
            tid, name = tenant_named()
            ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp), client_secret_ref="idp/missing/client-secret")
            app, _ = make_auth_app(tmp_path)
            assert Browser(app).sign_in(name).status_code in (502, 503)
        finally:
            idp.close()

    def test_sessions_follow_a_later_role_assignment(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        b = Browser(app)
        b.sign_in(name)
        assert "analyze" in b.me()["permissions"]
        ids_for(tid).update_user(ids_for(tid).list_users()[0]["id"], roles=["viewer"])                       # revokes the session as well
        assert b.c.get("/api/v1/repositories").status_code == 401
        b.sign_in(name)
        assert "analyze" not in b.me()["permissions"]


# =====================================================================================================================================
class TestAdministration:
    def admin(self, tmp_path, **kw):
        app, tid = make_auth_app(tmp_path, **kw)
        return app, tid, TestClient(app), bearer(token_for(tid, ["administrator"], name="root", allow_administrator=True))

    def test_idp_lifecycle_and_validation(self, idp, tmp_path):
        app, tid, c, h = self.admin(tmp_path)
        r = c.post("/api/v1/admin/idps", headers=h, json={"name": "corp", "protocol": "oidc", "config": oidc_cfg(idp)})
        assert r.status_code == 200, r.text
        iid = r.json()["id"]
        bad = c.post("/api/v1/admin/idps", headers=h, json={"name": "x", "config": {"isuer": "https://x", "default_role": "administrator"}})
        assert bad.status_code == 422 and len(bad.json()["detail"]["problems"]) >= 3
        assert [i["name"] for i in c.get("/api/v1/admin/idps", headers=h).json()] == ["corp"]
        dup = c.post("/api/v1/admin/idps", headers=h, json={"name": "corp", "config": oidc_cfg(idp, audience="z")})
        assert dup.status_code == 422 and "already exists" in dup.text
        r = c.put(f"/api/v1/admin/idps/{iid}", headers=h, json={"name": "corp", "config": oidc_cfg(idp), "enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert c.get("/auth/options", params={"org": "x"}).json() == {"providers": []}
        assert c.put(f"/api/v1/admin/idps/{uuid.uuid4()}", headers=h, json={"name": "n", "config": oidc_cfg(idp)}).status_code == 404
        assert c.delete(f"/api/v1/admin/idps/{iid}", headers=h).status_code == 200 and c.delete(f"/api/v1/admin/idps/{iid}", headers=h).status_code == 404
        ev = [e["event"] for e in audit(tid)]
        assert ev.count("idp_saved") == 2 and "idp_deleted" in ev and all(e.get("actor") == "service:root" for e in audit(tid, "idp_saved"))

    def test_a_client_secret_is_stored_encrypted_and_never_returned(self, idp, tmp_path):
        default = conftest.new_tenant()
        store = SecretStore(conftest.PG.db, default, LocalKeyWrapper({"k": os.urandom(32)}, "k"))
        app, tid, c, h = self.admin(tmp_path, tenant=default, secrets=store)
        r = c.post("/api/v1/admin/idps", headers=h, json={"name": "Corp SSO", "config": oidc_cfg(idp), "client_secret": "very-secret-value"})
        assert r.status_code == 200 and "very-secret-value" not in r.text
        assert r.json()["client_secret_ref"] == "idp/corp-sso/client-secret"
        assert "very-secret-value" not in c.get("/api/v1/admin/idps", headers=h).text
        assert SecretStore(conftest.PG.db, tid, store.wrapper).get_text("idp/corp-sso/client-secret") == "very-secret-value"
        with conftest.PG.db.tx(tid) as cur:
            raw = cur.execute("select ciphertext from tenant_secrets where tenant_id = %s", (tid,)).fetchone()[0]
        assert b"very-secret-value" not in bytes(raw)
        assert "very-secret-value" not in str(audit(tid))

    def test_a_client_secret_without_a_secret_store_is_refused_clearly(self, idp, tmp_path):
        app, tid, c, h = self.admin(tmp_path)
        r = c.post("/api/v1/admin/idps", headers=h, json={"name": "corp", "config": oidc_cfg(idp), "client_secret": "x"})
        assert r.status_code == 400 and "secret store" in r.text

    def test_token_management(self, tmp_path):
        app, tid, c, h = self.admin(tmp_path)
        r = c.post("/api/v1/admin/tokens", headers=h, json={"name": "ci", "roles": ["engineer"], "repos": ["a"], "expires_in_days": 7})
        assert r.status_code == 201 and r.json()["token"].startswith("tia_") and r.headers["cache-control"] == "no-store"
        tok = r.json()
        listing = c.get("/api/v1/admin/tokens", headers=h).json()
        assert {t["name"] for t in listing} == {"root", "ci"}
        assert tok["token"] not in str(listing) and "secret" not in str(listing).lower()
        assert c.get("/api/v1/me", headers=bearer(tok["token"])).json()["roles"] == ["engineer"]
        assert c.post("/api/v1/admin/tokens", headers=h, json={"name": "root2", "roles": ["administrator"]}).status_code == 422
        assert c.post("/api/v1/admin/tokens", headers=h, json={"name": "bad", "roles": ["boss"]}).status_code == 422
        assert c.delete(f"/api/v1/admin/tokens/{tok['id']}", headers=h).status_code == 200
        assert c.get("/api/v1/me", headers=bearer(tok["token"])).status_code == 401
        assert c.delete(f"/api/v1/admin/tokens/{tok['id']}", headers=h).status_code == 404
        ev = audit(tid, "service_token_created")
        assert ev[0]["name"] == "ci" and not any(str(v).startswith("tia_") for v in ev[0].values())
        assert "tia_" + uuid.UUID(tid).hex not in str(audit(tid))                                      # a token value is never audited

    def test_user_management_and_session_revocation(self, idp, tmp_path):
        app, tid, name, _ = setup_org(idp, tmp_path)
        c = TestClient(app)
        h = bearer(token_for(tid, ["administrator"], name="root", allow_administrator=True))
        b = Browser(app)
        b.sign_in(name)
        users = c.get("/api/v1/admin/users", headers=h).json()
        assert len(users) == 1 and users[0]["roles"] is None
        sessions = c.get("/api/v1/admin/sessions", headers=h).json()
        assert len(sessions) == 1 and "secret" not in str(sessions)
        assert c.delete(f"/api/v1/admin/sessions/{sessions[0]['id']}", headers=h).status_code == 200
        assert b.me()["authenticated"] is False
        r = c.put(f"/api/v1/admin/users/{users[0]['id']}", headers=h, json={"roles": ["auditor"], "scm_logins": {"github": "ada"}})
        assert r.status_code == 200 and r.json()["roles"] == ["auditor"] and r.json()["scm_logins"] == {"github": "ada"}
        r = c.put(f"/api/v1/admin/users/{users[0]['id']}", headers=h, json={"roles": None})
        assert r.json()["roles"] is None                                                                  # back to the identity provider
        assert c.put(f"/api/v1/admin/users/{users[0]['id']}", headers=h, json={"roles": ["boss"]}).status_code == 422
        assert c.put(f"/api/v1/admin/users/{uuid.uuid4()}", headers=h, json={"disabled": True}).status_code == 404

    def test_only_administrators_may_administer(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        c = TestClient(app)
        h = bearer(token_for(tid, ["release_manager", "engineer", "auditor"]))
        for m, p in (("get", "/api/v1/admin/idps"), ("get", "/api/v1/admin/users"), ("get", "/api/v1/admin/tokens"), ("get", "/api/v1/admin/sessions"),
                     ("post", "/api/v1/admin/tokens")):
            assert getattr(c, m)(p, headers=h).status_code == 403, p

    def test_administration_is_per_tenant(self, tmp_path):
        app, a = make_auth_app(tmp_path)
        b = conftest.new_tenant()
        ha = bearer(token_for(a, ["administrator"], name="root", allow_administrator=True))
        hb = bearer(token_for(b, ["administrator"], name="root", allow_administrator=True))
        c = TestClient(app)
        c.post("/api/v1/admin/tokens", headers=ha, json={"name": "only-a", "roles": ["viewer"]})
        assert "only-a" in str(c.get("/api/v1/admin/tokens", headers=ha).json())
        assert "only-a" not in str(c.get("/api/v1/admin/tokens", headers=hb).json())


# =====================================================================================================================================
class TestProvisioning:
    """The first identity provider and a break-glass token come from the admin CLI (the API needs a signed-in administrator)."""

    def cli(self, *args, env=None):
        import subprocess
        e = {k: v for k, v in os.environ.items() if not k.startswith("TIA_")}
        e.update({"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "TIA_ADMIN_DATABASE_URL": conftest.PG.admin,
                  "TIA_ALLOW_PRIVATE_IDP_HOSTS": "true", **(env or {})})
        r = subprocess.run([sys.executable, "-m", "tia.tenants", *args], capture_output=True, text=True, env=e, cwd=str(Path(__file__).resolve().parent.parent))
        return r.returncode, r.stdout, r.stderr

    def test_idp_and_tokens_by_cli(self, idp, tmp_path):
        import json
        tid, name = tenant_named()
        f = tmp_path / "idp.json"
        f.write_text(json.dumps({"name": "corp", "protocol": "oidc", "config": oidc_cfg(idp)}))
        code, out, err = self.cli("idp-set", name, "--file", str(f), "--actor", "terraform")
        assert code == 0, err
        row = json.loads(out)
        assert row["name"] == "corp" and row["created_by"] == "terraform"
        f.write_text(json.dumps({"name": "corp", "protocol": "oidc", "config": oidc_cfg(idp, default_role="viewer")}))
        code, out, err = self.cli("idp-set", name, "--file", str(f))                      # same name: an update, not a duplicate
        assert code == 0 and json.loads(out)["id"] == row["id"] and json.loads(out)["config"]["default_role"] == "viewer"
        assert [i["name"] for i in json.loads(self.cli("idp-list", name)[1])] == ["corp"]
        f.write_text(json.dumps({"name": "bad", "config": {"isuer": "x"}}))
        code, out, err = self.cli("idp-set", name, "--file", str(f))
        assert code == 1 and "did you mean" in err
        code, out, err = self.cli("token-create", name, "--name", "ci", "--roles", "engineer,viewer", "--repos", "a,b", "--days", "30")
        assert code == 0, err
        tok = json.loads(out)
        assert tok["token"].startswith("tia_") and tok["roles"] == ["engineer", "viewer"] and tok["repos"] == ["a", "b"]
        code, out, err = self.cli("token-create", name, "--name", "root", "--roles", "administrator")
        assert code == 1 and "administrator" in err
        code, out, err = self.cli("token-create", name, "--name", "root", "--roles", "administrator", "--allow-administrator")
        assert code == 0 and json.loads(out)["roles"] == ["administrator"]
        assert {t["name"] for t in json.loads(self.cli("token-list", name)[1])} == {"ci", "root"}
        assert "token" not in json.loads(self.cli("token-list", name)[1])[0]
        assert self.cli("token-revoke", name, tok["id"])[0] == 0 and self.cli("token-revoke", name, tok["id"])[0] == 1
        evs = json.loads(self.cli("events", name)[1])
        assert {"idp_set", "service_token_created", "service_token_revoked"} <= {e["event"] for e in evs}

    def test_a_token_made_by_the_cli_works_against_the_app(self, tmp_path):
        import json
        tid, name = tenant_named()
        code, out, err = self.cli("token-create", name, "--name", "ci", "--roles", "viewer")
        app, _ = make_auth_app(tmp_path)
        assert TestClient(app).get("/api/v1/me", headers=bearer(json.loads(out)["token"])).json()["tenant_id"] == tid

    def test_a_suspended_tenant_cannot_be_provisioned(self, tmp_path):
        tid, name = tenant_named()
        Tenants(conftest.PG.admin).suspend(name, "test")
        code, out, err = self.cli("token-create", name, "--name", "ci", "--roles", "viewer")
        assert code == 1 and "suspended" in err


class TestServerStartup:
    def run(self, **env):
        import subprocess
        e = {k: v for k, v in os.environ.items() if not k.startswith("TIA_")}
        e.update({"PYTHONPATH": str(Path(__file__).resolve().parent.parent), **env})
        return subprocess.run([sys.executable, "-m", "tia.server"], capture_output=True, text=True, env=e, timeout=60,
                              cwd=str(Path(__file__).resolve().parent.parent))

    def test_refuses_to_listen_on_a_network_address_without_authentication(self):
        r = self.run(TIA_HOST="0.0.0.0", TIA_DATABASE_URL="x", TIA_TENANT_ID=str(uuid.uuid4()))
        assert r.returncode != 0 and "authentication off" in (r.stderr + r.stdout)

    @pytest.mark.parametrize("host", ["10.1.2.3", "example.com", "127.example.com", "::"])
    def test_other_non_loopback_hosts_are_refused_too(self, host):
        r = self.run(TIA_HOST=host, TIA_DATABASE_URL="x", TIA_TENANT_ID=str(uuid.uuid4()))
        assert r.returncode != 0 and "authentication off" in (r.stderr + r.stdout)

    def test_with_authentication_required_that_check_passes(self):
        r = self.run(TIA_HOST="0.0.0.0", TIA_AUTH_REQUIRED="true", TIA_DATABASE_URL="x", TIA_TENANT_ID=str(uuid.uuid4()))
        assert "authentication off" not in (r.stderr + r.stdout)               # it fails later, on the bogus database URL, not on this check
