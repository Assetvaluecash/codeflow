"""Release 2 SaaS platform: usage metering and plan limits (PRD PL-014), data residency (PL-015, AC-041), per-tenant and customer-managed
keys (PL-016 BYOK)."""
import base64
import json
import urllib.parse
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
import pytest
from fastapi.testclient import TestClient

import conftest
from fixtures.build_lanes_fixture import build as build_lanes
from tia.api import create_app
from tia.credentials import LocalKeyWrapper, SecretError
from tia.identity import AuthError, IdentityStore
from tia.jobs import JobQueue
from tia.kms import AwsKmsWrapper, AzureKeyVaultWrapper, TenantKeyWrapper, sigv4_headers
from tia.secretstore import SecretStore
from tia.tenants import TenantError, Tenants, main as tenants_main


@pytest.fixture(scope="module")
def land(tmp_path_factory):
    return build_lanes(str(tmp_path_factory.mktemp("saas")))


def tenant(name=None):
    t = Tenants(conftest.PG.admin)
    return t, t.create(name or f"t-{uuid.uuid4().hex[:10]}")


def app_for(tid, tmp_path, **kw):
    return TestClient(create_app(conftest.PG.db, tid, str(tmp_path / f"ws-{tid[:8]}"), allow_local=True, **kw))


# ---- plan limits and metering ----------------------------------------------------------------------------------------------------
def test_plan_limits_refuse_the_action_that_would_exceed_them(land, tmp_path):
    adm, t = tenant()
    adm.set_limits(t["id"], {"max_repositories": 2, "max_analyses_per_month": 1, "max_evidence_records_per_month": 1})
    c = app_for(t["id"], tmp_path)
    names = list(land["paths"])
    for n in names[:2]:
        assert c.post("/api/v1/repositories", json={"path": land["paths"][n], "name": n}).status_code == 200
    r = c.post("/api/v1/repositories", json={"path": land["paths"][names[2]], "name": names[2]})
    assert r.status_code == 402 and r.headers["X-TIA-Code"] == "plan_limit" and "allows 2 repositories" in r.json()["detail"]
    assert c.post("/api/v1/impact-analyses", json={"target": names[0], "head": "HEAD", "base": "HEAD"}).status_code == 200
    assert c.post("/api/v1/impact-analyses", json={"target": names[0], "head": "HEAD", "base": "HEAD"}).status_code == 402
    dep = [{"repo": names[0], "environment": "prod", "revision": "abcdef1"}]
    assert c.post("/api/v1/evidence/deployments", content=json.dumps(dep)).status_code == 200
    assert c.post("/api/v1/evidence/deployments", content=json.dumps(dep)).status_code == 402
    audit = [e["event"] for e in c.get("/api/v1/audit?limit=200").json()]
    assert audit.count("plan_limit_reached") == 3
    with pytest.raises(TenantError, match="unknown plan limit"):
        adm.set_limits(t["id"], {"max_widgets": 1})


def test_a_new_user_beyond_the_user_limit_cannot_sign_in():
    adm, t = tenant()
    adm.set_limits(t["id"], {"max_users": 1})
    ids = IdentityStore(conftest.PG.db, t["id"])
    ids.upsert_user(None, {"subject": "alice"})
    ids.upsert_user(None, {"subject": "alice", "name": "Alice"})                      # an existing user still signs in
    with pytest.raises(AuthError) as e:
        ids.upsert_user(None, {"subject": "bob"})
    assert e.value.code == "plan_limit" and e.value.status == 402


def test_metering_and_billing_export(land, tmp_path, capsys):
    adm, t = tenant()
    c = app_for(t["id"], tmp_path)
    n = next(iter(land["paths"]))
    c.post("/api/v1/repositories", json={"path": land["paths"][n], "name": n})
    c.post("/api/v1/impact-analyses", json={"target": n, "head": "HEAD", "base": "HEAD"})
    u = c.get("/api/v1/usage").json()
    assert u["usage"]["repositories"] == 1 and u["usage"]["analyses_in_month"] == 1 and u["region"] is None and not u["customer_managed_key"]
    m = adm.meter(t["id"])
    assert m["repositories"] == 1 and m["analyses_in_month"] == 1 and m["tenant"] == t["name"]
    assert tenants_main(["--admin-dsn", conftest.PG.admin, "meter", t["id"], "--format", "csv"]) == 0
    head, row = capsys.readouterr().out.strip().splitlines()
    assert head.startswith("tenant,tenant_id,plan,month,repositories") and row.split(",")[0] == t["name"]
    assert c.get("/api/v1/usage?month=2026-13").status_code == 400


# ---- data residency --------------------------------------------------------------------------------------------------------------
def test_a_tenant_pinned_to_another_region_is_refused_before_any_data_is_touched(land, tmp_path, monkeypatch):
    adm, t = tenant()
    adm.set_region(t["id"], "us")
    monkeypatch.setenv("TIA_REGION", "eu")
    c = app_for(t["id"], tmp_path)
    r = c.post("/api/v1/repositories", json={"path": next(iter(land["paths"].values())), "name": "x"})
    assert r.status_code == 421 and r.json()["code"] == "wrong_region" and "region 'us'" in r.json()["detail"]
    with psycopg.connect(conftest.PG.admin) as conn:                                  # AC-041: nothing was written for it here
        assert conn.execute("select count(*) from repositories where tenant_id = %s", (t["id"],)).fetchone()[0] == 0
    monkeypatch.setenv("TIA_REGION", "us")
    assert app_for(t["id"], tmp_path).get("/api/v1/repositories").status_code == 200
    monkeypatch.delenv("TIA_REGION")
    assert app_for(t["id"], tmp_path).get("/api/v1/repositories").json()["code"] == "wrong_region"    # a pinned tenant needs a regional deployment
    with pytest.raises(TenantError, match="one of"):
        adm.set_region(t["id"], "mars")


def test_a_worker_never_claims_a_job_of_another_regions_tenant(monkeypatch):
    adm, us = tenant()
    adm.set_region(us["id"], "us")
    _, free = tenant()
    q = JobQueue(conftest.PG.db)
    for tid in (us["id"], free["id"]):
        q.enqueue(tid, "noop", {}, key=f"k-{tid}")
    monkeypatch.setenv("TIA_REGION", "eu")
    wq = JobQueue(conftest.PG.worker_db)
    claimed = []
    while True:
        j = wq.claim("w1", kinds=["noop"])
        if not j:
            break
        claimed.append(str(j.tenant_id))
        wq.succeed(j, {})
    assert free["id"] in claimed and us["id"] not in claimed


# ---- customer-managed keys (BYOK) -------------------------------------------------------------------------------------------------
class FakeKms:
    """AWS KMS stand-in: Encrypt/Decrypt with EncryptionContext enforcement, SigV4 header check, and a switch to disable the key."""

    def __init__(self):
        self.enabled, self.calls, outer = True, [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                target, auth = self.headers.get("X-Amz-Target"), self.headers.get("Authorization", "")
                outer.calls.append(target)
                if not auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") or "SignedHeaders=content-type;host;x-amz-date;x-amz-target" not in auth:
                    return self._send(403, {"__type": "InvalidSignatureException"})
                if not outer.enabled:
                    return self._send(400, {"__type": "DisabledException"})
                req = json.loads(body)
                ctx = json.dumps(req["EncryptionContext"], sort_keys=True)
                if target == "TrentService.Encrypt":
                    blob = base64.b64encode(json.dumps({"p": req["Plaintext"], "ctx": ctx}).encode()).decode()
                    return self._send(200, {"CiphertextBlob": blob, "KeyId": req["KeyId"]})
                inner = json.loads(base64.b64decode(req["CiphertextBlob"]))
                if inner["ctx"] != ctx:
                    return self._send(400, {"__type": "InvalidCiphertextException"})
                return self._send(200, {"Plaintext": inner["p"]})

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


ARN = "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"


@pytest.fixture
def kms(monkeypatch):
    k = FakeKms()
    monkeypatch.setenv("TIA_AWS_KMS_ENDPOINT", k.url)
    monkeypatch.setenv("TIA_AWS_ACCESS_KEY_ID", "AKIDTEST")
    monkeypatch.setenv("TIA_AWS_SECRET_ACCESS_KEY", "test-secret-key")
    yield k
    k.srv.shutdown()


def test_aws_kms_wrapper_binds_the_tenant(kms):
    w = AwsKmsWrapper(ARN)
    wrapped = w.wrap(b"k" * 32, b"tia:dek:tenant-a:1")
    assert w.unwrap(wrapped, b"tia:dek:tenant-a:1") == b"k" * 32
    with pytest.raises(SecretError, match="HTTP 400"):
        w.unwrap(wrapped, b"tia:dek:tenant-b:1")                                       # moved to another tenant: refused by the KMS
    kms.enabled = False
    with pytest.raises(SecretError, match="disabled, deleted, or not granted"):
        w.unwrap(wrapped, b"tia:dek:tenant-a:1")                                       # crypto-shredding: the customer revoked the key
    with pytest.raises(SecretError, match="ARN"):
        AwsKmsWrapper("arn:aws:s3:::bucket")


def test_sigv4_is_stable_and_signs_the_body():
    import datetime
    now = datetime.datetime(2026, 11, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)
    a = sigv4_headers("POST", "https://kms.eu-west-1.amazonaws.com/", "eu-west-1", "kms", b"{}", "AK", "SK", None, "TrentService.Encrypt", now)
    b = sigv4_headers("POST", "https://kms.eu-west-1.amazonaws.com/", "eu-west-1", "kms", b"{ }", "AK", "SK", None, "TrentService.Encrypt", now)
    assert a["x-amz-date"] == "20261104T120000Z" and "Credential=AK/20261104/eu-west-1/kms/aws4_request" in a["authorization"]
    assert a["authorization"] != b["authorization"]


def test_switching_a_tenant_to_its_own_key_keeps_every_secret(kms):
    adm, t = tenant()
    platform = LocalKeyWrapper({"p1": os.urandom(32)}, "p1")
    ss = SecretStore(conftest.PG.db, t["id"], TenantKeyWrapper(lambda: None, platform))
    ss.put("github/token", "ghp-value-123")
    r = adm.set_key(t["id"], f"aws-kms:{ARN}", platform_wrapper=platform)
    assert r["data_keys_rewrapped"] == 1 and "TrentService.Encrypt" in kms.calls
    byok = SecretStore(conftest.PG.db, t["id"], TenantKeyWrapper(lambda: f"aws-kms:{ARN}", platform))
    assert byok.get_text("github/token") == "ghp-value-123" and byok.wrapper.customer_managed
    with pytest.raises(SecretError):
        SecretStore(conftest.PG.db, t["id"], platform).get_text("github/token")         # the platform key alone can no longer open it
    kms.enabled = False
    with pytest.raises(SecretError):
        SecretStore(conftest.PG.db, t["id"], TenantKeyWrapper(lambda: f"aws-kms:{ARN}", platform)).get_text("github/token")
    kms.enabled = True
    back = adm.set_key(t["id"], None, platform_wrapper=platform)
    assert back["key_ref"] is None and SecretStore(conftest.PG.db, t["id"], platform).get_text("github/token") == "ghp-value-123"


class FakeAzure:
    def __init__(self):
        outer = self
        self.key = os.urandom(16)
        self.token_forms = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path.endswith("/oauth2/v2.0/token"):
                    outer.token_forms.append(dict(urllib.parse.parse_qsl(body.decode())))
                    return self._send(200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 3599})
                if self.headers.get("Authorization") != "Bearer tok":
                    return self._send(401, {})
                v = json.loads(body)["value"]
                raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
                out = bytes(x ^ y for x, y in zip(raw, (outer.key * 8)[:len(raw)]))           # reversible stand-in for RSA-OAEP
                return self._send(200, {"kid": "k", "value": base64.urlsafe_b64encode(out).decode().rstrip("=")})

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_azure_key_vault_wrapper(monkeypatch):
    az = FakeAzure()
    monkeypatch.setenv("TIA_AZURE_KEYVAULT_TEST_URL", "1")
    monkeypatch.setenv("TIA_AZURE_LOGIN_URL", az.url)
    for k, v in (("TIA_AZURE_TENANT_ID", "t"), ("TIA_AZURE_CLIENT_ID", "c"), ("TIA_AZURE_CLIENT_SECRET", "s")):
        monkeypatch.setenv(k, v)
    w = AzureKeyVaultWrapper(az.url + "/keys/tia/1")
    blob = w.wrap(b"d" * 32, b"tia:dek:a:1")
    assert w.unwrap(blob, b"tia:dek:a:1") == b"d" * 32
    with pytest.raises(SecretError, match="does not belong to this tenant"):
        w.unwrap(blob, b"tia:dek:b:1")
    assert len(az.token_forms) == 1                                           # the token is reused until it nears expiry
    az.srv.shutdown()


def test_azure_workload_identity_needs_no_secret(monkeypatch, tmp_path):
    az = FakeAzure()
    jwt = tmp_path / "token"
    jwt.write_text("projected-service-account-jwt")
    monkeypatch.setenv("TIA_AZURE_KEYVAULT_TEST_URL", "1")
    monkeypatch.setenv("TIA_AZURE_LOGIN_URL", az.url)
    for k in ("TIA_AZURE_TENANT_ID", "TIA_AZURE_CLIENT_ID", "TIA_AZURE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (("AZURE_TENANT_ID", "t"), ("AZURE_CLIENT_ID", "wi-client"), ("AZURE_FEDERATED_TOKEN_FILE", str(jwt))):
        monkeypatch.setenv(k, v)
    w = AzureKeyVaultWrapper(az.url + "/keys/tia/1")
    assert w.unwrap(w.wrap(b"k" * 32, b"aad"), b"aad") == b"k" * 32
    form = az.token_forms[0]
    assert form["client_assertion"] == "projected-service-account-jwt" and "client_secret" not in form and form["client_id"] == "wi-client"
    assert form["client_assertion_type"].endswith("jwt-bearer")
    az.srv.shutdown()
