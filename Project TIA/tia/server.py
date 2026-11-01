"""Run the TIA server:  python -m tia.server

Environment
  TIA_DATABASE_URL        PostgreSQL DSN for the runtime role (tia_app)            [required]; must demand TLS (sslmode=require|verify-full)
                          unless the database is on this machine (TIA_ALLOW_INSECURE_DB=true accepts the risk)
  TIA_TENANT_ID           the default tenant: served when authentication is off, and for unauthenticated routes such as the GitHub webhook  [required]
  TIA_AUTH_REQUIRED       "true": every API call needs a service-account token, an OIDC bearer token or a browser session (OIDC / SAML sign-in);
                          the credential names the tenant. Without it everyone is a development administrator, and the server refuses to
                          listen on a non-loopback address (TIA_INSECURE_NO_AUTH=true accepts that risk)
  TIA_PUBLIC_URL          the address users reach TIA at (https://...): browser sign-in redirects back to it; SAML needs https
  TIA_SESSION_TTL_S / TIA_SESSION_IDLE_S   browser session lifetime / idle timeout (default 28800 / 1800)
  TIA_ALLOW_PRIVATE_IDP_HOSTS  "true" lets identity-provider endpoints be http / on private addresses (development and tests only)
  TIA_IDP_CA_FILE         CA bundle for identity providers behind a private certificate authority
  TIA_ACCESS_MAX_AGE_S    how long a mirror of the source-code host's permissions grants access without a refresh (default 3600)
  TIA_WORKSPACE           where remote repositories are cloned (default ./.tia-data/repos)
  TIA_HOST / TIA_PORT     bind address (default 127.0.0.1:8080)
  TIA_ALLOW_LOCAL_PATHS   "true" to allow ingesting local paths / file:// URLs (development only)
  TIA_ALLOWED_GIT_HOSTS   comma-separated host allow-list for remote cloning (default: any)
  TIA_AUDIT_KEY           secret for keyed (HMAC) audit chain; keep it outside the database [strongly recommended]
  TIA_GITHUB_WEBHOOK_SECRET  shared secret for POST /api/v1/webhooks/github (receiver is off without it)
  TIA_GITHUB_TOKEN           token used by the worker to fetch PRs and post comments (interim; GitHub App later)
  TIA_GITHUB_API_URL         GitHub Enterprise API base (default https://api.github.com)
  TIA_GITHUB_APP_ID / TIA_GITHUB_APP_PRIVATE_KEY   the platform's GitHub App (private key as a secret reference): installation tokens replace the personal
                             token; each webhook is processed for the tenant that owns its installation (python -m tia.tenants map-installation, or an
                             administrator links it via the API); TIA_GITHUB_APP_CLIENT_ID / _CLIENT_SECRET let administrators prove they may link one;
                             TIA_GITHUB_WEB_URL (default https://github.com) for GitHub Enterprise
  TIA_ALLOW_UPLOADS          "true" (default) lets users upload a zip or folder; TIA_MAX_UPLOAD_BYTES (default 2 GiB), TIA_MAX_UPLOAD_FILES (200000)
  TIA_COMMENT_AUDIENCE       "target" (default: PR comment names only the PR's repository) or "all" (full detail)
  TIA_CHECK_RUN              "true" (default) also publishes an advisory check run (needs a GitHub App token; skipped otherwise)
  TIA_PUBLIC_URL             externally reachable base URL; comments and check runs link to <url>/analyses/<id>
  TIA_DEFAULT_STORAGE_QUOTA_BYTES   storage quota for a tenant that has none set in the registry (default: unlimited)
  TIA_TENANT_STATE_TTL       seconds a tenant's status/quota is cached (default 5): how fast a suspension takes effect

Secrets: every secret variable (TIA_DATABASE_URL, TIA_AUDIT_KEY, TIA_GITHUB_TOKEN, TIA_GITHUB_WEBHOOK_SECRET, TIA_MASTER_KEY, ...) may be a
  literal (development), a reference file:/path or vault:mount/path#key (VAULT_ADDR, VAULT_TOKEN or VAULT_TOKEN_FILE), or NAME_FILE=/path.
  TIA_MASTER_KEY (+ TIA_MASTER_KEY_ID, TIA_MASTER_KEY_PREVIOUS) or TIA_KEK_PROVIDER=vault-transit enables the per-tenant encrypted secret store.
Transport: TIA_TLS_CERTFILE / TIA_TLS_KEYFILE (TIA_TLS_KEYFILE_PASSWORD, TIA_TLS_CA_CERTS) terminate TLS in the process and enable HSTS (or TIA_HSTS=true
  behind a TLS proxy); outbound credentials (GitHub API, Vault, git tokens) require https unless the host is localhost (TIA_ALLOW_INSECURE_OUTBOUND=true).

Observability: logs are JSON lines on stderr (TIA_LOG_FORMAT=json|text, TIA_LOG_LEVEL), every request carries X-Request-ID and a W3C trace
  (traceparent in, X-Trace-Id out); /healthz (liveness), /readyz (platform readiness) and /metrics (Prometheus; TIA_METRICS_TOKEN, or this machine only,
  or TIA_METRICS_ALLOW_ANY=true); TIA_OTLP_ENDPOINT (+TIA_OTLP_HEADERS, TIA_TRACE_SAMPLE) exports traces as OTLP/HTTP; the worker serves the same
  endpoints on TIA_WORKER_METRICS_PORT.

Tenant lifecycle (create, suspend, quota, offboard) is administered with:  python -m tia.tenants   (admin credentials)

Schema is created with:  python -m tia.migrate   (admin credentials; separate from the runtime role)
"""
import logging
import os
import sys

import uvicorn

from . import obs, tracing
from .api import create_app
from .credentials import SecretError, env_secret, is_loopback, keywrapper_from_env
from .db import Database
from .secretstore import SecretStore
from .transport import check_db_tls, tls_options
from .authapi import AuthSettings
from .upload import UploadConfig
from .webhooks import ScmConfig


def build():
    dsn, tenant = env_secret("TIA_DATABASE_URL"), os.environ.get("TIA_TENANT_ID")
    if not dsn or not tenant:
        sys.exit("TIA_DATABASE_URL and TIA_TENANT_ID are required (see tia/server.py)")
    hosts = [h.strip() for h in os.environ.get("TIA_ALLOWED_GIT_HOSTS", "").split(",") if h.strip()]
    audit_key = env_secret("TIA_AUDIT_KEY")
    db = Database(check_db_tls(dsn))
    wrapper = keywrapper_from_env()
    return create_app(db, tenant, os.environ.get("TIA_WORKSPACE", ".tia-data/repos"),
                      allow_local=os.environ.get("TIA_ALLOW_LOCAL_PATHS", "").lower() in ("1", "true", "yes"),
                      allowed_hosts=hosts or None,
                      audit_key=audit_key.encode() if audit_key else None,
                      secrets=SecretStore(db, tenant, wrapper) if wrapper else None,
                      hsts=bool(tls_options()) or os.environ.get("TIA_HSTS", "").lower() in ("1", "true", "yes"),
                      scm=ScmConfig.from_env(), uploads=UploadConfig.from_env(), auth=AuthSettings.from_env(),
                      gate_ttl=float(os.environ.get("TIA_TENANT_STATE_TTL", "5")),
                      default_quota=int(os.environ["TIA_DEFAULT_STORAGE_QUOTA_BYTES"]) if os.environ.get("TIA_DEFAULT_STORAGE_QUOTA_BYTES") else None)


def main():
    obs.setup_logging()
    tracing.configure_from_env(logging.getLogger("tia.trace"))
    host = os.environ.get("TIA_HOST", "127.0.0.1")
    if not AuthSettings.from_env().required and not is_loopback(host) and os.environ.get("TIA_INSECURE_NO_AUTH", "").lower() not in ("1", "true", "yes"):
        sys.exit(f"refusing to listen on {host} with authentication off: everyone who can reach it would be an administrator. "
                 "Set TIA_AUTH_REQUIRED=true (and TIA_PUBLIC_URL), or TIA_INSECURE_NO_AUTH=true to accept the risk.")
    try:
        app, tls = build(), tls_options()
    except SecretError as e:
        sys.exit(f"configuration error: {e}")
    uvicorn.run(app, host=host, port=int(os.environ.get("TIA_PORT", "8080")),
                log_level=os.environ.get("TIA_LOG_LEVEL", "info").lower(), log_config=None, access_log=False, **tls)


if __name__ == "__main__":
    main()
