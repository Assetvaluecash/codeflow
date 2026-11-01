"""UC1-18 / UC1-19: single sign-in (OIDC), service-account tokens, roles, tenant routing, sessions.

Real PostgreSQL (row-level security), a real HTTP identity provider on loopback (tests/fakeidp.py), the real app through TestClient.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakeidp import FakeIdp, Key  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tia import authapi, jwtverify, netsafe, rbac  # noqa: E402
from tia.api import create_app  # noqa: E402
from tia.credentials import LocalKeyWrapper  # noqa: E402
from tia.identity import (AuthError, IdentityConfigError, IdentityStore, Principal, parse_idp, parse_service_token, route_issuer,  # noqa: E402
                          safe_return_to, login_options)
from tia.secretstore import SecretStore  # noqa: E402

PUBLIC_URL = "http://testserver"


@pytest.fixture(autouse=True)
def loopback_idp(monkeypatch):
    monkeypatch.setenv("TIA_ALLOW_PRIVATE_IDP_HOSTS", "true")          # the fake provider is on 127.0.0.1


@pytest.fixture
def idp():
    p = FakeIdp()
    yield p
    p.close()


def tenant_named():
    name = f"org-{uuid.uuid4().hex[:10]}"
    return conftest.PG.tenants.ensure(name), name


def ids_for(tid) -> IdentityStore:
    return IdentityStore(conftest.PG.db, tid)


def oidc_cfg(idp, **kw):
    return {"issuer": idp.issuer, "audience": idp.client_id, "groups_claim": "groups",
            "role_map": {"tia-admins": "administrator", "tia-engineers": "engineer", "tia-viewers": "viewer", "tia-release": "release_manager",
                         "tia-audit": "auditor"}, **kw}


def make_auth_app(tmp_path, tenant=None, public_url=PUBLIC_URL, secrets=None, **kw):
    tenant = tenant or conftest.new_tenant()
    app = create_app(conftest.PG.db, tenant, str(tmp_path / "ws"), allow_local=True, secrets=secrets,
                     auth=authapi.AuthSettings(required=True, public_url=public_url), **kw)
    return app, tenant


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def token_for(tid, roles, name=None, repos="*", **kw):
    return ids_for(tid).create_token(name or f"t-{uuid.uuid4().hex[:6]}", roles, repos, **kw)["token"]


# =====================================================================================================================================
# JWT verification
# =====================================================================================================================================
class TestJwt:
    def jwks(self, idp):
        return jwtverify.Jwks(lambda: {"keys": [k.jwk for k in idp.keys]})

    def check(self, idp, tok, **kw):
        return jwtverify.verify(tok, self.jwks(idp), issuer=idp.issuer, audience=idp.client_id, **kw)

    def test_valid_tokens_of_each_algorithm(self, idp):
        ec, ps = Key("EC", "e1"), Key("RSA", "p1", alg="PS256")
        idp.keys += [ec, ps]
        for alg, key in (("RS256", idp.keys[0]), ("PS256", ps), ("ES256", ec)):
            assert self.check(idp, idp.mint(alg=alg, key=key)).claims["sub"] == "u-1"

    @pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "....", "x" * 20000])
    def test_malformed_tokens(self, idp, bad):
        with pytest.raises(jwtverify.TokenError):
            self.check(idp, bad)

    def test_alg_none_is_refused(self, idp):
        tok = idp.mint(alg="none", sign=False, header={"alg": "none"})
        with pytest.raises(jwtverify.TokenError, match="not accepted"):
            self.check(idp, tok)

    def test_hmac_with_the_public_key_as_secret_is_refused(self, idp):
        """The classic algorithm-confusion attack: HS256 signed with the (public) RSA key material."""
        import hashlib
        import hmac
        h = jwtverify.b64e(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
        c = jwtverify.b64e(json.dumps({"iss": idp.issuer, "aud": idp.client_id, "sub": "x", "exp": int(time.time()) + 60}).encode())
        sig = jwtverify.b64e(hmac.new(json.dumps(idp.keys[0].jwk).encode(), f"{h}.{c}".encode(), hashlib.sha256).digest())
        with pytest.raises(jwtverify.TokenError, match="not accepted"):
            self.check(idp, f"{h}.{c}.{sig}")

    def test_wrong_signature(self, idp):
        other = Key("RSA", "k1")                                       # same kid, different key
        with pytest.raises(jwtverify.TokenError, match="signature"):
            self.check(idp, idp.mint(key=other))

    def test_tampered_payload(self, idp):
        h, p, s = idp.mint().split(".")
        claims = json.loads(jwtverify.b64d(p))
        claims["sub"] = "admin"
        with pytest.raises(jwtverify.TokenError, match="signature"):
            self.check(idp, ".".join([h, jwtverify.b64e(json.dumps(claims).encode()), s]))

    def test_embedded_key_headers_are_ignored(self, idp):
        attacker = Key("RSA", "k1")
        tok = idp.mint(key=attacker, header={"jwk": attacker.jwk, "jku": "http://evil/jwks", "x5u": "http://evil/cert"})
        with pytest.raises(jwtverify.TokenError, match="signature"):
            self.check(idp, tok)

    def test_unknown_critical_header(self, idp):
        with pytest.raises(jwtverify.TokenError, match="critical"):
            self.check(idp, idp.mint(header={"crit": ["exp2"], "exp2": 1}))

    @pytest.mark.parametrize("over,msg", [
        ({"exp": int(time.time()) - 3600}, "expired"), ({"nbf": int(time.time()) + 3600}, "not valid yet"),
        ({"iss": "https://other"}, "issuer"), ({"aud": "someone-else"}, "audience"),
        ({"iat": int(time.time()) + 3600}, "future"), ({"exp": None}, "no expiry"), ({"sub": None}, "subject"),
    ])
    def test_claim_checks(self, idp, over, msg):
        with pytest.raises(jwtverify.TokenError, match=msg):
            self.check(idp, idp.mint(**over))

    def test_exp_must_be_a_number(self, idp):
        with pytest.raises(jwtverify.TokenError, match="expiry"):
            self.check(idp, idp.mint(exp="9999999999"))
        with pytest.raises(jwtverify.TokenError, match="expiry"):
            self.check(idp, idp.mint(exp=True))

    def test_clock_leeway_is_small_and_explicit(self, idp):
        assert self.check(idp, idp.mint(exp=int(time.time()) - 30)).claims["sub"]             # inside the 60 s leeway
        with pytest.raises(jwtverify.TokenError, match="expired"):
            self.check(idp, idp.mint(exp=int(time.time()) - 120))
        with pytest.raises(jwtverify.TokenError, match="expired"):
            self.check(idp, idp.mint(exp=int(time.time()) - 30), leeway=0)

    def test_multiple_audiences_need_the_authorised_party(self, idp):
        tok = idp.mint(aud=[idp.client_id, "other-api"])
        with pytest.raises(jwtverify.TokenError, match="authorised party"):
            self.check(idp, tok)
        assert self.check(idp, idp.mint(aud=[idp.client_id, "other-api"], azp=idp.client_id))

    def test_nonce(self, idp):
        tok = idp.mint(nonce="n1")
        assert self.check(idp, tok, nonce="n1")
        with pytest.raises(jwtverify.TokenError, match="nonce"):
            self.check(idp, tok, nonce="n2")
        with pytest.raises(jwtverify.TokenError, match="nonce"):
            self.check(idp, idp.mint(), nonce="n1")

    def test_max_age(self, idp):
        with pytest.raises(jwtverify.TokenError, match="older"):
            self.check(idp, idp.mint(iat=int(time.time()) - 5000, exp=int(time.time()) + 100), max_age=600)

    def test_small_rsa_keys_are_refused(self, idp):
        weak = Key("RSA", "w1", bits=1024)
        idp.keys.append(weak)
        with pytest.raises(jwtverify.TokenError, match="2048"):
            self.check(idp, idp.mint(key=weak))

    def test_key_type_must_match_algorithm(self, idp):
        ec = Key("EC", "e1")
        idp.keys.append(ec)
        tok = idp.mint(alg="ES256", key=ec, header={"kid": "k1"})                  # claims the RSA key
        with pytest.raises(jwtverify.TokenError):
            self.check(idp, tok)

    def test_key_restricted_to_another_algorithm(self, idp):
        jwk = {**idp.keys[0].jwk, "alg": "RS512"}
        with pytest.raises(jwtverify.TokenError, match="another algorithm"):
            jwtverify.verify(idp.mint(), jwtverify.Jwks(lambda: {"keys": [jwk]}), issuer=idp.issuer, audience=idp.client_id)

    def test_rotation_refetches_once_per_interval(self, idp):
        clock, fetched = [1000.0], []

        def fetch():
            fetched.append(1)
            return {"keys": [k.jwk for k in idp.keys]}

        jwks = jwtverify.Jwks(fetch, min_refresh=30, now=lambda: clock[0])
        jwtverify.verify(idp.mint(), jwks, issuer=idp.issuer, audience=idp.client_id)
        assert len(fetched) == 1
        new = idp.rotate()
        with pytest.raises(jwtverify.TokenError, match="no key"):                  # too soon to refetch: the new key is not trusted yet
            jwtverify.verify(idp.mint(key=new), jwks, issuer=idp.issuer, audience=idp.client_id)
        assert len(fetched) == 1
        clock[0] += 31
        assert jwtverify.verify(idp.mint(key=new), jwks, issuer=idp.issuer, audience=idp.client_id).claims["sub"]
        assert len(fetched) == 2

    def test_unknown_kid_never_causes_a_fetch_storm(self, idp):
        fetched = []
        jwks = jwtverify.Jwks(lambda: fetched.append(1) or {"keys": [k.jwk for k in idp.keys]})
        jwtverify.verify(idp.mint(), jwks, issuer=idp.issuer, audience=idp.client_id)
        for _ in range(20):
            with pytest.raises(jwtverify.TokenError):
                jwtverify.verify(idp.mint(header={"kid": "nope"}), jwks, issuer=idp.issuer, audience=idp.client_id)
        assert len(fetched) <= 2
        fetched.clear()
        for _ in range(20):                                                       # a forged algorithm never reaches the key set at all
            with pytest.raises(jwtverify.TokenError):
                jwtverify.verify(idp.mint(alg="none", sign=False, header={"alg": "none"}), jwks, issuer=idp.issuer, audience=idp.client_id)
        assert not fetched

    def test_provider_with_no_keys(self, idp):
        with pytest.raises(jwtverify.TokenError, match="no keys"):
            jwtverify.verify(idp.mint(), jwtverify.Jwks(lambda: {"keys": []}), issuer=idp.issuer, audience=idp.client_id)

    def test_peek_is_unverified_routing_only(self, idp):
        assert jwtverify.peek(idp.mint(key=Key("RSA")))["iss"] == idp.issuer


# =====================================================================================================================================
# outbound fetches
# =====================================================================================================================================
class TestNetsafe:
    @pytest.mark.parametrize("url", ["http://10.0.0.5/x", "https://10.1.2.3/.well-known", "https://169.254.169.254/latest/meta-data/",
                                     "https://127.0.0.1/", "https://[::1]/", "https://[::ffff:10.0.0.1]/", "https://192.168.1.1/",
                                     "https://100.64.0.1/", "https://0.0.0.0/"])
    def test_private_addresses_are_refused(self, url, monkeypatch):
        monkeypatch.delenv("TIA_ALLOW_PRIVATE_IDP_HOSTS")
        with pytest.raises(netsafe.FetchError):
            netsafe.check_url(url)

    def test_plain_http_is_refused_unless_private_hosts_are_allowed(self, monkeypatch):
        monkeypatch.delenv("TIA_ALLOW_PRIVATE_IDP_HOSTS")
        with pytest.raises(netsafe.FetchError, match="https"):
            netsafe.check_url("http://example.com/")

    @pytest.mark.parametrize("url", ["ftp://example.com/", "https://user:pw@example.com/", "https:///x", "file:///etc/passwd", "gopher://x"])
    def test_unacceptable_urls(self, url):
        with pytest.raises(netsafe.FetchError):
            netsafe.check_url(url)

    def test_redirects_are_not_followed(self, idp):
        status, _ = netsafe.request("GET", idp.base + "/authorize?redirect_uri=http://x/cb&state=s")
        assert status == 302

    def test_get_json_checks_status_and_shape(self, idp):
        assert netsafe.get_json(idp.base + "/jwks")["keys"]
        with pytest.raises(netsafe.FetchError, match="404"):
            netsafe.get_json(idp.base + "/nope")

    def test_unreachable_host(self):
        with pytest.raises(netsafe.FetchError):
            netsafe.get_json("http://127.0.0.1:1/x")

    def test_response_size_is_capped(self, idp, monkeypatch):
        monkeypatch.setattr(netsafe, "MAX_BODY", 50)
        with pytest.raises(netsafe.FetchError, match="too large"):
            netsafe.get_json(idp.base + "/jwks")


# =====================================================================================================================================
# identity-provider configuration
# =====================================================================================================================================
class TestIdpConfig:
    def test_valid_minimal(self, idp):
        c = parse_idp("oidc", {"issuer": idp.issuer, "audience": "c"})
        assert c["discovery"] is True and c["subject_claim"] == "sub" and c["scopes"] == "openid email profile"

    def test_every_problem_at_once_with_did_you_mean(self):
        with pytest.raises(IdentityConfigError) as e:
            parse_idp("oidc", {"isuer": "https://x", "audience": "c", "role_map": {"g": "boss"}, "default_role": "administrator", "client_secret": "s"})
        text = " | ".join(e.value.problems)
        assert "isuer" in text and "did you mean 'issuer'" in text and "'issuer' is required" in text
        assert "unknown role 'boss'" in text and "default_role cannot be 'administrator'" in text and "looks like a secret" in text

    @pytest.mark.parametrize("cfg,msg", [
        ({"issuer": "https://x", "audience": "c", "discovery": False}, "all required"),
        ({"issuer": "https://x", "audience": "c", "scopes": "email"}, "openid"),
        ({"issuer": "https://x", "audience": "c", "allowed_algs": ["none"]}, "allowed_algs"),
        ({"issuer": "https://x", "audience": "c", "allowed_algs": ["HS256"]}, "allowed_algs"),
        ({"issuer": "ftp://x", "audience": "c"}, "https URL"),
        ({"issuer": "https://u:p@x", "audience": "c"}, "https URL"),
        ({"issuer": "https://x", "audience": "c", "groups_claim": "a b"}, "claim name"),
        ({"issuer": "https://x", "audience": "c", "max_token_age": 5}, "max_token_age"),
        ({"issuer": "https://x", "audience": "c", "scm_login_claims": {"github": 1}}, "scm_login_claims"),
        ({"issuer": "https://x", "audience": "c", "role_map": []}, "role_map"),
    ])
    def test_rejections(self, cfg, msg, monkeypatch):
        monkeypatch.delenv("TIA_ALLOW_PRIVATE_IDP_HOSTS")
        with pytest.raises(IdentityConfigError, match=msg):
            parse_idp("oidc", cfg)

    def test_http_issuer_only_when_private_hosts_are_allowed(self, monkeypatch):
        monkeypatch.delenv("TIA_ALLOW_PRIVATE_IDP_HOSTS")
        with pytest.raises(IdentityConfigError, match="https"):
            parse_idp("oidc", {"issuer": "http://idp.example.com", "audience": "c"})
        monkeypatch.setenv("TIA_ALLOW_PRIVATE_IDP_HOSTS", "true")
        assert parse_idp("oidc", {"issuer": "http://127.0.0.1:9", "audience": "c"})

    def test_unknown_protocol(self):
        with pytest.raises(IdentityConfigError, match="protocol"):
            parse_idp("ldap", {})

    def test_names_and_secret_references_are_validated(self, idp):
        ids = ids_for(tenant_named()[0])
        for bad in ("", "-x", "a" * 80, "a/b", "a<b>"):
            with pytest.raises(IdentityConfigError, match="name"):
                ids.put_idp(bad, "oidc", oidc_cfg(idp))
        with pytest.raises(IdentityConfigError, match="client_secret_ref"):
            ids.put_idp("ok", "oidc", oidc_cfg(idp), client_secret_ref="UPPER CASE")

    def test_duplicate_name_and_cross_tenant_issuer_audience(self, idp):
        a, b = ids_for(tenant_named()[0]), ids_for(tenant_named()[0])
        a.put_idp("corp", "oidc", oidc_cfg(idp))
        with pytest.raises(IdentityConfigError, match="already exists"):
            a.put_idp("corp", "oidc", oidc_cfg(idp, audience="other"))
        with pytest.raises(IdentityConfigError, match="issuer and audience"):                # another tenant cannot claim the same pair
            b.put_idp("mine", "oidc", oidc_cfg(idp))
        b.put_idp("mine", "oidc", oidc_cfg(idp, audience="a-different-client"))

    def test_disabled_providers_do_not_route(self, idp):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        row = ids.put_idp("corp", "oidc", oidc_cfg(idp))
        assert route_issuer(conftest.PG.db, idp.issuer, idp.client_id) == [(tid, row["id"])]
        ids.put_idp("corp", "oidc", oidc_cfg(idp), enabled=False, idp_id=row["id"])
        assert route_issuer(conftest.PG.db, idp.issuer, idp.client_id) == []

    def test_suspended_tenants_do_not_route_or_offer_sign_in(self, idp):
        tid, name = tenant_named()
        ids_for(tid).put_idp("corp", "oidc", oidc_cfg(idp))
        assert login_options(conftest.PG.db, name)
        conftest.PG.tenants  # noqa
        from tia.tenants import Tenants
        Tenants(conftest.PG.admin).suspend(name, "test")
        assert route_issuer(conftest.PG.db, idp.issuer, idp.client_id) == [] and login_options(conftest.PG.db, name) == []

    def test_unknown_organisation_has_no_options(self):
        assert login_options(conftest.PG.db, "no-such-org") == []

    def test_claim_paths_and_group_mapping(self):
        from tia.identity import claim_at, roles_from_groups
        assert claim_at({"realm_access": {"roles": ["a"]}}, "realm_access.roles") == ["a"]
        assert claim_at({"a.b": 1}, "a.b") == 1 and claim_at({}, "x.y") is None
        cfg = {"role_map": {"g1": "viewer", "g2": ["engineer", "auditor"], "g3": "boss"}, "default_role": None}
        assert roles_from_groups(cfg, ["g1", "g2", "g3", "zz"]) == ["viewer", "engineer", "auditor"]
        assert roles_from_groups({**cfg, "default_role": "viewer"}, ["zz"]) == ["viewer"]
        assert roles_from_groups(cfg, []) == []


# =====================================================================================================================================
# identity store: tokens, users, sessions, isolation
# =====================================================================================================================================
class TestStore:
    def test_service_token_roundtrip_and_what_is_stored(self):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        t = ids.create_token("ci", ["engineer"], ["a", "b"], 30)
        parsed = parse_service_token(t["token"])
        assert parsed and parsed[0] == tid and parsed[1] == t["id"]
        p = ids.token_principal(parsed[1], parsed[2])
        assert p.kind == "service" and p.roles == {"engineer"} and p.repo_scope == ("a", "b")
        with conftest.PG.db.tx(tid) as c:
            stored = c.execute("select secret_hash from service_tokens where tenant_id = %s", (tid,)).fetchone()[0]
        assert parsed[2] not in stored and len(stored) == 64                    # only a hash is stored, never the secret

    def test_token_failures_are_indistinguishable(self):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        t = ids.create_token("ci", ["viewer"])
        _, tok_id, secret = parse_service_token(t["token"])
        for tid_, s_ in ((tok_id, "x" * 43), (str(uuid.uuid4()), secret)):
            with pytest.raises(AuthError) as e:
                ids.token_principal(tid_, s_)
            assert e.value.code == "invalid_token"

    def test_revoked_and_expired_tokens(self):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        t = ids.create_token("a", ["viewer"])
        _, i, s = parse_service_token(t["token"])
        assert ids.revoke_token(i) and not ids.revoke_token(i)
        with pytest.raises(AuthError, match="invalid_token"):
            ids.token_principal(i, s)
        t2 = ids.create_token("b", ["viewer"])
        _, i2, s2 = parse_service_token(t2["token"])
        with conftest.PG.db.tx(tid) as c:
            c.execute("update service_tokens set expires_at = now() - interval '1 minute' where tenant_id = %s and id = %s", (tid, i2))
        with pytest.raises(AuthError, match="invalid_token"):
            ids.token_principal(i2, s2)

    def test_token_creation_rules(self):
        ids = ids_for(tenant_named()[0])
        for kw, msg in (({"roles": ["administrator"]}, "administrator"), ({"roles": []}, "at least one"), ({"roles": ["boss"]}, "unknown role"),
                        ({"roles": ["viewer"], "name": ""}, "name"), ({"roles": ["viewer"], "repos": "a"}, "repos"),
                        ({"roles": ["viewer"], "expires_in_days": 0}, "expires_in_days")):
            with pytest.raises(IdentityConfigError, match=msg):
                ids.create_token(**{"name": "x", **kw})
        ids.create_token("dup", ["viewer"])
        with pytest.raises(IdentityConfigError, match="already exists"):
            ids.create_token("dup", ["viewer"])
        assert ids.create_token("breakglass", ["administrator"], allow_administrator=True)["roles"] == ["administrator"]
        assert ids.create_token("forever", ["viewer"], expires_in_days=None)["expires_at"] is None

    def test_last_used_is_recorded_without_a_write_per_request(self):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        t = ids.create_token("a", ["viewer"])
        _, i, s = parse_service_token(t["token"])
        assert ids.list_tokens()[0]["last_used_at"] is None
        ids.token_principal(i, s)
        first = ids.list_tokens()[0]["last_used_at"]
        assert first
        ids.token_principal(i, s)
        assert ids.list_tokens()[0]["last_used_at"] == first                      # throttled

    def test_tenant_isolation_of_every_identity_table(self, idp):
        a, b = tenant_named()[0], tenant_named()[0]
        ia, ib = ids_for(a), ids_for(b)
        ia.put_idp("corp", "oidc", oidc_cfg(idp))
        ia.create_token("t", ["viewer"])
        user = ia.upsert_user(None, {"subject": "s", "email": "e", "name": "n"})
        ia.create_session(user, None, ["viewer"], [])
        ia.save_login_state(str(uuid.uuid4()), "n", "v", "/", "b")
        assert ib.list_idps() == [] and ib.list_tokens() == [] and ib.list_users() == [] and ib.list_sessions() == []
        for table in ("tenant_idps", "tenant_users", "auth_sessions", "auth_login_states", "service_tokens", "saml_assertions_seen"):
            with conftest.PG.db.tx(b) as c:
                assert c.execute(f"select count(*) from {table} where tenant_id = %s", (a,)).fetchone()[0] == 0, table
            with conftest.PG.db.plain() as c:
                assert c.execute(f"select count(*) from {table}").fetchone()[0] == 0, table         # no tenant bound: sees nothing
            with conftest.PG.db.tx(b) as c:
                with pytest.raises(Exception):                                                    # and cannot write another tenant's rows
                    c.execute(f"insert into {table} (tenant_id) values (%s)", (a,))

    def test_users_jit_and_admin_edits(self, idp):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        row = ids.put_idp("corp", "oidc", oidc_cfg(idp))
        u = ids.upsert_user(row["id"], {"subject": "s1", "email": "a@x", "name": "A", "scm_logins": {"github": "ada"}})
        again = ids.upsert_user(row["id"], {"subject": "s1", "email": "a2@x", "name": "A", "scm_logins": {}})
        assert again["id"] == u["id"] and again["email"] == "a2@x" and again["scm_logins"] == {"github": "ada"}      # hand-set logins are kept
        assert ids.effective_roles(u, parse_idp("oidc", oidc_cfg(idp)), ["tia-viewers"]) == ["viewer"]
        ids.update_user(u["id"], roles=["auditor"])
        u2 = ids.get_user(u["id"])
        assert ids.effective_roles(u2, parse_idp("oidc", oidc_cfg(idp)), ["tia-admins"]) == ["auditor"]           # explicit assignment wins
        ids.update_user(u["id"], roles=None)
        assert ids.effective_roles(ids.get_user(u["id"]), parse_idp("oidc", oidc_cfg(idp)), ["tia-admins"]) == ["administrator"]
        with pytest.raises(ValueError, match="unknown role"):
            ids.update_user(u["id"], roles=["boss"])
        with pytest.raises(IdentityConfigError, match="scm_logins"):
            ids.update_user(u["id"], scm_logins={"github": "bad login!"})
        assert ids.update_user(str(uuid.uuid4()), disabled=True) is None and ids.get_user("not-a-uuid") is None

    def test_same_subject_in_two_providers_is_two_users(self, idp):
        ids = ids_for(tenant_named()[0])
        r1 = ids.put_idp("one", "oidc", oidc_cfg(idp))
        r2 = ids.put_idp("two", "oidc", oidc_cfg(idp, audience="c2"))
        assert ids.upsert_user(r1["id"], {"subject": "s"})["id"] != ids.upsert_user(r2["id"], {"subject": "s"})["id"]

    def test_session_lifecycle(self, idp):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        user = ids.upsert_user(None, {"subject": "s", "email": "e", "name": "n"})
        cookie, sid = ids.create_session(user, None, ["engineer"], ["g"])
        t, secret = authapi_parse(cookie)
        assert t == tid
        p = ids.session_principal(secret)
        assert p.roles == {"engineer"} and p.csrf and p.kind == "user"
        with conftest.PG.db.tx(tid) as c:
            assert secret not in c.execute("select secret_hash from auth_sessions where tenant_id = %s", (tid,)).fetchone()[0]
        with pytest.raises(AuthError, match="no_session"):
            ids.session_principal("x" * 43)
        ids.update_user(user["id"], roles=["viewer"])                                                      # role change: sessions revoked
        with pytest.raises(AuthError, match="session_revoked"):
            ids.session_principal(secret)

    def test_session_expiry_idle_revocation_and_disabled_user(self):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        user = ids.upsert_user(None, {"subject": "s"})

        def fresh():
            c, sid = ids.create_session(user, None, ["viewer"], [])
            return authapi_parse(c)[1], sid

        sec, sid = fresh()
        with conftest.PG.db.tx(tid) as c:
            c.execute("update auth_sessions set expires_at = now() - interval '1 second' where tenant_id = %s and id = %s", (tid, sid))
        with pytest.raises(AuthError, match="session_expired"):
            ids.session_principal(sec)
        sec, sid = fresh()
        with conftest.PG.db.tx(tid) as c:
            c.execute("update auth_sessions set last_seen_at = now() - interval '2 hours' where tenant_id = %s and id = %s", (tid, sid))
        with pytest.raises(AuthError, match="session_expired"):
            ids.session_principal(sec)
        sec, sid = fresh()
        assert ids.revoke_session(sid) and not ids.revoke_session(sid)
        with pytest.raises(AuthError, match="session_revoked"):
            ids.session_principal(sec)
        sec, sid = fresh()
        with conftest.PG.db.tx(tid) as c:
            c.execute("update tenant_users set disabled = true where tenant_id = %s", (tid,))
        with pytest.raises(AuthError, match="user_disabled"):
            ids.session_principal(sec)

    def test_a_session_secret_of_another_tenant_does_not_work(self):
        a, b = tenant_named()[0], tenant_named()[0]
        user = ids_for(a).upsert_user(None, {"subject": "s"})
        cookie, _ = ids_for(a).create_session(user, None, ["administrator"], [])
        secret = authapi_parse(cookie)[1]
        with pytest.raises(AuthError, match="no_session"):
            ids_for(b).session_principal(secret)

    def test_login_state_is_single_use_bound_to_the_browser_and_capped(self):
        tid = tenant_named()[0]
        ids = ids_for(tid)
        idp_id = str(uuid.uuid4())
        s = ids.save_login_state(idp_id, "nonce", "verifier", "/x", "browser-1").split(".")[1]
        assert ids.take_login_state(s, "browser-2") is None                  # a different browser: refused, and the state is consumed
        assert ids.take_login_state(s, "browser-1") is None
        s = ids.save_login_state(idp_id, "nonce", "verifier", "/x", "browser-1").split(".")[1]
        got = ids.take_login_state(s, "browser-1")
        assert got and got["nonce"] == "nonce" and got["verifier"] == "verifier" and got["return_to"] == "/x"
        assert ids.take_login_state(s, "browser-1") is None                  # replay
        s = ids.save_login_state(idp_id, "n", "v", "/", "b").split(".")[1]
        with conftest.PG.db.tx(tid) as c:
            c.execute("update auth_login_states set expires_at = now() - interval '1 second' where tenant_id = %s", (tid,))
        assert ids.take_login_state(s, "b") is None                          # expired
        ids.MAX_LIVE_STATES = 3
        for _ in range(3):
            ids.save_login_state(idp_id, "n", "v", "/", "b")
        with pytest.raises(AuthError, match="too_many_logins"):
            ids.save_login_state(idp_id, "n", "v", "/", "b")

    @pytest.mark.parametrize("v,expected", [("/", "/"), ("/analyses/abc?x=1", "/analyses/abc?x=1"), ("//evil.com", "/"), ("https://evil.com", "/"),
                                            ("/\\evil.com", "/"), ("javascript:alert(1)", "/"), ("", "/"), (None, "/"), ("/a\nb", "/"),
                                            ("/" + "a" * 600, "/"), ("evil", "/")])
    def test_return_to_is_a_local_path(self, v, expected):
        assert safe_return_to(v) == expected

    def test_saml_assertion_replay_protection(self):
        ids = ids_for(tenant_named()[0])
        from datetime import datetime, timedelta, timezone
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert ids.saml_assertion_once("a1", later) and not ids.saml_assertion_once("a1", later) and ids.saml_assertion_once("a2", later)

    def test_definer_functions_are_the_only_cross_tenant_door(self):
        with conftest.PG.db.plain() as c:
            assert c.execute("select count(*) from tenant_idps").fetchone()[0] == 0
            assert c.execute("select count(*) from tia_idp_by_issuer('x', 'y')").fetchone()[0] == 0
        # the worker role inherits tia_app's grants but nothing of the identity tables is its business; the functions are not public
        import psycopg
        with psycopg.connect(conftest.PG.admin) as c:
            ok = c.execute("select has_function_privilege('public', 'tia_idp_by_issuer(text,text)', 'execute')").fetchone()[0]
            assert ok is False


def authapi_parse(cookie):
    from tia.identity import parse_session_cookie
    return parse_session_cookie(cookie)


# =====================================================================================================================================
# route table
# =====================================================================================================================================
class TestRoutes:
    def test_every_registered_route_is_classified_and_every_entry_exists(self, tmp_path):
        app, _ = make_auth_app(tmp_path)
        registered = set()
        for r in app.routes:
            if getattr(r, "methods", None) and r.path not in rbac.UNLISTED_OK:
                for m in r.methods:
                    if m != "HEAD":
                        registered.add((m, r.path))
        unclassified = sorted(x for x in registered if rbac.classify(*x) is None)
        assert not unclassified, f"routes with no permission in tia/rbac.py: {unclassified}"
        # every route the table lists is real (the table does not drift from the API); SAML routes appear with the SAML provider
        dev, _t = make_dev_app(tmp_path)
        all_routes = {(m, r.path) for r in dev.routes if getattr(r, "methods", None) for m in r.methods}
        assert not sorted(k for k in rbac.ROUTES if k not in all_routes), "rbac.ROUTES lists routes the API does not have"

    def test_permissions_only_name_known_roles(self):
        assert all(r in rbac.ROLES for roles in rbac.PERMISSIONS.values() for r in roles)
        assert {v for v in rbac.ROUTES.values()} - set(rbac.PERMISSIONS) <= {rbac.PUBLIC, rbac.AUTHENTICATED}

    def test_role_matrix(self):
        m = lambda p: sorted(rbac.PERMISSIONS[p])
        assert m("repo_delete") == ["administrator"] and m("admin") == ["administrator"]
        assert "viewer" not in rbac.PERMISSIONS["analyze"] and "auditor" not in rbac.PERMISSIONS["analyze"]
        assert "engineer" not in rbac.PERMISSIONS["config_write"] and "release_manager" in rbac.PERMISSIONS["config_write"]
        assert "engineer" not in rbac.PERMISSIONS["audit_read"] and "auditor" in rbac.PERMISSIONS["audit_read"]
        assert rbac.allows({"viewer", "auditor"}, "audit_read") and not rbac.allows(set(), "read") and not rbac.allows({"viewer"}, "nonexistent")

    def test_unlisted_routes_are_refused_even_to_administrators(self, tmp_path):
        app, tid = make_auth_app(tmp_path)
        app.add_api_route("/api/v1/zzz-new", lambda: {"ok": 1}, methods=["GET"])
        c = TestClient(app)
        r = c.get("/api/v1/zzz-new", headers=bearer(token_for(tid, ["administrator"], allow_administrator=True)))
        assert r.status_code == 403 and r.json()["code"] == "forbidden"


def make_dev_app(tmp_path):
    t = conftest.new_tenant()
    return create_app(conftest.PG.db, t, str(tmp_path / "dev-ws"), allow_local=True), t
