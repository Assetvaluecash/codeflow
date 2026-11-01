"""Encryption in transit (PL-011): refuse insecure transport instead of discovering it later.

* The database connection of a networked deployment must use TLS (sslmode require / verify-ca / verify-full). A loopback or unix-socket
  database (development, a sidecar) is exempt; anything else needs an explicit TIA_ALLOW_INSECURE_DB=true.
* Outbound calls that carry credentials (the GitHub API, Vault, a git remote with a token) must be https; plain http is only allowed to
  this machine, or when the operator explicitly accepts the risk (TIA_ALLOW_INSECURE_OUTBOUND=true).
* The server can terminate TLS itself (TIA_TLS_CERTFILE / TIA_TLS_KEYFILE) and sends HSTS on https requests.
"""
from __future__ import annotations

import os
from typing import Optional

import psycopg.conninfo

from .credentials import SecretError, is_loopback, require_secure_url

SECURE_SSLMODES = ("require", "verify-ca", "verify-full")


class TransportError(SecretError):
    pass


def allow_insecure(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in ("1", "true", "yes")


def check_db_tls(dsn: str, allow: Optional[bool] = None) -> str:
    """Raises TransportError unless the DSN is local or demands TLS. Returns the DSN unchanged."""
    allow = allow_insecure("TIA_ALLOW_INSECURE_DB") if allow is None else allow
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    hosts = [h for h in (info.get("host") or "").split(",") if h]
    local = not hosts or all(h.startswith("/") or is_loopback(h) for h in hosts)
    mode = (info.get("sslmode") or "prefer").lower()
    if local or allow or mode in SECURE_SSLMODES:
        return dsn
    raise TransportError(f"the database connection to {','.join(hosts)} does not require TLS (sslmode={mode}); set sslmode=require "
                         "(or verify-full with sslrootcert) in the DSN, or TIA_ALLOW_INSECURE_DB=true to accept the risk")


def check_outbound(url: str, what: str) -> str:
    try:
        return require_secure_url(url, what, allow_insecure("TIA_ALLOW_INSECURE_OUTBOUND"))
    except SecretError as e:
        raise TransportError(str(e))


def tls_options() -> dict:
    """uvicorn keyword arguments for terminating TLS in the process, from TIA_TLS_* (empty when TLS is terminated upstream)."""
    from .credentials import env_secret
    cert, key = os.environ.get("TIA_TLS_CERTFILE"), os.environ.get("TIA_TLS_KEYFILE")
    if bool(cert) != bool(key):
        raise TransportError("TIA_TLS_CERTFILE and TIA_TLS_KEYFILE must be set together")
    if not cert:
        return {}
    for f in (cert, key):
        if not os.path.isfile(f):
            raise TransportError(f"TLS file '{f}' does not exist")
    opts = {"ssl_certfile": cert, "ssl_keyfile": key}
    pw = env_secret("TIA_TLS_KEYFILE_PASSWORD")
    if pw:
        opts["ssl_keyfile_password"] = pw
    if os.environ.get("TIA_TLS_CA_CERTS"):
        opts["ssl_ca_certs"] = os.environ["TIA_TLS_CA_CERTS"]
    return opts
