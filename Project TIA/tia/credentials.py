"""Where TIA's own secrets come from (PL-011): an approved secrets manager, never source code and never the database in clear.

A configuration value may be a literal (development only) or a reference:
    file:/run/secrets/github_token          a file (Docker / Kubernetes mounted secret)
    vault:secret/tia/github#token           a HashiCorp Vault KV v2 secret, one key of it
    env:OTHER_VARIABLE                      another environment variable
and every secret variable NAME also honours NAME_FILE (the Docker convention). Vault connection settings come from the standard
VAULT_ADDR, VAULT_TOKEN or VAULT_TOKEN_FILE (re-read on every call, so a rotated token is picked up), VAULT_NAMESPACE, VAULT_CACERT.

Key-encryption keys for the per-tenant secret store are provided by a KeyWrapper: a local master key held in a secrets manager, or
Vault's Transit engine, which performs the wrapping so the key never leaves Vault.

Nothing here logs a value. Errors name the reference and the HTTP status, never the response body.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretError(Exception):
    """A secret could not be obtained or used. The message never contains secret material."""


def is_loopback(host: Optional[str]) -> bool:
    """This machine: 'localhost' or a loopback IP address. Parsed, not pattern-matched: '127.example.com' is a public hostname."""
    h = (host or "").strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def require_secure_url(url: str, what: str, allow_insecure: bool = False) -> str:
    """https is required for anything that carries credentials; plain http only to this machine (development) or when the operator
    explicitly accepts the risk."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" or allow_insecure or (u.scheme == "http" and is_loopback(u.hostname)):
        return url
    raise SecretError(f"{what} must use https (got '{u.scheme or 'no scheme'}://{u.hostname or ''}'); plain http is only allowed to localhost")


# ---- files ---------------------------------------------------------------------------------------------------------------
def read_secret_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise SecretError(f"secret file '{path}' cannot be read ({e.strerror or type(e).__name__})")
    text = raw.decode("utf-8", "strict").rstrip("\r\n")
    if not text:
        raise SecretError(f"secret file '{path}' is empty")
    return text


# ---- Vault -----------------------------------------------------------------------------------------------------------------
class VaultClient:
    def __init__(self, addr: str, token: Optional[str] = None, token_file: Optional[str] = None, namespace: Optional[str] = None,
                 ca_file: Optional[str] = None, timeout: float = 10.0, allow_insecure: bool = False):
        self.addr = require_secure_url(addr.rstrip("/"), "VAULT_ADDR", allow_insecure)
        self._token, self._token_file, self.namespace, self.timeout = token, token_file, namespace, timeout
        self._ctx = ssl.create_default_context(cafile=ca_file) if self.addr.startswith("https") else None
        if not token and not token_file:
            raise SecretError("Vault needs VAULT_TOKEN or VAULT_TOKEN_FILE")

    @classmethod
    def from_env(cls) -> "VaultClient":
        addr = os.environ.get("VAULT_ADDR")
        if not addr:
            raise SecretError("VAULT_ADDR is not set")
        return cls(addr, os.environ.get("VAULT_TOKEN"), os.environ.get("VAULT_TOKEN_FILE"), os.environ.get("VAULT_NAMESPACE"),
                   os.environ.get("VAULT_CACERT"), allow_insecure=os.environ.get("TIA_ALLOW_INSECURE_OUTBOUND", "").lower() in ("1", "true", "yes"))

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        token = self._token or read_secret_file(self._token_file)
        headers = {"X-Vault-Token": token, "Content-Type": "application/json", "Accept": "application/json"}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        req = urllib.request.Request(f"{self.addr}/v1/{path.lstrip('/')}", method=method, headers=headers,
                                     data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise SecretError(f"Vault {method} {path} failed (HTTP {e.code})")
        except (urllib.error.URLError, OSError) as e:
            raise SecretError(f"Vault is unreachable ({type(e).__name__})")
        return json.loads(body) if body else {}

    def kv_get(self, mount: str, path: str, key: Optional[str] = None):
        data = self._call("GET", f"{mount}/data/{path}")["data"]["data"]
        if key is None:
            return data
        if key not in data:
            raise SecretError(f"Vault secret {mount}/{path} has no key '{key}'")
        return data[key]

    def kv_put(self, mount: str, path: str, data: dict) -> None:
        self._call("POST", f"{mount}/data/{path}", {"data": data})

    def transit_encrypt(self, key_name: str, plaintext: bytes, context: bytes = b"") -> str:
        body = {"plaintext": base64.b64encode(plaintext).decode()}
        if context:
            body["context"] = base64.b64encode(context).decode()
        return self._call("POST", f"transit/encrypt/{key_name}", body)["data"]["ciphertext"]

    def transit_decrypt(self, key_name: str, ciphertext: str, context: bytes = b"") -> bytes:
        body = {"ciphertext": ciphertext}
        if context:
            body["context"] = base64.b64encode(context).decode()
        return base64.b64decode(self._call("POST", f"transit/decrypt/{key_name}", body)["data"]["plaintext"])


# ---- references --------------------------------------------------------------------------------------------------------------
def resolve(value: Optional[str], vault: Optional[VaultClient] = None) -> Optional[str]:
    """A literal stays as it is; file:/vault:/env: references are fetched. None stays None."""
    if value is None:
        return None
    if value.startswith("file:"):
        return read_secret_file(value[5:])
    if value.startswith("env:"):
        got = os.environ.get(value[4:])
        if got is None:
            raise SecretError(f"environment variable {value[4:]} (referenced by env:) is not set")
        return got
    if value.startswith("vault:"):
        ref = value[6:]
        loc, _, key = ref.partition("#")
        mount, _, path = loc.partition("/")
        if not (mount and path and key):
            raise SecretError("a vault reference looks like vault:<mount>/<path>#<key>")
        return str(( vault or VaultClient.from_env()).kv_get(mount, path, key))
    return value


def env_secret(name: str, default: Optional[str] = None, vault: Optional[VaultClient] = None) -> Optional[str]:
    """The secret configured as NAME (a literal or a reference) or NAME_FILE (a path). Setting both is an error, not a guess."""
    direct, filed = os.environ.get(name), os.environ.get(name + "_FILE")
    if direct and filed:
        raise SecretError(f"both {name} and {name}_FILE are set; set only one")
    if filed:
        return read_secret_file(filed)
    if direct:
        return resolve(direct, vault)
    return default


# ---- key-encryption keys -------------------------------------------------------------------------------------------------------
class KeyWrapper:
    """Wraps and unwraps data-encryption keys. `id` names the key-encryption key in use so keys can be re-wrapped on rotation."""
    id: str

    def wrap(self, dek: bytes, aad: bytes) -> bytes:                       # pragma: no cover - interface
        raise NotImplementedError

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:                 # pragma: no cover - interface
        raise NotImplementedError


class LocalKeyWrapper(KeyWrapper):
    """AES-256-GCM key wrapping with master keys held by a secrets manager. Several may be loaded so that old wrapped keys can still be
    opened while they are re-wrapped under the current one."""

    def __init__(self, keys: dict, current: str):
        if current not in keys:
            raise SecretError(f"current master key id '{current}' is not among the loaded master keys")
        for kid, k in keys.items():
            if len(k) != 32:
                raise SecretError(f"master key '{kid}' must be 32 bytes (256 bits)")
        self.keys, self.current = dict(keys), current
        self.id = f"local:{current}"

    @staticmethod
    def generate() -> str:
        return base64.b64encode(os.urandom(32)).decode()

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        kid = self.current.encode()
        return b"L1" + bytes([len(kid)]) + kid + nonce + AESGCM(self.keys[self.current]).encrypt(nonce, dek, aad)

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        wrapped = bytes(wrapped)
        if not wrapped.startswith(b"L1"):
            raise SecretError("wrapped key was not produced by a local master key")
        n = wrapped[2]
        kid, rest = wrapped[3:3 + n].decode(), wrapped[3 + n:]
        if kid not in self.keys:
            raise SecretError(f"master key '{kid}' is not loaded; add it (as a previous key) to open this tenant's keys")
        try:
            return AESGCM(self.keys[kid]).decrypt(rest[:12], rest[12:], aad)
        except Exception:
            raise SecretError("a wrapped key failed its integrity check")


class VaultTransitWrapper(KeyWrapper):
    """Key wrapping by Vault's Transit engine: the key-encryption key never leaves Vault; rotating it there needs no downtime."""

    def __init__(self, vault: VaultClient, key_name: str):
        self.vault, self.key_name = vault, key_name
        self.id = f"vault-transit:{key_name}"

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        return b"V1" + self.vault.transit_encrypt(self.key_name, dek, aad).encode()

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        wrapped = bytes(wrapped)
        if not wrapped.startswith(b"V1"):
            raise SecretError("wrapped key was not produced by Vault Transit")
        return self.vault.transit_decrypt(self.key_name, wrapped[2:].decode(), aad)


def keywrapper_from_env(vault: Optional[VaultClient] = None) -> Optional[KeyWrapper]:
    """TIA_KEK_PROVIDER=local (default when a master key is configured) or vault-transit. Returns None when no key is configured:
    the per-tenant secret store is then simply unavailable (secrets come only from the environment)."""
    provider = os.environ.get("TIA_KEK_PROVIDER", "").lower()
    if provider == "vault-transit":
        return VaultTransitWrapper(vault or VaultClient.from_env(), os.environ.get("TIA_KEK_TRANSIT_KEY", "tia"))
    keys: dict = {}
    for name, kid in (("TIA_MASTER_KEY", None), ("TIA_MASTER_KEY_PREVIOUS", None)):
        raw = env_secret(name, vault=vault)
        if raw:
            for item in raw.split(","):
                ident, sep, b64 = item.strip().partition(":")
                if not sep:
                    ident, b64 = "k1" if name == "TIA_MASTER_KEY" else f"prev{len(keys)}", item.strip()
                try:
                    keys[ident] = base64.b64decode(b64, validate=True)
                except Exception:
                    raise SecretError(f"{name} must be base64 (optionally 'id:base64')")
    if not keys:
        if provider == "local":
            raise SecretError("TIA_KEK_PROVIDER=local needs TIA_MASTER_KEY")
        return None
    current = os.environ.get("TIA_MASTER_KEY_ID") or next(iter(keys))
    return LocalKeyWrapper(keys, current)
