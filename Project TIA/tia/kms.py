"""Customer-managed keys (PRD PL-016 BYOK): a tenant's data-encryption key wrapped by a key in the CUSTOMER's KMS.

Every tenant already has its own data key (tenant_keys, see secretstore.py). With BYOK the tenant's key reference names the customer's key:
    aws-kms:arn:aws:kms:eu-west-1:111122223333:key/<id>        AWS KMS Encrypt/Decrypt, the tenant binding as encryption context
    azure-kv:https://acme.vault.azure.net/keys/tia/<version>    Azure Key Vault wrapKey/unwrapKey (RSA-OAEP-256)
    vault-transit:<key name>                                     HashiCorp Vault Transit (the customer's own key in TIA's Vault, or a Vault the
                                                                 customer runs, fronting any cloud KMS through Vault's managed keys)
The key never leaves the customer's KMS; TIA holds only the wrapped data key. If the customer disables or deletes the key, the tenant's
secrets can no longer be decrypted (crypto-shredding) and every use fails closed with a clear error.

Credentials: AWS (TIA_AWS_ACCESS_KEY_ID, TIA_AWS_SECRET_ACCESS_KEY, optional TIA_AWS_SESSION_TOKEN; a role granted kms:Encrypt and kms:Decrypt on
the key); Azure (TIA_AZURE_TENANT_ID, TIA_AZURE_CLIENT_ID, TIA_AZURE_CLIENT_SECRET; wrapKey and unwrapKey permissions). TIA_AWS_KMS_ENDPOINT and
TIA_AZURE_LOGIN_URL override the service addresses (sovereign clouds, tests). Standard library only (SigV4 is implemented here).
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .credentials import KeyWrapper, SecretError, VaultClient, VaultTransitWrapper
from . import airgap

ARN = re.compile(r"^arn:aws[a-z-]*:kms:([a-z0-9-]+):\d{12}:(key|alias)/[A-Za-z0-9/_-]+$")
AZURE_KEY = re.compile(r"^https://[a-z0-9-]+\.(vault\.azure\.net|vault\.azure\.cn|vault\.usgovcloudapi\.net|managedhsm\.azure\.net)(:\d+)?/keys/[A-Za-z0-9-]+(/[A-Za-z0-9]+)?$")


def _http(method: str, url: str, headers: dict, body: bytes) -> dict:
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(airgap.checked(req, "kms"), timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SecretError(f"the customer key service answered HTTP {e.code} (the key may be disabled, deleted, or not granted to TIA)")
    except OSError:
        raise SecretError("the customer key service could not be reached")


# ---- AWS KMS ---------------------------------------------------------------------------------------------------------------------------
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, region: str, service: str, body: bytes, access_key: str, secret_key: str,
                  session_token: Optional[str], amz_target: str, now: Optional[datetime.datetime] = None) -> dict:
    """AWS Signature Version 4 for a JSON-1.1 request (the form AWS KMS uses)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    u = urllib.parse.urlsplit(url)
    headers = {"content-type": "application/x-amz-json-1.1", "host": u.netloc, "x-amz-date": amz_date, "x-amz-target": amz_target}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, u.path or "/", u.query, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                           hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return headers


class AwsKmsWrapper(KeyWrapper):
    """Wraps with KMS Encrypt; the AAD (tenant id and key version) is the EncryptionContext, so a wrapped key moved to another tenant
    cannot be unwrapped."""

    def __init__(self, key_arn: str):
        m = ARN.match(key_arn or "")
        if not m:
            raise SecretError("not an AWS KMS key ARN (arn:aws:kms:<region>:<account>:key/<id>)")
        self.arn, self.region = key_arn, m.group(1)
        self.id = f"aws-kms:{key_arn}"
        self.endpoint = os.environ.get("TIA_AWS_KMS_ENDPOINT") or f"https://kms.{self.region}.amazonaws.com/"
        self.access, self.secret = os.environ.get("TIA_AWS_ACCESS_KEY_ID"), os.environ.get("TIA_AWS_SECRET_ACCESS_KEY")
        self.session = os.environ.get("TIA_AWS_SESSION_TOKEN")
        if not (self.access and self.secret):
            raise SecretError("AWS credentials are not configured (TIA_AWS_ACCESS_KEY_ID, TIA_AWS_SECRET_ACCESS_KEY)")

    def _call(self, target: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        h = sigv4_headers("POST", self.endpoint, self.region, "kms", body, self.access, self.secret, self.session, f"TrentService.{target}")
        return _http("POST", self.endpoint, h, body)

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        r = self._call("Encrypt", {"KeyId": self.arn, "Plaintext": base64.b64encode(dek).decode(), "EncryptionContext": {"tia": aad.hex()}})
        return b"aws1:" + r["CiphertextBlob"].encode()

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        if not wrapped.startswith(b"aws1:"):
            raise SecretError("this data key was not wrapped by AWS KMS")
        r = self._call("Decrypt", {"KeyId": self.arn, "CiphertextBlob": wrapped[5:].decode(), "EncryptionContext": {"tia": aad.hex()}})
        return base64.b64decode(r["Plaintext"])


# ---- Azure Key Vault ---------------------------------------------------------------------------------------------------------------------
class AzureKeyVaultWrapper(KeyWrapper):
    """Wraps with Key Vault wrapKey (RSA-OAEP-256). Key Vault has no AAD, so the AAD is bound by sealing it with the data key: the wrapped
    blob carries an AES-GCM tag over the AAD that only the right data key reproduces."""

    def __init__(self, key_url: str):
        if not AZURE_KEY.match(key_url or "") and not os.environ.get("TIA_AZURE_KEYVAULT_TEST_URL"):
            raise SecretError("not an Azure Key Vault key URL (https://<vault>.vault.azure.net/keys/<name>[/<version>])")
        self.key_url = key_url.rstrip("/")
        self.id = f"azure-kv:{self.key_url}"
        # a client secret (TIA_AZURE_*), or AKS workload identity (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE injected by
        # the platform): then no secret exists anywhere, the pod's projected service-account token is exchanged for a Key Vault token
        self.tenant = os.environ.get("TIA_AZURE_TENANT_ID") or os.environ.get("AZURE_TENANT_ID")
        self.client = os.environ.get("TIA_AZURE_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
        self.secret = os.environ.get("TIA_AZURE_CLIENT_SECRET")
        self.assertion_file = None if self.secret else os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        self.login = (os.environ.get("TIA_AZURE_LOGIN_URL") or os.environ.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com").rstrip("/")
        if not (self.tenant and self.client and (self.secret or self.assertion_file)):
            raise SecretError("Azure credentials are not configured (TIA_AZURE_TENANT_ID, TIA_AZURE_CLIENT_ID, TIA_AZURE_CLIENT_SECRET, "
                              "or AKS workload identity)")
        self._token, self._expires = None, 0.0

    def _bearer(self) -> str:
        if self._token is None or time.time() > self._expires:            # tokens last about an hour: renew 5 minutes early
            form = {"grant_type": "client_credentials", "client_id": self.client, "scope": "https://vault.azure.net/.default"}
            if self.secret:
                form["client_secret"] = self.secret
            else:
                with open(self.assertion_file, encoding="utf-8") as f:     # re-read: the platform rotates it
                    form["client_assertion"] = f.read().strip()
                form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            r = _http("POST", f"{self.login}/{self.tenant}/oauth2/v2.0/token", {"Content-Type": "application/x-www-form-urlencoded"},
                      urllib.parse.urlencode(form).encode())
            self._token = r.get("access_token")
            if not self._token:
                raise SecretError("Azure did not issue an access token")
            self._expires = time.time() + max(60, int(r.get("expires_in") or 3600) - 300)
        return self._token

    def _call(self, op: str, value: bytes) -> bytes:
        body = json.dumps({"alg": "RSA-OAEP-256", "value": base64.urlsafe_b64encode(value).decode().rstrip("=")}).encode()
        r = _http("POST", f"{self.key_url}/{op}?api-version=7.4", {"Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/json"}, body)
        v = r.get("value", "")
        return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        tag = hmac.new(dek, b"tia-aad|" + aad, hashlib.sha256).hexdigest().encode()
        return b"akv1:" + tag + b":" + base64.b64encode(self._call("wrapkey", dek))

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        if not wrapped.startswith(b"akv1:"):
            raise SecretError("this data key was not wrapped by Azure Key Vault")
        tag, blob = wrapped[5:].split(b":", 1)
        dek = self._call("unwrapkey", base64.b64decode(blob))
        if not hmac.compare_digest(tag, hmac.new(dek, b"tia-aad|" + aad, hashlib.sha256).hexdigest().encode()):
            raise SecretError("the data key does not belong to this tenant")
        return dek


class TenantKeyWrapper(KeyWrapper):
    """The platform's wrapper, or the tenant's customer-managed key when its registry entry names one (resolved on first use)."""

    def __init__(self, key_ref_of, default: Optional[KeyWrapper]):
        self._ref_of, self._default, self._w, self._ref = key_ref_of, default, None, None

    def _wrapper(self) -> KeyWrapper:
        if self._w is None:
            self._ref = self._ref_of()
            w = wrapper_for(self._ref, self._default)
            if w is None:
                raise SecretError("no key-encryption key is configured")
            self._w = w
        return self._w

    @property
    def id(self) -> str:
        return self._wrapper().id

    @property
    def customer_managed(self) -> bool:
        self._wrapper()
        return bool(self._ref)

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        return self._wrapper().wrap(dek, aad)

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        return self._wrapper().unwrap(wrapped, aad)


def wrapper_for(key_ref: Optional[str], default: Optional[KeyWrapper], vault: Optional[VaultClient] = None) -> Optional[KeyWrapper]:
    """The key wrapper for a tenant: its customer-managed key when it has one, else the platform's."""
    if not key_ref:
        return default
    kind, _, ref = key_ref.partition(":")
    if kind == "aws-kms":
        return AwsKmsWrapper(ref)
    if kind == "azure-kv":
        return AzureKeyVaultWrapper(ref)
    if kind == "vault-transit":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", ref):
            raise SecretError("a Vault Transit key name is letters, digits, '.', '_' or '-'")
        return VaultTransitWrapper(vault or VaultClient.from_env(), ref)
    raise SecretError(f"unknown key reference '{kind}' (aws-kms:, azure-kv: or vault-transit:)")
