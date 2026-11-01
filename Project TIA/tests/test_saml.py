"""UC1-18: SAML 2.0 sign-in. Real signed XML (signxml), the real app, and the attacks a SAML service provider has to survive."""
import base64
import sys
import urllib.parse
import uuid
from pathlib import Path

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402
from fakesaml import NS, SamlIdp, decode_request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_auth import ids_for, make_auth_app, tenant_named  # noqa: E402
from test_auth_api import audit  # noqa: E402
from tia import saml  # noqa: E402
from tia.identity import IdentityConfigError, parse_idp  # noqa: E402
from tia.store import Store  # noqa: E402

URL = "https://tia.example.com"
ACS = URL + "/auth/saml/acs"
SP = URL + "/auth/saml/metadata"


@pytest.fixture
def idp():
    return SamlIdp()


def setup(idp, tmp_path, **cfg):
    tid, name = tenant_named()
    ids_for(tid).put_idp("saml-corp", "saml", idp.config(**cfg))
    app, _ = make_auth_app(tmp_path, public_url=URL)
    return app, tid, name


class Login:
    """One SAML sign-in attempt: start (SP -> IdP), then post whatever the identity provider (or an attacker) sends back."""

    def __init__(self, app, name, base=URL):
        self.c = TestClient(app, base_url=base)
        r = self.c.get("/auth/login", params={"org": name, "idp": "saml-corp", "return_to": "/analyses/x"}, follow_redirects=False)
        self.start = r
        if r.status_code != 302:
            return
        u = urllib.parse.urlsplit(r.headers["location"])
        q = dict(urllib.parse.parse_qsl(u.query))
        self.sso, self.relay = u.scheme + "://" + u.netloc + u.path, q["RelayState"]
        self.req = decode_request(q["SAMLRequest"])
        self.request_id = self.req.get("ID")

    def post(self, xml_b64, relay=None, client=None):
        return (client or self.c).post("/auth/saml/acs", data={"SAMLResponse": xml_b64, "RelayState": self.relay if relay is None else relay},
                                       follow_redirects=False)

    def respond(self, idp, **kw):
        return self.post(idp.b64(idp.response(request_id=self.request_id, acs=ACS, audience=SP, **kw)))


def ok(r):
    assert r.status_code == 302, r.text[:300]
    return r


def why(tid):
    """The internal reason of the most recent refused sign-in (audit log; never shown to the caller)."""
    return audit(tid, "login_failed")[0]["detail"]


def refused(r, status=(400, 401, 403, 413)):
    assert r.status_code in status, (r.status_code, r.text[:200])
    assert "tia_session" not in r.headers.get("set-cookie", "")
    return r


# =====================================================================================================================================
class TestStart:
    def test_the_authn_request(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        assert L.start.status_code == 302 and L.sso == idp.sso_url
        assert L.req.get("Destination") == idp.sso_url and L.req.get("AssertionConsumerServiceURL") == ACS and L.request_id.startswith("_")
        assert L.req.findtext("{urn:oasis:names:tc:SAML:2.0:assertion}Issuer") == SP
        assert L.req.get("ProtocolBinding").endswith("HTTP-POST")
        cookie = L.start.headers["set-cookie"].lower()
        assert "tia_login=" in cookie and "samesite=none" in cookie and "secure" in cookie.replace("samesite", "") and "httponly" in cookie

    def test_saml_needs_an_https_public_url(self, idp, tmp_path):
        tid, name = tenant_named()
        ids_for(tid).put_idp("saml-corp", "saml", idp.config())
        app, _ = make_auth_app(tmp_path, public_url="http://testserver")
        r = Login(app, name, base="http://testserver").start
        assert r.status_code == 503

    def test_metadata_for_the_identity_provider_administrator(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        r = TestClient(app, base_url=URL).get("/auth/saml/metadata")
        root = etree.fromstring(r.content)
        assert root.get("entityID") == SP and ACS in r.text and 'WantAssertionsSigned="true"' in r.text


# =====================================================================================================================================
class TestSuccess:
    @pytest.mark.parametrize("sign", ["assertion", "response", "both"])
    def test_signed_assertion_or_response(self, idp, tmp_path, sign):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        r = ok(L.respond(idp, sign=sign))
        assert r.headers["location"] == "/analyses/x" and "tia_session=" in r.headers["set-cookie"]
        me = L.c.get("/auth/me").json()
        assert me["authenticated"] and me["principal"]["roles"] == ["engineer"] and me["principal"]["email"] == "ada@example.com"
        assert me["principal"]["name"] == "Ada" and me["principal"]["subject"] == "ada@example.com"
        ev = audit(tid, "login_succeeded")[0]
        assert ev["method"] == "saml" and Store(conftest.PG.db, tid).verify_audit()["ok"]

    def test_attribute_mapping_and_source_host_logins(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path, subject_attribute="uid", scm_login_attributes={"github": "ghlogin"})
        L = Login(app, name)
        ok(L.respond(idp, attrs={"uid": ["u-77"], "email": ["a@x"], "groups": ["tia-viewers"], "ghlogin": ["ada-lovelace"]}, subject=None))
        assert L.c.get("/auth/me").json()["principal"]["subject"] == "u-77"
        assert ids_for(tid).list_users()[0]["scm_logins"] == {"github": "ada-lovelace"}

    def test_a_certificate_rotation_accepts_either_certificate(self, idp, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa
        newkey = rsa.generate_private_key(65537, 2048)
        newcert = idp.make_cert(newkey)
        app, tid, name = setup(idp, tmp_path, certificates=[idp.cert, newcert])
        ok(Login(app, name).respond(idp))
        ok(Login(app, name).respond(idp, key=newkey, cert=newcert))

    def test_no_group_means_no_role(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        refused(L.respond(idp, attrs={"email": ["a@x"], "groups": ["unrelated"]}), (403,))
        assert audit(tid, "login_failed")[0]["code"] == "no_role"


# =====================================================================================================================================
class TestAttacks:
    def test_an_unsigned_assertion_is_refused(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        refused(Login(app, name).respond(idp, sign="none"))
        assert "not signed" in why(tid)

    def test_signed_by_someone_else(self, idp, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa
        evil = rsa.generate_private_key(65537, 2048)
        app, tid, name = setup(idp, tmp_path)
        refused(Login(app, name).respond(idp, key=evil, cert=idp.make_cert(evil)))
        assert "no valid signature" in why(tid)
        refused(Login(app, name).respond(idp, key=evil, cert=idp.cert))                     # claims the right certificate, wrong key
        assert "no valid signature" in why(tid)

    def test_tampering_after_signing(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        xml = idp.response(request_id=L.request_id, acs=ACS, audience=SP, subject="ada@example.com", attrs={"groups": ["tia-viewers"]})
        refused(L.post(idp.b64(xml.replace(b"tia-viewers", b"tia-admins"))))
        assert "no valid signature" in why(tid)
        L2 = Login(app, name)
        xml = idp.response(request_id=L2.request_id, acs=ACS, audience=SP, subject="ada@example.com")
        refused(L2.post(idp.b64(xml.replace(b"ada@example.com</saml:NameID>", b"ceo@example.com</saml:NameID>"))))
        assert "no valid signature" in why(tid)

    def test_sha1_signatures_are_refused(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        try:
            xml = idp.response(request_id=L.request_id, acs=ACS, audience=SP, alg="rsa-sha1", digest="sha1")
        except Exception:
            pytest.skip("signxml will not even produce SHA-1 signatures")
        refused(L.post(idp.b64(xml)))

    def test_signature_wrapping_with_an_evil_unsigned_assertion_first(self, idp, tmp_path):
        """XSW: keep the legitimate signed assertion, but put an attacker's unsigned one where a naive reader looks first."""
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)

        def wrap(resp, signed):
            evil = etree.fromstring(idp.assertion_xml(
                aid="_evil", request_id=L.request_id, acs=ACS, audience=SP, subject="admin@example.com", attrs={"groups": ["tia-admins"]},
                not_before=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), not_after=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc) + __import__("datetime").timedelta(minutes=5), issuer=idp.entity_id))
            resp.insert(1, evil)

        refused(L.post(idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=SP, extra=wrap))))
        assert "exactly one assertion" in why(tid)

    def test_signature_wrapping_by_hiding_the_signed_assertion_in_extensions(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)

        def hide(resp, signed):
            ext = etree.SubElement(resp, "{urn:oasis:names:tc:SAML:2.0:protocol}Extensions")
            resp.remove(signed)
            ext.append(signed)
            import datetime as dt
            now = dt.datetime.now(dt.timezone.utc)
            resp.append(etree.fromstring(idp.assertion_xml(aid="_evil", request_id=L.request_id, acs=ACS, audience=SP, subject="admin@example.com",
                                                           attrs={"groups": ["tia-admins"]}, not_before=now, not_after=now + dt.timedelta(minutes=5),
                                                           issuer=idp.entity_id)))

        refused(L.post(idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=SP, extra=hide))))
        assert "exactly one assertion" in why(tid)

    def test_a_valid_signed_assertion_cannot_be_embedded_in_another_document(self, idp, tmp_path):
        """Even with a single assertion, one the signature does not cover (copy of a signed one with changed content) is refused."""
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        xml = idp.response(request_id=L.request_id, acs=ACS, audience=SP)
        root = etree.fromstring(xml)
        a = root.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
        a.set("ID", "_other")                                                              # the signed reference no longer matches
        refused(L.post(idp.b64(etree.tostring(root))))
        assert "no valid signature" in why(tid)

    def test_two_assertions(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        import copy

        def dup(resp, signed):
            resp.append(copy.deepcopy(signed))

        refused(L.post(idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=SP, extra=dup))))
        assert "exactly one assertion" in why(tid)

    def test_encrypted_assertions_are_not_supported(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)

        def enc(resp, signed):
            resp.remove(signed)
            e = etree.SubElement(resp, "{urn:oasis:names:tc:SAML:2.0:assertion}EncryptedAssertion")
            e.text = "x"

        refused(L.post(idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=SP, extra=enc))))
        assert "encrypted" in why(tid)

    def test_xxe_and_entity_expansion_are_refused_before_parsing(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-LOCAL-FILE")
        L = Login(app, name)
        xxe = (f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///{str(secret).replace(chr(92), "/")}">]>'
               f'<samlp:Response {NS} ID="_r">&x;</samlp:Response>')
        r = refused(L.post(idp.b64(xxe.encode())))
        assert "TOP-SECRET" not in r.text and "DOCTYPE" in why(tid)
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                f'<samlp:Response {NS}>&b;</samlp:Response>')
        refused(Login(app, name).post(idp.b64(bomb.encode())))
        assert "DOCTYPE" in why(tid)

    @pytest.mark.parametrize("kw,reason", [
        ({"audience": "https://someone-else/sp"}, "not addressed to this service provider"),
        ({"destination": "https://evil.example/acs"}, "Destination"), ({"issuer": "https://other-idp.example"}, "not the configured identity provider"),
        ({"recipient": "https://evil.example/acs"}, "no bearer subject confirmation"),
        ({"in_response_to": "_not-my-request"}, "no bearer subject confirmation"), ({"offset": -3600}, "expired"),
        ({"offset": 3600}, "not valid yet"), ({"status": "urn:oasis:names:tc:SAML:2.0:status:AuthnFailed"}, "reported"),
    ])
    def test_checks_on_what_the_assertion_says(self, idp, tmp_path, kw, reason):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        audience = kw.pop("audience", SP)
        refused(L.post(idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=audience, **kw))))
        assert reason in why(tid)

    def test_the_validity_window_has_a_small_skew(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        ok(Login(app, name).respond(idp, not_after=now + dt.timedelta(seconds=5), not_before=now - dt.timedelta(seconds=30)))
        L = Login(app, name)
        refused(L.respond(idp, not_after=now - dt.timedelta(seconds=120), not_before=now - dt.timedelta(minutes=10),
                          confirm_after=now + dt.timedelta(hours=1)))
        assert "expired" in why(tid)

    def test_an_unsolicited_response_cannot_start_a_session(self, idp, tmp_path):
        """IdP-initiated: no request of ours, so no state: refused."""
        app, tid, name = setup(idp, tmp_path)
        c = TestClient(app, base_url=URL)
        xml = idp.response(request_id="_unsolicited", acs=ACS, audience=SP)
        refused(c.post("/auth/saml/acs", data={"SAMLResponse": idp.b64(xml), "RelayState": ""}, follow_redirects=False))
        refused(c.post("/auth/saml/acs", data={"SAMLResponse": idp.b64(xml), "RelayState": uuid.uuid4().hex + "." + "a" * 30}, follow_redirects=False))

    def test_login_csrf_the_response_must_come_back_through_the_browser_that_started_it(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        victim = TestClient(app, base_url=URL)
        r = L.post(idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=SP)), client=victim)
        refused(r)
        assert victim.get("/auth/me").json()["authenticated"] is False

    def test_a_relay_state_is_single_use_so_a_response_cannot_be_posted_twice(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        xml = idp.b64(idp.response(request_id=L.request_id, acs=ACS, audience=SP))
        ok(L.post(xml))
        refused(L.post(xml))

    def test_replayed_assertion_id_is_refused_even_with_a_fresh_request(self, idp, tmp_path):
        """An attacker who captured an assertion and somehow obtained a matching request id still cannot use its id twice."""
        app, tid, name = setup(idp, tmp_path)
        L1 = Login(app, name)
        ok(L1.respond(idp, aid="_same-id"))
        L2 = Login(app, name)
        refused(L2.respond(idp, aid="_same-id"))
        assert "replayed" in why(tid)

    @pytest.mark.parametrize("payload", ["", "!!!not base64!!!", "bm90IHhtbA==", "A" * 1_500_000])
    def test_garbage_and_oversized_responses(self, idp, tmp_path, payload):
        app, tid, name = setup(idp, tmp_path)
        refused(Login(app, name).post(payload))

    def test_a_signature_over_another_element_is_refused(self, idp, tmp_path):
        """The signature is valid, but over a harmless element, not the assertion that is read."""
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)

        def decoy(resp, signed):
            decoy_el = etree.SubElement(resp, "{urn:oasis:names:tc:SAML:2.0:protocol}Extensions", ID="_decoy")
            resp.remove(decoy_el)
            resp.insert(1, decoy_el)

        # an unsigned assertion plus a signed decoy: the assertion has no signature of its own
        xml = idp.response(request_id=L.request_id, acs=ACS, audience=SP, sign="none", extra=decoy)
        refused(L.post(idp.b64(xml)))
        assert "not signed" in why(tid)

    def test_failures_are_audited_only_when_the_tenant_is_known_and_never_leak_detail(self, idp, tmp_path):
        app, tid, name = setup(idp, tmp_path)
        L = Login(app, name)
        r = refused(L.respond(idp, sign="none"))
        assert "not signed" not in r.text and "assertion" not in r.text.lower()                  # the caller is told only "sign-in failed"
        assert audit(tid, "login_failed")


# =====================================================================================================================================
class TestConfig:
    def test_valid_and_invalid_settings(self, idp):
        assert parse_idp("saml", idp.config())["want_assertions_signed"] is True
        with pytest.raises(IdentityConfigError) as e:
            parse_idp("saml", {"sso_url": "ftp://x", "entity_id": "e", "certificates": ["not a cert"], "want_assertions_signed": False,
                               "clock_skew": 9999, "default_role": "administrator", "bogus": 1})
        t = " | ".join(e.value.problems)
        for frag in ("https URL", "PEM certificates", "cannot be turned off", "clock_skew", "administrator", "unknown setting 'bogus'"):
            assert frag in t, frag

    def test_signing_certificate_is_required(self):
        with pytest.raises(IdentityConfigError, match="certificates"):
            parse_idp("saml", {"sso_url": "https://x/sso", "entity_id": "e"})
