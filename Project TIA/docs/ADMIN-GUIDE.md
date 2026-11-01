# TIA administrator guide

For the people who install, configure and operate TIA. Users read the [user guide](USER-GUIDE.md); programmers read the [API reference](API.md).
Everything here describes what the software does today; the last section lists what it does **not** do.

> **Verification status.** The features below are tested against a real PostgreSQL, real git repositories, and local stand-ins for GitHub,
> an OpenID Connect provider, a SAML identity provider, Vault and an OpenTelemetry collector. **No real GitHub App, identity provider,
> Vault or collector has been used**, and the CI workflow has never run on GitHub Actions. Expect to verify those integrations in your environment.

## 1. What you run

| Part | Command | Role |
|---|---|---|
| API server and web interface | `python -m tia.server` | Serves the UI and `/api/v1`, signs people in, receives GitHub webhooks, queues work |
| Worker | `python -m tia.worker` | Runs queued analyses and pull-request jobs, publishes PR comments and check runs |
| PostgreSQL | your database | The only store: graph, evidence snapshots, jobs, audit log, identities. Every tenant table has forced row-level security |
| Optional | Vault, an OTLP collector, a GitHub App | Key management, traces, GitHub integration |

Two database roles are created by the migration: `tia_app` (the server) and `tia_worker` (the worker). Neither is an owner and neither can bypass row-level security.
The **admin DSN** (an owner or superuser) is used only by `python -m tia.migrate` and `python -m tia.tenants`; never give it to the server or worker.

## 2. Install

Requirements: Python 3.10 or newer, `git` on the path, PostgreSQL 14 or newer (tested on 16).

```
python -m venv .venv
.venv/bin/pip install -e ".[api]"          # Windows: .venv\Scripts\pip
```

Third-party source code is only **read** (cloned and parsed). TIA never builds or runs it.

## 3. Create the schema

```
export TIA_ADMIN_DATABASE_URL='postgresql://owner:...@db.example.com/tia?sslmode=verify-full'
export TIA_APP_PASSWORD='<password for tia_app>'
export TIA_WORKER_PASSWORD='<password for tia_worker>'      # defaults to TIA_APP_PASSWORD
python -m tia.migrate
```

Migrations are numbered and idempotent; run `python -m tia.migrate` again after every upgrade, **before** restarting the server and worker.
A database that is not on this machine must demand TLS (`sslmode=require`, `verify-ca` or `verify-full`); `TIA_ALLOW_INSECURE_DB=true` accepts the risk.

## 4. Tenants (organisations)

One deployment serves many tenants; the credential on each request names the tenant. Tenants are managed with the admin DSN:

```
python -m tia.tenants create acme-bank --display-name "Acme Bank" --plan enterprise --quota-bytes 50000000000
python -m tia.tenants list
python -m tia.tenants show acme-bank
python -m tia.tenants suspend acme-bank --reason "contract lapsed"     # API answers 403 tenant_suspended; queued jobs are not claimed
python -m tia.tenants resume acme-bank
python -m tia.tenants quota acme-bank 100000000000                     # bytes, or "none"
python -m tia.tenants usage acme-bank --workspace /var/lib/tia/repos/<tenant id>
python -m tia.tenants events acme-bank                                 # lifecycle history (metadata only)
python -m tia.tenants offboard acme-bank --confirm acme-bank --workspace /var/lib/tia/repos/<tenant id>
python -m tia.tenants verify-deletion acme-bank
```

`offboard` is irreversible: it deletes every row of the tenant in every table and the listed workspace directories, then issues a deletion certificate
(row counts per table and a hash). A tenant name is never reused. Storage quota counts database rows plus workspace bytes (approximate); when it is
used up, calls that add data answer `403 storage_quota_exceeded`.

## 5. Run

```
export TIA_DATABASE_URL='postgresql://tia_app:...@db.example.com/tia?sslmode=verify-full'
export TIA_TENANT_ID='<id of the default tenant>'        # used for unauthenticated routes only (see below)
export TIA_AUTH_REQUIRED=true
export TIA_PUBLIC_URL='https://tia.example.com'
export TIA_AUDIT_KEY='file:/run/secrets/tia-audit-key'
export TIA_HOST=0.0.0.0 TIA_PORT=8080
python -m tia.server

export TIA_WORKER_DATABASE_URL='postgresql://tia_worker:...@db.example.com/tia?sslmode=verify-full'
python -m tia.worker
```

**Production checklist**

1. `TIA_AUTH_REQUIRED=true` and `TIA_PUBLIC_URL`. The server refuses to listen on a non-loopback address without authentication unless `TIA_INSECURE_NO_AUTH=true`.
2. Terminate TLS in front of TIA (or set `TIA_TLS_CERTFILE`/`TIA_TLS_KEYFILE`); HSTS is sent on https. SAML requires an https public URL.
3. `TIA_AUDIT_KEY`: a secret held outside the database makes the audit hash chain tamper-evident even against someone who can edit rows.
4. `TIA_MASTER_KEY` (or Vault Transit) if tenants will store secrets (identity-provider client secrets, a GitHub token).
5. `TIA_METRICS_TOKEN` if `/metrics` is scraped from another machine (otherwise it answers only on this machine).
6. Put a reverse proxy with **rate limiting** in front: TIA does not rate-limit sign-in or token attempts itself (tokens and session secrets are 256-bit random values).
7. Several API instances can run behind a load balancer: sessions and queued work live in PostgreSQL, so any instance can serve any request.

The **default tenant** (`TIA_TENANT_ID`) serves requests that carry no credential: the web page shell, health checks and, when no GitHub App is configured, the GitHub webhook.
With a GitHub App, each webhook is processed for the tenant that owns its installation (section 9).

Every secret variable accepts a literal (development only), a file reference `file:/path`, a Vault reference `vault:mount/path#key`, or the `NAME_FILE=/path` form.

## 6. Signing in: identity providers, users, roles, tokens

### Roles

| Role | May |
|---|---|
| `viewer` | read reports, graph, evidence, repositories, jobs, configuration |
| `engineer` | viewer, plus run/reproduce/cancel analyses, publish to a PR, register/sync/upload repositories, rescan |
| `release_manager` | engineer, plus change the tenant configuration and refresh the repository-access mirror |
| `auditor` | viewer, plus read the audit log and verify its chain (cannot run analyses) |
| `administrator` | everything, plus identity providers, users, tokens, sessions, repository access, GitHub installations, deleting repositories |

Roles are additive. They come from the identity provider's groups through the provider's `role_map`, unless an administrator assigns roles to a person
(then the assignment wins; changing or removing roles, or disabling a person, ends their sessions at once). `default_role` may never be `administrator`.

### First identity provider (bootstrap)

The first provider is created from the command line, because the API needs a signed-in administrator. Put the settings in a file:

<!-- check: idp-oidc -->
```json
{
  "name": "corp",
  "protocol": "oidc",
  "config": {
    "issuer": "https://login.example.com/realms/acme",
    "audience": "tia-client-id",
    "groups_claim": "groups",
    "scm_login_claims": {"github": "preferred_username"},
    "role_map": {
      "tia-admins": "administrator",
      "tia-engineers": "engineer",
      "tia-release": "release_manager",
      "tia-audit": "auditor"
    },
    "default_role": "viewer"
  }
}
```

```
export TIA_ALLOW_PRIVATE_IDP_HOSTS=            # leave unset in production: provider URLs must be https on public addresses
python -m tia.tenants idp-set acme-bank --file idp.json --actor terraform
python -m tia.tenants idp-list acme-bank
```

* `issuer` + `audience` (the OAuth client id) must be unique across all tenants: they route a bearer token to its tenant.
* Discovery (`<issuer>/.well-known/openid-configuration`) is used unless you give `authorization_endpoint`, `token_endpoint` and `jwks_uri`.
* A confidential client's secret is **never** put in this file. Create the provider through the API with `client_secret` (it is stored encrypted) or store the secret yourself and reference it by name in `client_secret_ref`.
* Every problem in a configuration is listed at once, with "did you mean" hints. Unknown settings and secret-looking keys are refused.
* `scm_login_claims` says which claim holds the person's GitHub login; it links them to the repositories they may read (section 8).
* Redirect URI to register at the provider: `<TIA_PUBLIC_URL>/auth/callback`.

SAML 2.0 is configured the same way with `"protocol": "saml"`. Register the service provider metadata (`<TIA_PUBLIC_URL>/auth/saml/metadata`; ACS URL `<TIA_PUBLIC_URL>/auth/saml/acs`)
at the identity provider. The assertion **must be signed** by one of the certificates you list (list two during a rotation); unsigned or SHA-1-signed assertions,
encrypted assertions and IdP-initiated responses are refused.

<!-- check: idp-saml -->
```json
{
  "name": "corp-saml",
  "protocol": "saml",
  "config": {
    "entity_id": "https://idp.example.com/metadata",
    "sso_url": "https://idp.example.com/sso",
    "certificates": ["-----BEGIN CERTIFICATE-----\nMIIB...the identity provider's signing certificate...\n-----END CERTIFICATE-----"],
    "email_attribute": "email",
    "name_attribute": "displayName",
    "groups_attribute": "groups",
    "role_map": {"tia-admins": "administrator", "tia-engineers": "engineer"}
  }
}
```

After the first administrator has signed in, use the web interface (section 6 "Administration") or the `/api/v1/admin` endpoints (see the [API reference](API.md#administration-apiv1admin)) for everything else.

### Service-account tokens

For CI and scripts. Created by an administrator (UI, `POST /api/v1/admin/tokens`, or the CLI), shown **once**, only a hash is stored:

```
python -m tia.tenants token-create acme-bank --name ci-bot --roles engineer --repos payments-service,orders-service --days 90
python -m tia.tenants token-list acme-bank
python -m tia.tenants token-revoke acme-bank <token id>
```

A token has roles and either `"*"` (every repository) or a list of repository names. It cannot be an administrator (least privilege); for a break-glass token an
operator can add `--allow-administrator` on the command line. Use it as `Authorization: Bearer tia_...`.

### Sessions

Browser sessions last 8 hours and expire after 30 minutes idle (`TIA_SESSION_TTL_S`, `TIA_SESSION_IDLE_S`). Administrators can list and revoke sessions.
A session cookie is `HttpOnly`, `SameSite=Lax` and `Secure` on https; every state-changing call also needs the CSRF value the page gets from `/auth/me`.

## 7. Tenant configuration

Per-tenant behaviour is configured as JSON. Every save is a new immutable version (`GET /api/v1/tenant/config/versions`), identical saves are no-ops,
any version can be restored (as a new version), and reports record which configuration produced them. Needs `release_manager` or `administrator`.

<!-- check: tenant-config -->
```json
{
  "limits": {"max_depth": 6, "max_results": 200, "time_budget_seconds": 60, "max_concurrent_jobs": 2},
  "paths": {"strip_prefixes": ["/api/gateway"]},
  "tests": {"naming_convention": true},
  "severity": {"overrides": {"SEV-BREAKING-CRITICAL": "high"}, "inferred_cap": "medium", "stale_cap": "low"},
  "materiality": {"severities": ["high", "critical"], "breaking_on_path": true},
  "criticality": {"payments-service": "critical"},
  "comments": {"audience": "target"}
}
```

* `limits` are defaults **and ceilings**: a caller asking for a larger depth gets the tenant's value. `max_concurrent_jobs` can only lower the worker's per-tenant cap.
* `paths.strip_prefixes` lists URL prefixes that are not part of a service's own routes (an API gateway, or **servlet context paths set in external configuration**).
  TIA does not read context paths from configuration files: if a Feign or WebClient call says `/accounts/{name}` while the controller maps `/{name}`, list `/accounts` here.
* A severity override never hides anything: the finding keeps its rule id and records the override.
* `comments.audience`: `target` (the default) names only the pull request's own repository in its comment; `readers` also names repositories that every reader of the pull request is provably allowed to read
  (needs the repository-access mirror); `all` shows everything (with authentication on it is treated as `readers`).
* Validate before saving: `POST /api/v1/tenant/config/validate`, or the **Validate** button.

From the command line: `python -m tia.tenants config-show acme-bank` and `python -m tia.tenants config-set acme-bank --file config.json --comment "why" --actor terraform`.

## 8. Repository access

Who may read which repository is decided per repository (`GET /api/v1/admin/repo-access`):

| Mode | Readers | Default for |
|---|---|---|
| `scm` | the people the source-code host lists (collaborators, teams, organisation members), matched by the GitHub login on their profile | repositories hosted on GitHub |
| `restricted` | people explicitly granted by an administrator | - |
| `tenant` | every member of the tenant | uploads and local paths |

* The host's list is **mirrored**: `POST /api/v1/admin/repo-access/sync` (needs `release_manager`, or `administrator`; the token needs push access to list collaborators). Run it on a schedule with a
  `release_manager` service token, e.g. hourly from cron. A mirror older than `TIA_ACCESS_MAX_AGE_S` (default 3600 seconds) **grants nothing**, so a failing sync fails closed.
* A person with no GitHub login on their profile sees no `scm` repository. Set the login from a claim (`scm_login_claims`) or by hand in the people table.
* A repository a person may not read answers `404`; reports are redacted for them and say how many findings were omitted.
* Administrators are **not** exempt: they can change modes and grants (audited) but see nothing they are not allowed to read.

## 9. GitHub

**With a GitHub App (recommended)**

1. Create a GitHub App with a webhook (URL `<TIA_PUBLIC_URL>/api/v1/webhooks/github`, a secret, event *Pull request*) and repository permissions Contents: read, Pull requests: write, Checks: write, Metadata: read.
   To mirror repository access, GitHub requires more (listing collaborators needs push or administration rights); the sync reports exactly what GitHub refuses.
2. Give TIA the App: `TIA_GITHUB_APP_ID`, `TIA_GITHUB_APP_PRIVATE_KEY` (a secret reference to the PEM), `TIA_GITHUB_WEBHOOK_SECRET`; for self-service linking also `TIA_GITHUB_APP_CLIENT_ID` and `TIA_GITHUB_APP_CLIENT_SECRET`.
3. Link each customer's installation to its tenant, either as the operator: `python -m tia.tenants map-installation acme-bank github <installation id> --account acme`,
   (list them with `python -m tia.tenants installations acme-bank`, release one with `python -m tia.tenants unmap-installation acme-bank github <installation id>`),
   or as the tenant's administrator through `POST /api/v1/admin/github/installations` with GitHub's user-authorisation `code` (proof that they can see the installation: knowing an id is not enough).
4. Register repositories: **Discover** with `provider: "github-app"` lists and ingests the repositories the customer gave the installation.

Each webhook is then processed for the tenant that owns its installation; installation tokens (1 hour) are used for clone/fetch, the comment and a real check run.
If an installation is removed or suspended, or a repository is outside it, the API answers `424` and the job fails permanently with that reason.

**Interim: a personal token** (`TIA_GITHUB_TOKEN`, or a tenant's stored `github/token`) with `TIA_GITHUB_WEBHOOK_SECRET`. Comments work; check runs need an App token and are skipped.

Comments are **advisory**: they are one comment per pull request, updated in place, and never block a merge. The check run always concludes `neutral`.

## 10. Secrets and encryption

* In transit: the database connection must use TLS off-machine; outbound calls that carry credentials (GitHub, Vault) must be https unless the host is this machine.
* At rest, tenant secrets (an identity provider's client secret, a GitHub token) are encrypted with AES-256-GCM under a per-tenant key, which is itself wrapped by a key-encryption key
  held **outside** the database: `TIA_MASTER_KEY` (+ `TIA_MASTER_KEY_ID`; keep the old one in `TIA_MASTER_KEY_PREVIOUS` while rotating), or Vault Transit (`TIA_KEK_PROVIDER=vault-transit`, `VAULT_ADDR`, `VAULT_TOKEN`/`VAULT_TOKEN_FILE`, `TIA_KEK_TRANSIT_KEY`).
  Deleting a tenant's keys (offboarding does) makes every copy of its secrets, backups included, unreadable.
* Manage them with `python -m tia.secretstore` (values are read from standard input, never from arguments).
* Credentials that appear in source code, SQL, logs, audit records, job errors or reports are detected and withheld; a report says how many were withheld.

## 11. Observability

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | liveness: the process is serving |
| `GET /readyz` | readiness of the platform: database, schema version, workspace, queue consumers (not about analysis results) |
| `GET /metrics` | Prometheus text; needs `TIA_METRICS_TOKEN` (bearer) when scraped from another machine |

Logs are JSON lines on stderr (`TIA_LOG_FORMAT=json|text`, `TIA_LOG_LEVEL`); every request carries `X-Request-ID`, and W3C trace context is accepted and propagated through the queue into the worker.
`TIA_OTLP_ENDPOINT` (with `TIA_OTLP_HEADERS`, `TIA_TRACE_SAMPLE`) exports traces as OTLP/HTTP. The worker serves the same probes on `TIA_WORKER_METRICS_PORT`.
No dashboards or alert rules are shipped.

## 12. Operations

* **Upgrade:** stop the worker, run `python -m tia.migrate`, start the server and worker. A new extractor version invalidates cached evidence for that language automatically.
* **Backup:** back up PostgreSQL with your usual tooling. The **workspace** (`TIA_WORKSPACE`, one directory per tenant when authentication is on) holds clones, which can be re-fetched,
  **and uploaded code, which cannot**: include the workspace in backups if people upload code.
* **Audit:** `GET /api/v1/audit` (auditor/administrator) and `GET /api/v1/audit/verify`. Entries are hash-chained; with `TIA_AUDIT_KEY` the chain is also keyed. Every authenticated action records who did it.
* **Capacity:** `TIA_DEFAULT_STORAGE_QUOTA_BYTES`, per-tenant quotas, `TIA_MAX_UPLOAD_BYTES` (default 2 GiB), `TIA_MAX_UPLOAD_FILES`, `TIA_ALLOW_UPLOADS`, the tenant's `limits`.

## 13. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every call answers `401 authentication_required` | no credential sent, or `TIA_AUTH_REQUIRED=true` and the page was opened without signing in |
| Sign-in page says "No sign-in method is available" | wrong organisation name, tenant suspended, or no enabled identity provider |
| Sign-in fails with `idp_unreachable` / `idp_misconfigured` | provider URL not https or not on a public address, discovery issuer differs from `issuer`, or a client secret is missing from the secret store |
| SAML sign-in says it needs an https public URL | `TIA_PUBLIC_URL` is http |
| `403 no_role` after a successful sign-in | none of the person's groups is in `role_map` and there is no `default_role` |
| A person sees no repositories | no GitHub login on their profile, the mirror was never synced or is older than `TIA_ACCESS_MAX_AGE_S`, or they are not listed on GitHub |
| `424` on sync/publish | the GitHub App installation was removed or suspended, or does not cover that repository |
| An analysis comes back `partial` or `complete_with_gaps` | read the report's **unknowns**: unparseable files, unreachable repositories, unsupported clients; they say what is missing |
| Cross-service calls are not linked | a context path or gateway prefix is missing from `paths.strip_prefixes` |
| `/readyz` is not ready | database unreachable, migrations not applied, workspace not writable, or no worker heartbeat |

## 14. What TIA does not do

* No rate limiting, no WAF, no account lockout (use a reverse proxy).
* No Azure DevOps webhooks, PR comments or permission mirroring yet; Azure DevOps repositories can be ingested with a personal token.
* No container images, Compose file or Helm chart yet; no backup/restore tooling beyond PostgreSQL's.
* Group changes at the identity provider reach a signed-in person at their next sign-in; an administrator's edits reach bearer-token users within 30 seconds.
* Policy evaluation, runtime/event evidence, ITSM integration and the other later-release features are not built.
* Real-world interoperability with specific identity providers (group-overage claims, signed requests, single logout) and with real GitHub Apps is untested.

## 15. Environment reference

| Variable | Meaning |
|---|---|
| `TIA_DATABASE_URL` | server DSN for role `tia_app` (required) |
| `TIA_WORKER_DATABASE_URL` | worker DSN for role `tia_worker` |
| `TIA_ADMIN_DATABASE_URL` | owner DSN for `tia.migrate` / `tia.tenants` only |
| `TIA_APP_PASSWORD`, `TIA_WORKER_PASSWORD` | passwords `tia.migrate` sets for the two roles |
| `TIA_ALLOW_INSECURE_DB` | accept a non-TLS database connection off-machine |
| `TIA_ALLOW_INSECURE_OUTBOUND` | accept plain http for outbound calls that carry credentials |
| `TIA_TENANT_ID` | default tenant (unauthenticated routes, webhook without an App) |
| `TIA_AUTH_REQUIRED` | require a credential on every protected route |
| `TIA_INSECURE_NO_AUTH` | allow listening on a network address without authentication |
| `TIA_PUBLIC_URL` | externally reachable base URL: sign-in redirects, links in comments and check runs |
| `TIA_SESSION_TTL_S`, `TIA_SESSION_IDLE_S` | browser session lifetime / idle timeout (28800 / 1800) |
| `TIA_ALLOW_PRIVATE_IDP_HOSTS` | development: allow identity-provider endpoints on http or private addresses |
| `TIA_IDP_CA_FILE` | CA bundle for identity providers behind a private authority |
| `TIA_ACCESS_MAX_AGE_S` | freshness limit of the repository-access mirror (3600) |
| `TIA_HOST`, `TIA_PORT` | bind address (127.0.0.1:8080) |
| `TIA_WORKSPACE` | where repositories are cloned (`./.tia-data/repos`) |
| `TIA_ALLOW_LOCAL_PATHS` | allow registering local paths and `file://` URLs (development) |
| `TIA_ALLOWED_GIT_HOSTS` | comma-separated host allow-list for cloning |
| `TIA_ALLOW_UPLOADS`, `TIA_MAX_UPLOAD_BYTES`, `TIA_MAX_UPLOAD_FILES`, `TIA_UPLOAD_SESSION_TTL_S` | code upload switch, size cap (2 GiB), file cap (200000), staged-upload lifetime (24 h) |
| `TIA_DEFAULT_STORAGE_QUOTA_BYTES` | quota for tenants without one (unlimited) |
| `TIA_TENANT_STATE_TTL` | seconds a tenant's status/quota is cached (5): how fast a suspension takes effect |
| `TIA_AUDIT_KEY` | secret for the keyed audit chain |
| `TIA_GITHUB_TOKEN`, `TIA_GITHUB_WEBHOOK_SECRET`, `TIA_GITHUB_API_URL` | interim GitHub token, webhook secret, API base (GitHub Enterprise) |
| `TIA_GITHUB_APP_ID`, `TIA_GITHUB_APP_PRIVATE_KEY`, `TIA_GITHUB_APP_CLIENT_ID`, `TIA_GITHUB_APP_CLIENT_SECRET`, `TIA_GITHUB_WEB_URL` | the GitHub App; web base for GitHub Enterprise |
| `TIA_COMMENT_AUDIENCE` | default comment audience: `target`, `readers`, `all` |
| `TIA_CHECK_RUN` | publish a neutral check run (true) |
| `TIA_MASTER_KEY`, `TIA_MASTER_KEY_ID`, `TIA_MASTER_KEY_PREVIOUS` | key-encryption key for tenant secrets, its id, the previous key during rotation |
| `TIA_KEK_PROVIDER`, `TIA_KEK_TRANSIT_KEY` | `vault-transit` and the Transit key name |
| `TIA_TLS_CERTFILE`, `TIA_TLS_KEYFILE`, `TIA_TLS_KEYFILE_PASSWORD`, `TIA_TLS_CA_CERTS` | terminate TLS in the process |
| `TIA_HSTS` | send HSTS behind a TLS-terminating proxy |
| `TIA_LOG_FORMAT`, `TIA_LOG_LEVEL` | log format (`json`) and level (`INFO`) |
| `TIA_METRICS_TOKEN`, `TIA_METRICS_ALLOW_ANY`, `TIA_METRICS_CACHE_TTL` | `/metrics` access and cache |
| `TIA_OTLP_ENDPOINT`, `TIA_OTLP_HEADERS`, `TIA_TRACE_SAMPLE`, `TIA_SERVICE_NAME` | trace export, sampling and the service name reported |
| `TIA_WORKER_METRICS_HOST`, `TIA_WORKER_METRICS_PORT` | the worker's probe and metrics listener |
| `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_TOKEN_FILE`, `VAULT_NAMESPACE`, `VAULT_CACERT` | Vault access |
