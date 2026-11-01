"""A SAML 2.0 identity provider for tests: builds and signs real responses (and the attacks on them) with signxml."""
from __future__ import annotations

import base64
import datetime as dt
import secrets
import zlib
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

NS = 'xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SamlIdp:
    def __init__(self, entity_id="https://idp.example.com/saml", sso_url="https://idp.example.com/sso"):
        self.entity_id, self.sso_url = entity_id, sso_url
        self.key = rsa.generate_private_key(65537, 2048)
        self.cert = self.make_cert(self.key)

    @staticmethod
    def make_cert(key) -> str:
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.com")])
        now = dt.datetime.now(dt.timezone.utc)
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365)).sign(key, hashes.SHA256()))
        return c.public_bytes(serialization.Encoding.PEM).decode()

    def key_pem(self, key=None) -> bytes:
        return (key or self.key).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    def config(self, **over) -> dict:
        return {"entity_id": self.entity_id, "sso_url": self.sso_url, "certificates": [self.cert], "groups_attribute": "groups",
                "email_attribute": "email", "name_attribute": "displayName",
                "role_map": {"tia-engineers": "engineer", "tia-admins": "administrator", "tia-viewers": "viewer"}, **over}

    # ---- responses -------------------------------------------------------------------------------------------------------------------
    def assertion_xml(self, *, aid, request_id, acs, audience, subject, attrs, not_before, not_after, issuer, recipient=None, in_response_to=None,
                      confirm_after=None) -> str:
        recipient = recipient or acs
        irt = request_id if in_response_to is None else in_response_to
        attr_xml = "".join(
            f'<saml:Attribute Name="{escape(k)}">' + "".join(f"<saml:AttributeValue>{escape(v)}</saml:AttributeValue>" for v in vs) + "</saml:Attribute>"
            for k, vs in attrs.items())
        name_id = f"<saml:NameID>{escape(subject)}</saml:NameID>" if subject else ""
        return (f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{aid}" Version="2.0" IssueInstant="{iso(not_before)}">'
                f"<saml:Issuer>{escape(issuer)}</saml:Issuer><saml:Subject>{name_id}"
                f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData '
                f'NotOnOrAfter="{iso(confirm_after or not_after)}" Recipient="{escape(recipient)}" InResponseTo="{escape(irt)}"/></saml:SubjectConfirmation>'
                f'</saml:Subject><saml:Conditions NotBefore="{iso(not_before)}" NotOnOrAfter="{iso(not_after)}"><saml:AudienceRestriction>'
                f"<saml:Audience>{escape(audience)}</saml:Audience></saml:AudienceRestriction></saml:Conditions>"
                f'<saml:AuthnStatement AuthnInstant="{iso(not_before)}"><saml:AuthnContext><saml:AuthnContextClassRef>'
                "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
                f"<saml:AttributeStatement>{attr_xml}</saml:AttributeStatement></saml:Assertion>")

    def sign(self, el, key=None, cert=None, alg="rsa-sha256", digest="sha256"):
        s = XMLSigner(method=methods.enveloped, signature_algorithm=alg, digest_algorithm=digest, c14n_algorithm=C14N)
        return s.sign(el, key=self.key_pem(key), cert=cert or self.cert, reference_uri="#" + el.get("ID"))

    def response(self, *, request_id, acs, audience, subject="ada@example.com", attrs=None, sign="assertion", offset=0, lifetime=300,
                 issuer=None, destination=None, recipient=None, in_response_to=None, status="urn:oasis:names:tc:SAML:2.0:status:Success",
                 aid=None, key=None, cert=None, alg="rsa-sha256", digest="sha256", not_before=None, not_after=None, extra=None,
                 confirm_after=None) -> bytes:
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset)
        attrs = {"email": ["ada@example.com"], "displayName": ["Ada"], "groups": ["tia-engineers"]} if attrs is None else attrs
        aid = aid or "_a" + secrets.token_hex(8)
        a_xml = self.assertion_xml(aid=aid, request_id=request_id, acs=acs, audience=audience, subject=subject, attrs=attrs,
                                   not_before=not_before or now - dt.timedelta(seconds=5), not_after=not_after or now + dt.timedelta(seconds=lifetime),
                                   issuer=issuer or self.entity_id, recipient=recipient, in_response_to=in_response_to, confirm_after=confirm_after)
        assertion = etree.fromstring(a_xml)
        if sign in ("assertion", "both"):
            assertion = self.sign(assertion, key, cert, alg, digest)
        rid = "_r" + secrets.token_hex(8)
        resp = etree.fromstring(
            f'<samlp:Response {NS} ID="{rid}" Version="2.0" IssueInstant="{iso(now)}" Destination="{escape(destination or acs)}" '
            f'InResponseTo="{escape(request_id)}"><saml:Issuer>{escape(issuer or self.entity_id)}</saml:Issuer>'
            f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status></samlp:Response>')
        resp.append(assertion)
        if extra:
            extra(resp, assertion)
        if sign in ("response", "both"):
            resp = self.sign(resp, key, cert, alg, digest)
        return etree.tostring(resp)

    @staticmethod
    def b64(xml: bytes) -> str:
        return base64.b64encode(xml).decode()


def decode_request(saml_request: str) -> etree._Element:
    return etree.fromstring(zlib.decompress(base64.b64decode(saml_request), -15))
