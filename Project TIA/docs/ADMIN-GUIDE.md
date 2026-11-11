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
6. Rate limiting: with authentication on, TIA limits requests per principal (`TIA_RATE_LIMIT_PER_MINUTE`, default 600), per tenant (`TIA_RATE_LIMIT_TENANT_PER_MINUTE`, 6000) and per client address for unauthenticated and failed-authentication requests (`TIA_RATE_LIMIT_ANON_PER_MINUTE`, 120), answering 429 with Retry-After; starting an analysis or pushing evidence costs 10. Limits are per API replica: set `TIA_RATE_LIMIT_REPLICAS` to the replica count. `TIA_RATE_LIMIT=false` turns it off, `true` forces it on. Behind a gateway set `TIA_TRUST_PROXY=true` so the client address comes from `X-Forwarded-For`. Keep a WAF and a global rate limit at the gateway as well (`deploy/terraform/azure`).
7. Several API instances can run behind a load balancer: sessions and queued work live in PostgreSQL, so any instance can serve any request.

The **default tenant** (`TIA_TENANT_ID`) serves requests that carry no credential: the web page shell, health checks and, when no GitHub App is configured, the GitHub webhook.
With a GitHub App, each webhook is processed for the tenant that owns its installation (section 9).

Every secret variable accepts a literal (development only), a file reference `file:/path`, a Vault reference `vault:mount/path#key`, or the `NAME_FILE=/path` form.

## 6. Signing in: identity providers, users, roles, tokens

### Roles

| Role | May |
|---|---|
| `viewer` | read reports, graph, evidence, repositories, jobs, configuration |
| `engineer` | viewer, plus run/reproduce/cancel analyses, publish to a PR, register/sync/upload repositories, rescan, push evidence (CI results, coverage, telemetry, deployments), record owner approvals, attach to a change record |
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
  "paths": {"strip_prefixes": ["/api/gateway"], "exclude_patterns": ["vendor/**", "*.min.js"]},
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
* `paths.exclude_patterns` (up to 100 globs) are files TIA does not analyse at all: vendored code, generated or minified files. `*` stays within one folder, `**` crosses folders,
  and a pattern without a folder also matches the file name anywhere (`*.min.js`). Other characters are literal. Excluding files only shrinks what is analysed: anything defined in an
  excluded file is invisible, so a change to it reaches nothing. Every report records the patterns it ran with, and a different list is a different cache entry.
* A severity override never hides anything: the finding keeps its rule id and records the override.
* `comments.audience`: `target` (the default) names only the pull request's own repository in its comment; `readers` also names repositories that every reader of the pull request is provably allowed to read
  (needs the repository-access mirror); `all` shows everything (with authentication on it is treated as `readers`).
* Validate before saving: `POST /api/v1/tenant/config/validate`, or the **Validate** button.
* `compatibility.rules` (rule id -> `breaking` | `compatible`) changes how a contract change is classified: REST rules `request_enum_value_removed`,
  `response_enum_value_added`, `request_constraint_tightened`, `request_param_required`, `response_shape_changed`, `success_status_changed`,
  `auth_added`, `auth_changed`, `auth_removed`; event rules `event_field_removed`, `event_field_type_changed`, `event_enum_value_added`,
  `event_enum_value_removed`, `event_field_added`, `event_required_field_added`, `event_schema_removed`, `event_topic_no_longer_produced`.
* `ownership` (service -> team) and `criticality` can be filled from a service catalog: `POST /api/v1/registry/import?format=backstage|csv|json`
  (a Backstage `catalog-info.yaml`, a CSV `service,owner,criticality`, or JSON). It saves a new configuration version. A repository's own `catalog-info.yaml`
  is also read as a fallback for its `tia-services.yaml`.
* `policies` holds the release policy (section 16) and `itsm` the change-management integration (section 17).

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
   Quicker: `python tools/github_app.py manifest --url <TIA_PUBLIC_URL> --org <org> --out register.html`, open the page, confirm on GitHub, then
   `python tools/github_app.py convert --code <code from the redirect> --out <secure folder>` writes the App id, key and secrets to files.
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

**Azure DevOps** (cloud or Azure DevOps Server)

1. In the project's *Service hooks*, add a *Web Hooks* subscription for **Pull request created** and another for **Pull request updated**, URL `<TIA_PUBLIC_URL>/api/v1/webhooks/ado`,
   with basic-authentication user name and password; give TIA the same values in `TIA_ADO_HOOK_USERNAME` / `TIA_ADO_HOOK_PASSWORD`.
2. A personal access token with *Code: read* and *Pull request threads: read & write* in `TIA_ADO_TOKEN` (or each tenant's stored secret `ado/token`). Azure DevOps Server: `TIA_ADO_API_URL`.
   The token is only ever sent to `dev.azure.com`, `*.visualstudio.com` or that configured host.
3. Register the repositories (Git URL, or **Discover** with provider `ado`). Events of an organisation that the operator mapped with
   `python -m tia.tenants map-installation <tenant> ado <collection id>` go to that tenant; others go to the default tenant.

Each pull request gets **one comment thread**, updated in place and created *closed* so a "resolve all comments" policy is never blocked, and a pull-request **status**
(`tia/impact-analysis`) whose state is always `notApplicable`: advisory, never a gate. Repository access is not mirrored from Azure DevOps: its repositories are `restricted`
until an administrator grants them, and comments name only the pull request's own repository.

**Knowledge Fabric V2.5 agents** can read finished reports through `POST /query/tia`; see [the adapter page](KF-ADAPTER.md).

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

* No WAF and no account lockout (use the gateway; `deploy/terraform/azure` puts Front Door's WAF in front). Rate limiting is per API replica, not global.
* No permission mirroring from Azure DevOps (its repositories are restricted until granted); the Azure DevOps connector has only been tested against a stand-in.
* Group changes at the identity provider reach a signed-in person at their next sign-in; an administrator's edits reach bearer-token users within 30 seconds.
* It never queries CI, telemetry or deployment systems during an analysis: evidence is pushed to TIA first (section 15).
* Events are analysed for Kafka only (code and declarations); other messaging is reported as an unknown.
* The ServiceNow integration and the MCP server are tested against stand-ins only; real identity-provider, GitHub App and ServiceNow interoperability is untested.

## 15. Evidence: CI results, coverage, runtime, deployments

Evidence is **pushed** to TIA (from a CI step, a telemetry export or a deployment pipeline) and kept per tenant; an analysis reads what is held and records
which records it used, so a reproduction gives the same result. Use a service token with the `engineer` role.

| Endpoint | Body |
|---|---|
| `POST /api/v1/evidence/test-results?repo=R&revision=SHA&build_id=B&environment=E` | JUnit XML (`format=junit`, default) or JSON (`format=json`: pytest-json-report, or `[{name, classname, result}]`) |
| `POST /api/v1/evidence/coverage?repo=R&revision=SHA` | Cobertura XML, JaCoCo XML, or coverage.py JSON (`coverage json --show-contexts` also says which test executed each line) |
| `POST /api/v1/evidence/runtime?format=otlp` | OpenTelemetry traces as OTLP/JSON (or `format=edges`: `[{from_service, to_service, method, path, environment, from_ts, to_ts, count}]`) |
| `POST /api/v1/evidence/deployments` | `[{repo or service, environment, revision, artifact, deployed_at}]` |

Bodies are capped at 25 MB; XML with a DOCTYPE is refused. `GET /api/v1/evidence?kind=...` lists records, `DELETE /api/v1/evidence/{rid}` removes one,
`GET /api/v1/environments` shows what runs where.

## 16. Release policy

`policies` in the tenant configuration (section 7):

```json
{"policies": {"mode": "enforcing", "rules": [
  {"id": "no-breaking-critical", "type": "no_breaking_change", "applies_to": {"criticality": ["critical"]}},
  {"id": "tests-pass", "type": "tests_passed", "params": {"max_priority": 2}},
  {"id": "owners", "type": "owner_approval", "mandatory": false}]}}
```

Rule types: `no_breaking_change`, `tests_passed`, `contract_validated`, `owner_approval`, `no_unresolved_dependencies`, `analysis_complete`, `production_evidence`.
Each yields `pass`, `fail`, `not_evaluated` (a data source is missing: never a pass) or `requires_review`.

* `advisory` (the default) never blocks: the GitHub check is `neutral` and the Azure DevOps status `notApplicable`.
* `enforcing`: a failed **mandatory** rule makes the check `failure` (Azure DevOps `failed`); `not_evaluated` or `requires_review` on a mandatory rule gives
  `action_required` (`pending`). Whether that blocks a merge is decided by **your** branch protection: TIA never changes it.

Owners approve with `POST /api/v1/impact-analyses/{aid}/approvals`; `GET /api/v1/impact-analyses/{aid}/policy` re-evaluates with the approvals recorded since.

## 17. ServiceNow change records

Set `"itsm": {"provider": "servicenow", "instance_url": "https://acme.service-now.com"}` and store the credentials as tenant secrets
`itsm/servicenow/username` and `itsm/servicenow/password` (or `itsm/servicenow/token`). `POST /api/v1/impact-analyses/{aid}/change-record` attaches the
report summary to the analysis's change record (or the one given). It reads the record and adds ONE attachment per analysis; it never creates,
updates, approves or transitions a change record. The account needs read access to `change_request` and the right to add attachments.
`TIA_ALLOW_PRIVATE_ITSM_HOSTS` lets it reach a private address (laboratories only).

## 18. MCP for coding assistants

`POST /mcp` is a read-only Model Context Protocol server (streamable HTTP). Configure the assistant with the URL `https://<tia>/mcp` and a `viewer` service token.
Tools: `get_impact_report`, `find_impact_report`, `get_test_scope`, `get_policy_outcome`. There is no write tool. The assistant sees only what the token may read.

## 18a. Plans, regions and customer-managed keys

```
python -m tia.tenants limits-set acme-bank '{"max_repositories": 200, "max_users": 500, "max_analyses_per_month": 20000, "max_evidence_records_per_month": null}'
python -m tia.tenants region-set acme-bank eu           # or none
python -m tia.tenants key-set acme-bank aws-kms:arn:aws:kms:eu-west-1:111122223333:key/<id>   # or azure-kv:https://<vault>.vault.azure.net/keys/<name>, vault-transit:<name>, none
python -m tia.tenants meter acme-bank --month 2026-10 --format csv
```

* **Limits** (`null` = unlimited): a request over a limit answers `402 plan_limit`; adding a user over `max_users` refuses that sign-in. `GET /api/v1/usage` shows the month's usage.
* **Region**: each deployment declares its region in `TIA_REGION` (`deploy/helm/tia/values-region-eu.yaml`). A tenant pinned to another region is answered `421 wrong_region` and its jobs
  are not claimed there, so its data is processed only where it is pinned. Regions: eu, us, apac, uk, ca, au, in, jp, ch, sa.
* **Customer-managed key**: `key-set` re-wraps the tenant's data keys under the new key; revoking the key at the customer's KMS makes the tenant's secrets unreadable.
  AWS KMS uses `TIA_AWS_ACCESS_KEY_ID`, `TIA_AWS_SECRET_ACCESS_KEY`, `TIA_AWS_SESSION_TOKEN` (optional), `TIA_AWS_KMS_ENDPOINT` (optional, e.g. a VPC endpoint);
  Azure Key Vault uses `TIA_AZURE_TENANT_ID`, `TIA_AZURE_CLIENT_ID`, `TIA_AZURE_CLIENT_SECRET` (`TIA_AZURE_LOGIN_URL` and `TIA_AZURE_KEYVAULT_TEST_URL` exist for stand-ins in tests only).
* **Dedicated deployment**: one tenant per installation with `deploy/helm/tia/values-dedicated.yaml`.
* **Tenant backup**: `python -m tia.backup export|restore|verify`; exports are encrypted with AES-256-GCM under `TIA_BACKUP_KEY` (base64, 32 bytes) and bound to the tenant id.

## 17a. GitLab and Bitbucket

| | GitLab (gitlab.com or self-managed) | Bitbucket (Cloud or Data Center) |
|---|---|---|
| Server URL | `TIA_GITLAB_URL` (default `https://gitlab.com`) | `TIA_BITBUCKET_URL` (default `https://bitbucket.org`) |
| Webhook | project/group webhook to `https://<tia>/api/v1/webhooks/gitlab`, *Merge request events*, secret token = `TIA_GITLAB_WEBHOOK_SECRET` | repository webhook to `https://<tia>/api/v1/webhooks/bitbucket`, pull request created/updated (Data Center: opened, source branch updated, modified), secret = `TIA_BITBUCKET_WEBHOOK_SECRET` |
| Token | `TIA_GITLAB_TOKEN` or the tenant secret `gitlab/token` (scopes `api`, `read_repository`) | `TIA_BITBUCKET_TOKEN` or the tenant secret `bitbucket/token` (repository read, pull-request write, repository admin to read permissions) |
| Published | one merge-request note, updated in place, and a commit status `tia/impact-analysis` | one pull-request comment, updated in place, and a build status `tia-impact-analysis` |
| Access mirror | project members with Reporter or higher (`POST /api/v1/admin/repo-access/sync`) | repository and project user permissions |

Register repositories by their clone URL; the provider is recognised from the host (gitlab.com, bitbucket.org, or the configured server). People are matched
by their login on that host: add `"gitlab": "<claim>"` or `"bitbucket": "<claim>"` to `scm_login_claims`. The status is advisory (success) unless the
policy is enforcing. Tested against stand-ins only.

## 17b. Team coordination

Material findings can be sent to the owning team of each affected component (owners come from `ownership`, `tia-services.yaml` or the registry import):

```json
{"coordination": {"auto_notify": true,
  "channels": {"team-payments": {"type": "slack"}, "team-ledger": {"type": "email", "to": "ledger-oncall@bank.example"},
               "team-cards": {"type": "servicenow_task", "assignment_group": "Cards Engineering"}},
  "default": {"type": "teams"}}}
```

* `slack`, `teams`, `webhook`: the incoming-webhook URL is the tenant secret `coordination/<team>/webhook` (or `coordination/default/webhook`).
* `email`: SMTP from `TIA_SMTP_HOST`, `TIA_SMTP_PORT` (587), `TIA_SMTP_USER`, `TIA_SMTP_PASSWORD`, `TIA_SMTP_FROM`; STARTTLS is required off this machine.
* `servicenow_task`: a task in the instance of the `itsm` setting, one per analysis and group (this is the only record TIA creates in ServiceNow).
* `TIA_ALLOW_PRIVATE_NOTIFY_HOSTS` lets webhooks reach a private address (laboratories only).

Each team is notified once per analysis (a failed delivery is retried by the next `POST /api/v1/impact-analyses/{aid}/notifications`); teams acknowledge with
`POST /api/v1/impact-analyses/{aid}/acknowledgements`; `GET /api/v1/impact-analyses/{aid}/coordination` shows the status. The policy rule type
`teams_acknowledged` passes when every affected team has acknowledged.

## 17c. Calibration and trends

Reviewers record verdicts on findings (`POST /api/v1/impact-analyses/{aid}/verdicts`: confirmed, false_positive, irrelevant). `GET /api/v1/calibration`
gives precision per severity rule, inference rule, resolution strategy, evidence status, impact type and configuration version; incidents that name a
change, or that a person linked to one, confirm the findings about the incident's service. Use it to tune `severity.overrides` and the caps: a
change applies to new analyses only, stored reports are never re-scored. `GET /api/v1/trends?group=application|team&period=week|month|quarter` reports
change volume, breaking share, unresolved-dependency share, missing-verification share and incident associations: descriptive figures, not predictions.

## 18b. Retention, backup and audit reporting

Retention is a tenant setting (section 7), applied by a job the operator schedules (daily is enough) with the admin DSN:

```json
{"retention": {"graph_days": 90, "analysis_days": 730, "runtime_days": 180, "incident_days": 1095, "audit_days": 2555}}
```

```
python -m tia.retention enforce                    # every active tenant; --tenant acme-bank for one
```

A missing or `null` value keeps data forever (the default). Minimums: 30 days for analyses and incidents, 365 for audit. Removing audit entries keeps an
**anchor** (the last removed entry's sequence number and hash), so `GET /api/v1/audit/verify` still verifies the remaining chain and reports the anchor;
the purge itself is the first entry written after it. `GET /api/v1/audit/report?start=...&end=...` summarises a period by event and by actor.

Tenant backup (in addition to the database's own point-in-time recovery, see [OPERATIONS.md](OPERATIONS.md)):

```
python -m tia.backup export acme-bank --out acme.tiabak        # encrypted with TIA_BACKUP_KEY
python -m tia.backup restore acme.tiabak                       # into the database of --admin-dsn; never merges into an existing tenant
python -m tia.backup verify acme-bank acme.tiabak              # row counts and digests of every table
```

## 18c. Air-gapped installations

Set `TIA_AIRGAPPED=true`. Every outbound connection (GitHub, Azure DevOps, git remotes, identity-provider discovery, ServiceNow, cloud KMS,
trace export) is then refused unless its host is this machine or is listed in `TIA_AIRGAP_ALLOWED_HOSTS` (for example
`git.bank.internal,*.corp.example`). A refused call fails with an error naming the host; nothing is silently skipped. Register repositories from
internal remotes or by upload, push evidence to TIA, and use Vault Transit or `TIA_MASTER_KEY` rather than a cloud KMS.
Install from a wheel bundle built on a connected machine (`pip download . -d bundle/`, copied in, then `pip install --no-index -f bundle/ tia`) or from
the container image loaded with `docker load`.

## 18d. Language adapters

Built in: Python, Java, Go, C#/.NET, Kotlin, TypeScript/JavaScript (see [ADAPTER-SDK.md](ADAPTER-SDK.md) for what each recognises). Further languages
are installed as Python packages that declare a `tia.adapters` entry point; install them on the server and the worker and restart both.

## 19. Environment reference

| Variable | Meaning |
|---|---|
| `TIA_DATABASE_URL` | server DSN for role `tia_app` (required) |
| `TIA_WORKER_DATABASE_URL` | worker DSN for role `tia_worker` |
| `TIA_ADMIN_DATABASE_URL` | owner DSN for `tia.migrate` / `tia.tenants` only |
| `TIA_APP_PASSWORD`, `TIA_WORKER_PASSWORD` | passwords `tia.migrate` sets for the two roles |
| `TIA_ALLOW_INSECURE_DB` | accept a non-TLS database connection off-machine |
| `TIA_ALLOW_INSECURE_OUTBOUND` | accept plain http for outbound calls that carry credentials |
| `TIA_ALLOW_PRIVATE_ITSM_HOSTS` | let the ServiceNow integration reach a private address (laboratories only) |
| `TIA_GITLAB_URL`, `TIA_GITLAB_WEBHOOK_SECRET`, `TIA_GITLAB_TOKEN` | GitLab connector (section 17a) |
| `TIA_BITBUCKET_URL`, `TIA_BITBUCKET_WEBHOOK_SECRET`, `TIA_BITBUCKET_TOKEN` | Bitbucket connector (section 17a) |
| `TIA_SMTP_HOST`, `TIA_SMTP_PORT`, `TIA_SMTP_USER`, `TIA_SMTP_PASSWORD`, `TIA_SMTP_FROM` | e-mail notifications (section 17b) |
| `TIA_ALLOW_PRIVATE_NOTIFY_HOSTS` | let notification webhooks reach a private address (laboratories only) |
| `TIA_RATE_LIMIT`, `TIA_RATE_LIMIT_PER_MINUTE`, `TIA_RATE_LIMIT_TENANT_PER_MINUTE`, `TIA_RATE_LIMIT_ANON_PER_MINUTE`, `TIA_RATE_LIMIT_REPLICAS` | request rate limits (section 5) |
| `TIA_TRUST_PROXY` | take the client address from `X-Forwarded-For` (only behind your gateway) |
| `TIA_AIRGAPPED`, `TIA_AIRGAP_ALLOWED_HOSTS` | refuse every outbound connection except to the listed internal hosts (section 18c) |
| `TIA_REGION` | this deployment's hosting region (section 18a) |
| `TIA_BACKUP_KEY` (or `TIA_BACKUP_KEY_FILE`) | key for encrypted tenant backups (section 18a) |
| `TIA_AWS_ACCESS_KEY_ID`, `TIA_AWS_SECRET_ACCESS_KEY`, `TIA_AWS_SESSION_TOKEN`, `TIA_AWS_KMS_ENDPOINT` | AWS KMS for customer-managed keys |
| `TIA_AZURE_TENANT_ID`, `TIA_AZURE_CLIENT_ID`, `TIA_AZURE_CLIENT_SECRET`, `TIA_AZURE_LOGIN_URL`, `TIA_AZURE_KEYVAULT_TEST_URL` | Azure Key Vault for customer-managed keys |
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
| `TIA_ADO_HOOK_USERNAME`, `TIA_ADO_HOOK_PASSWORD`, `TIA_ADO_TOKEN`, `TIA_ADO_API_URL` | Azure DevOps service-hook credentials, personal access token, Azure DevOps Server base URL |
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
