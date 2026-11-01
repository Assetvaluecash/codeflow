"""SAML 2.0 service provider (PL-001): SP-initiated sign-in, HTTP-Redirect request, HTTP-POST response.

What is accepted, and what is not (every refusal is deliberate):
* The ASSERTION must carry a valid XML signature by one of the identity provider's configured certificates (SHA-256 or stronger;
  SHA-1 signatures are refused). A response-level signature is accepted only if the assertion is inside the signed element, and only
  the signed element is ever read (signature-wrapping attacks cannot smuggle in an unsigned assertion).
* Exactly one assertion; encrypted assertions are refused; any DOCTYPE/entity declaration is refused before parsing (XXE, entity expansion).
* The response must answer a request this server made (InResponseTo matches the stored request id, bound to the browser that started it),
  so unsolicited IdP-initiated responses are refused. Destination, issuer, audience, recipient and the validity window are all checked,
  and an assertion id is accepted once (replay).
"""
from __future__ import annotations

import base64
import re
import secrets
import urllib.parse
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.responses import RedirectResponse
from lxml import etree

from .identity import AuthError, as_list, claim_at

NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol", "saml": "urn:oasis:names:tc:SAML:2.0:assertion", "ds": "http://www.w3.org/2000/09/xmldsig#"}
SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
MAX_RESPONSE = 1_000_000
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False, remove_comments=True)


def sp_entity_id(public_url: str, cfg: dict) -> str:
    return cfg.get("sp_entity_id") or public_url.rstrip("/") + "/auth/saml/metadata"


def acs_url(public_url: str) -> str:
    return public_url.rstrip("/") + "/auth/saml/acs"


def _utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(re.sub(r"Z$", "+00:00", s.strip()))
    except ValueError:
        raise AuthError("login_failed", 401, f"unparseable time '{s[:40]}'")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def authn_request(request_id: str, destination: str, acs: str, issuer: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    esc = lambda v: (v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))
    return ('<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="{request_id}" Version="2.0" IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}" Destination="{esc(destination)}" '
            f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" AssertionConsumerServiceURL="{esc(acs)}">'
            f'<saml:Issuer>{esc(issuer)}</saml:Issuer></samlp:AuthnRequest>')


def redirect_binding(sso_url: str, xml: str, relay_state: str) -> str:
    deflated = zlib.compress(xml.encode())[2:-4]                    # raw DEFLATE, as the HTTP-Redirect binding requires
    q = urllib.parse.urlencode({"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay_state})
    return sso_url + ("&" if "?" in sso_url else "?") + q


def start_login(auth, tenant: str, idp: dict, browser: str, return_to: str):
    s = auth.settings
    if not s.secure_cookies:
        raise AuthError("idp_misconfigured", 503, "SAML sign-in needs an https TIA_PUBLIC_URL (the browser must send its login cookie on the "
                                                  "cross-site POST from the identity provider)")
    cfg = idp["config"]
    request_id = "_" + secrets.token_hex(16)
    ids = auth.ids(tenant)
    state = ids.save_login_state(idp["id"], request_id, "saml", return_to, browser)
    xml = authn_request(request_id, cfg["sso_url"], acs_url(s.public_url), sp_entity_id(s.public_url, cfg))
    return RedirectResponse(redirect_binding(cfg["sso_url"], xml, state), status_code=302)


def parse_response(b64: str) -> etree._Element:
    if not b64 or len(b64) > MAX_RESPONSE * 4 // 3 + 8:
        raise AuthError("login_failed", 400, "SAMLResponse missing or too large")
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:
        raise AuthError("login_failed", 400, "SAMLResponse is not base64")
    if len(raw) > MAX_RESPONSE:
        raise AuthError("login_failed", 400, "SAMLResponse too large")
    if b"<!doctype" in raw.lower() or b"<!entity" in raw.lower():
        raise AuthError("login_failed", 400, "the response declares a DOCTYPE/ENTITY; refused")
    try:
        return etree.fromstring(raw, PARSER)
    except etree.XMLSyntaxError as e:
        raise AuthError("login_failed", 400, f"response is not well-formed XML: {str(e)[:80]}")


def _verify_signed(element, certs: list[str]):
    """The signed element, or AuthError. Tries each configured certificate (a rotation has two); SHA-1 is not accepted by signxml's defaults."""
    from signxml import XMLVerifier
    from signxml.exceptions import InvalidSignature, SignXMLException
    last = None
    for pem in certs:
        try:
            return XMLVerifier().verify(element, x509_cert=pem, expect_references=1).signed_xml
        except (InvalidSignature, SignXMLException, ValueError, etree.LxmlError) as e:
            last = e
    raise AuthError("login_failed", 401, f"no valid signature by a configured certificate: {str(last)[:120]}")


def validate(b64_response: str, *, cfg: dict, public_url: str, request_id: str, now: Optional[datetime] = None) -> dict:
    """Verify a SAMLResponse and return {'subject', 'attributes', 'assertion_id', 'not_after'}. Raises AuthError on anything not right."""
    now = now or datetime.now(timezone.utc)
    skew = timedelta(seconds=int(cfg.get("clock_skew", 60)))
    root = parse_response(b64_response)
    q = lambda el, path: el.xpath(path, namespaces=NS)
    if root.tag != "{%s}Response" % NS["samlp"]:
        raise AuthError("login_failed", 400, "document is not a SAML Response")
    if q(root, ".//saml:EncryptedAssertion"):
        raise AuthError("login_failed", 400, "encrypted assertions are not supported")
    if len(q(root, ".//saml:Assertion")) != 1:
        raise AuthError("login_failed", 400, "the response must contain exactly one assertion")
    candidate = q(root, ".//saml:Assertion")[0]
    if q(candidate, "./ds:Signature"):
        # the assertion's own signature: verified on a detached copy, and ONLY that verified element is read from here on
        signed = _verify_signed(etree.fromstring(etree.tostring(candidate), PARSER), cfg["certificates"])
        if signed.tag != "{%s}Assertion" % NS["saml"]:
            raise AuthError("login_failed", 401, "the assertion signature covers something else")
        assertion = signed
    elif q(root, "./ds:Signature"):
        signed = _verify_signed(root, cfg["certificates"])
        found = q(signed, "./saml:Assertion") if signed.tag == "{%s}Response" % NS["samlp"] else []
        if len(found) != 1:
            raise AuthError("login_failed", 401, "the signed response does not contain the assertion directly")
        assertion = found[0]
    else:
        raise AuthError("login_failed", 401, "the assertion is not signed")
    # facts about the response itself are read from the response element as signed, or checked on the assertion when only that is signed
    status = q(root, "./samlp:Status/samlp:StatusCode/@Value")
    if status != [SUCCESS]:
        raise AuthError("login_failed", 401, f"the identity provider reported {status[:1]}")
    if root.get("InResponseTo") != request_id:
        raise AuthError("login_failed", 401, "InResponseTo does not match the request this server made (unsolicited response)")
    if root.get("Destination") != acs_url(public_url):
        raise AuthError("login_failed", 401, "Destination is not this server's assertion consumer URL")
    for issuer in q(root, "./saml:Issuer/text()") + q(assertion, "./saml:Issuer/text()"):
        if issuer.strip() != cfg["entity_id"]:
            raise AuthError("login_failed", 401, f"issuer {issuer.strip()[:80]!r} is not the configured identity provider")
    if not q(assertion, "./saml:Issuer"):
        raise AuthError("login_failed", 401, "assertion has no issuer")
    aid = assertion.get("ID")
    if not aid:
        raise AuthError("login_failed", 401, "assertion has no ID")
    cond = q(assertion, "./saml:Conditions")
    if len(cond) != 1:
        raise AuthError("login_failed", 401, "assertion needs exactly one Conditions element")
    nb, na = _utc(cond[0].get("NotBefore")), _utc(cond[0].get("NotOnOrAfter"))
    if na is None:
        raise AuthError("login_failed", 401, "assertion has no NotOnOrAfter")
    if nb and now + skew < nb:
        raise AuthError("login_failed", 401, "assertion is not valid yet")
    if now - skew >= na:
        raise AuthError("login_failed", 401, "assertion has expired")
    audiences = q(cond[0], "./saml:AudienceRestriction/saml:Audience/text()")
    if sp_entity_id(public_url, cfg) not in [a.strip() for a in audiences]:
        raise AuthError("login_failed", 401, "assertion is not addressed to this service provider")
    confirmed = False
    for sc in q(assertion, "./saml:Subject/saml:SubjectConfirmation"):
        d = q(sc, "./saml:SubjectConfirmationData")
        if sc.get("Method") == BEARER and len(d) == 1:
            d0 = d[0]
            dna = _utc(d0.get("NotOnOrAfter"))
            if d0.get("Recipient") == acs_url(public_url) and d0.get("InResponseTo") == request_id and dna and now - skew < dna:
                confirmed = True
    if not confirmed:
        raise AuthError("login_failed", 401, "no bearer subject confirmation for this server and request")
    name_id = [t.strip() for t in q(assertion, "./saml:Subject/saml:NameID/text()") if t.strip()]
    attrs: dict = {}
    for a in q(assertion, "./saml:AttributeStatement/saml:Attribute"):
        vals = [(v.text or "").strip() for v in q(a, "./saml:AttributeValue") if (v.text or "").strip()]
        for key in (a.get("Name"), a.get("FriendlyName")):
            if key:
                attrs.setdefault(key, []).extend(vals)
    return {"name_id": name_id[0] if name_id else None, "attributes": attrs, "assertion_id": aid, "not_after": na}


def claims_from(cfg: dict, result: dict) -> dict:
    """SAML attributes in the shape the OIDC code maps (subject, email, name, groups, source-host logins), using the configured attribute names."""
    attrs = result["attributes"]

    def one(name):
        v = attrs.get(name) if name else None
        return v[0] if v else None
    subject = one(cfg.get("subject_attribute")) or result["name_id"]
    if not subject:
        raise AuthError("login_failed", 401, "no subject: the assertion has no NameID and no subject attribute")
    claims = {"sub": subject, "email": one(cfg.get("email_attribute")), "name": one(cfg.get("name_attribute")),
              "groups": attrs.get(cfg.get("groups_attribute"), []) if cfg.get("groups_attribute") else []}
    for host, attr in (cfg.get("scm_login_attributes") or {}).items():
        claims["scm_" + host] = one(attr)
    return claims


def metadata(public_url: str, cfg: dict) -> str:
    return ('<?xml version="1.0"?><md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'entityID="{sp_entity_id(public_url, cfg)}"><md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
            'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol"><md:NameIDFormat>'
            'urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified</md:NameIDFormat>'
            f'<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" Location="{acs_url(public_url)}" index="0" '
            'isDefault="true"/></md:SPSSODescriptor></md:EntityDescriptor>')


def claim_config(cfg: dict) -> dict:
    """The settings the shared identity mapping (identity_from_claims, role map) reads, for the claims dict claims_from() builds."""
    return {"subject_claim": "sub", "email_claim": "email", "name_claim": "name", "groups_claim": "groups",
            "scm_login_claims": {h: "scm_" + h for h in (cfg.get("scm_login_attributes") or {})},
            "role_map": cfg.get("role_map") or {}, "default_role": cfg.get("default_role")}
