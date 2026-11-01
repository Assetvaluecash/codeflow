# TIA HTTP API reference

*Generated from the application's route table and OpenAPI schema by `tools/gen_docs.py`; do not edit by hand (a test fails when it is out of date).*

All paths are relative to the server's address. Request and response bodies are JSON unless stated.

## Authentication

With `TIA_AUTH_REQUIRED=true` every route except the ones marked **public** needs one of:

| Credential | How it is sent | Who it is for |
|---|---|---|
| Service-account token | `Authorization: Bearer tia_<tenant>_<id>_<secret>` | CI jobs, scripts. Has roles and an optional repository scope; created by an administrator; shown once. |
| OpenID Connect token | `Authorization: Bearer <JWT>` from the tenant's identity provider | Programs acting for a signed-in person. Issuer and audience select the tenant. |
| Browser session | `tia_session` cookie set by `/auth/login` (OIDC or SAML) | The web interface. State-changing calls must also send the `X-TIA-CSRF` value from `GET /auth/me`. |

The credential names the **tenant**: every request runs against that tenant's data only. Without `TIA_AUTH_REQUIRED` (development) everyone is an administrator on one tenant.

Roles: `viewer`, `engineer`, `release_manager`, `auditor`, `administrator` (additive). Each route below lists the permission it needs and the roles that hold it.
A repository the caller may not read answers `404`, as if it did not exist.

Common answers: `401` no/invalid credential (`code`: `authentication_required`, `invalid_token`, `no_session`, `session_expired`, `session_revoked`), `403` not permitted
(`forbidden`, `csrf`, `user_disabled`, `no_role`, `tenant_suspended`, `storage_quota_exceeded`), `404` unknown or not readable, `410` tenant offboarded, `422` invalid body
(configuration endpoints list every problem), `424` a GitHub App installation problem, `429` too many pending sign-ins, `503` a required service is not configured.
Every response carries `X-Request-ID` and `X-Trace-Id`.


## Administration (/api/v1/admin)

### `GET /api/v1/admin/github/installations`

**Needs:** `admin` (`administrator`).  

### `POST /api/v1/admin/github/installations`

**Needs:** `admin` (`administrator`).  Link a GitHub App installation to this organisation. The caller must prove, through GitHub's own user authorisation, that they can see the installation: knowing its id is not enough.

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `installation_id` | integer | yes |  |
| `code` | string | yes |  |

### `DELETE /api/v1/admin/github/installations/{iid}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `iid` | path | string | yes |

### `GET /api/v1/admin/idps`

**Needs:** `admin` (`administrator`).  

### `POST /api/v1/admin/idps`

**Needs:** `admin` (`administrator`).  

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes |  |
| `protocol` | string | no |  |
| `config` | object | yes |  |
| `enabled` | boolean | no |  |
| `client_secret_ref` | string (optional) | no |  |
| `client_secret` | string (optional) | no |  |

### `DELETE /api/v1/admin/idps/{iid}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `iid` | path | string | yes |

### `PUT /api/v1/admin/idps/{iid}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `iid` | path | string | yes |

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes |  |
| `protocol` | string | no |  |
| `config` | object | yes |  |
| `enabled` | boolean | no |  |
| `client_secret_ref` | string (optional) | no |  |
| `client_secret` | string (optional) | no |  |

### `GET /api/v1/admin/repo-access`

**Needs:** `admin` (`administrator`).  How every repository is governed, who is granted, and how fresh the mirror of the source-code host's permissions is.

### `POST /api/v1/admin/repo-access/sync`

**Needs:** `access_sync` (`release_manager`, `administrator`).  Refresh the mirror of who the source-code host says can read each hosted repository. Needs a token with push access to them.

### `PUT /api/v1/admin/repo-access/{name}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `name` | path | string | yes |

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `mode` | string (optional) | no |  |
| `grants` | array (optional) | no |  |

### `GET /api/v1/admin/sessions`

**Needs:** `admin` (`administrator`).  

### `DELETE /api/v1/admin/sessions/{sid}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `sid` | path | string | yes |

### `GET /api/v1/admin/tokens`

**Needs:** `admin` (`administrator`).  

### `POST /api/v1/admin/tokens`

**Needs:** `admin` (`administrator`).  

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes |  |
| `roles` | array of string | yes |  |
| `repos` | any | no |  |
| `expires_in_days` | integer (optional) | no |  |

### `DELETE /api/v1/admin/tokens/{tid}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `tid` | path | string | yes |

### `GET /api/v1/admin/users`

**Needs:** `admin` (`administrator`).  

### `PUT /api/v1/admin/users/{uid}`

**Needs:** `admin` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `uid` | path | string | yes |

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `roles` | array (optional) | no |  |
| `disabled` | boolean (optional) | no |  |
| `scm_logins` | object (optional) | no |  |


## Audit

### `GET /api/v1/audit`

**Needs:** `audit_read` (`auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `limit` | query | integer | no |

### `GET /api/v1/audit/verify`

**Needs:** `audit_read` (`auditor`, `administrator`).  


## Code graph

### `GET /api/v1/graph`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `level` | query | string | no |
| `repos` | query | string|null | no |
| `q` | query | string|null | no |
| `focus` | query | string|null | no |
| `depth` | query | integer | no |
| `limit` | query | integer | no |

### `GET /api/v1/graph/relationships`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  Cross-repository relationships with their state: current, or stale with the reason (AC-011).

| Parameter | In | Type | Required |
|---|---|---|---|
| `state` | query | string|null | no |

### `POST /api/v1/graph/rescan`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  Full rescan of every repository (ignores caches), with drift detection against the incremental result.

### `GET /api/v1/graph/stats`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

### `GET /api/v1/graph/versions`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `limit` | query | integer | no |

### `GET /api/v1/graph/versions/{gid}`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `gid` | path | string | yes |


## Evidence snapshots

### `GET /api/v1/evidence-snapshots/{sid}`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `sid` | path | string | yes |


## Health and metrics

### `GET /api/v1/health`

**Needs:** public.  

### `GET /healthz`

**Needs:** public.  Liveness: the process is serving. No dependencies.

### `GET /metrics`

**Needs:** public.  

### `GET /readyz`

**Needs:** public.  Readiness of the platform (database, schema, workspace, queue consumers); never about analysis completeness or tenant status.


## Impact analyses

### `POST /api/v1/impact-analyses`

**Needs:** `analyze` (`engineer`, `release_manager`, `administrator`).  Synchronous by default. With ?async=true the analysis is queued and a job is returned (HTTP 202); queued analyses never accept tokens (nothing secret is stored in the queue).

| Parameter | In | Type | Required |
|---|---|---|---|
| `async` | query | boolean | no |

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `target` | string | yes |  |
| `head` | string (optional) | no |  |
| `pr` | integer (optional) | no |  |
| `base` | string (optional) | no |  |
| `token` | string (optional) | no |  |
| `repos` | array (optional) | no |  |
| `revisions` | object | no |  |
| `change_record` | string (optional) | no |  |
| `max_depth` | integer (optional) | no |  |
| `max_results` | integer (optional) | no |  |

### `GET /api/v1/impact-analyses/{aid}`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

### `GET /api/v1/impact-analyses/{aid}/comment`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

### `GET /api/v1/impact-analyses/{aid}/evidence`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  AC-015: the evidence snapshot and the exact evidence path behind every material finding.

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

### `GET /api/v1/impact-analyses/{aid}/graph`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

### `GET /api/v1/impact-analyses/{aid}/paths`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

### `POST /api/v1/impact-analyses/{aid}/publish`

**Needs:** `analyze` (`engineer`, `release_manager`, `administrator`).  Post (or update in place) the advisory comment and check run for a stored analysis on a GitHub pull request.

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `repository` | string | yes |  |
| `pr` | integer | yes |  |
| `head_sha` | string (optional) | no |  |
| `comment` | boolean | no |  |
| `check_run` | boolean | no |  |

### `POST /api/v1/impact-analyses/{aid}/reproduce`

**Needs:** `analyze` (`engineer`, `release_manager`, `administrator`).  Re-run the analysis on its pinned snapshot; the result must be identical (AC-015).

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |

### `GET /api/v1/impact-analyses/{aid}/unknowns`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `aid` | path | string | yes |


## Jobs

### `DELETE /api/v1/jobs/{job_id}`

**Needs:** `analyze` (`engineer`, `release_manager`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `job_id` | path | string | yes |

### `GET /api/v1/jobs/{job_id}`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `job_id` | path | string | yes |


## Other

### `POST /query/tia`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  A finished Change Impact Report in Knowledge Fabric's QueryResponse shape. The analysis is named (analysis_id, or repository plus pull_request/revision for the newest such report); the question text is never parsed and no analysis is started here.

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `intent` | string | no |  |
| `query` | string | no |  |
| `domain` | string | no |  |
| `top_k_semantic` | integer | no |  |
| `top_k_graph` | integer | no |  |
| `analysis_id` | string (optional) | no |  |
| `repository` | string (optional) | no |  |
| `pull_request` | integer (optional) | no |  |
| `revision` | string (optional) | no |  |


## Repositories

### `GET /api/v1/repositories`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

### `POST /api/v1/repositories`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | string (optional) | no |  |
| `path` | string (optional) | no |  |
| `name` | string (optional) | no |  |
| `token` | string (optional) | no |  |
| `branch` | string (optional) | no |  |

### `POST /api/v1/repositories/discover`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `provider` | string | yes |  |
| `org` | string (optional) | no |  |
| `project` | string (optional) | no |  |
| `token` | string (optional) | no |  |
| `api_base` | string (optional) | no |  |
| `ingest` | boolean | no |  |
| `names` | array (optional) | no |  |

### `DELETE /api/v1/repositories/{name}`

**Needs:** `repo_delete` (`administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `name` | path | string | yes |

### `GET /api/v1/repositories/{name}/revisions`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `name` | path | string | yes |
| `limit` | query | integer | no |

### `POST /api/v1/repositories/{name}/sync`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `name` | path | string | yes |

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `token` | string (optional) | no |  |

### `PUT /api/v1/repositories/{name}/upload`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  One call for scripts: PUT the zip as the body; creates (or adds a revision to) the repository `name`.

| Parameter | In | Type | Required |
|---|---|---|---|
| `name` | path | string | yes |
| `strip_root` | query | string | no |


## Sign-in and session (/auth)

### `GET /auth/callback`

**Needs:** public.  

### `GET /auth/login`

**Needs:** public.  

### `POST /auth/logout`

**Needs:** any signed-in principal.  

### `GET /auth/me`

**Needs:** public.  Who am I? Never 401: the page asks this first to decide between the application and the sign-in screen.

### `GET /auth/options`

**Needs:** public.  The sign-in methods of an organisation. Unknown, inactive and offboarded organisations look the same: an empty list.

### `POST /auth/saml/acs`

**Needs:** public.  The identity provider's POST (via the user's browser): the signed assertion that proves who signed in.

### `GET /auth/saml/metadata`

**Needs:** public.  


## Tenant and configuration

### `GET /api/v1/config`

**Needs:** public.  

### `GET /api/v1/tenant`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  This tenant's own lifecycle status and storage use against its quota (approximate: row sizes plus workspace bytes).

### `GET /api/v1/tenant/config`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  The configuration in force (defaults filled in), the built-in defaults, and who set it.

### `PUT /api/v1/tenant/config`

**Needs:** `config_write` (`release_manager`, `administrator`).  Save a new version (a no-op when identical to the current one). Invalid configurations are refused with every problem listed.

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `config` | object | yes |  |
| `comment` | string (optional) | no |  |

### `GET /api/v1/tenant/config/diff`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  Setting-by-setting differences between two versions (0 = the built-in defaults).

| Parameter | In | Type | Required |
|---|---|---|---|
| `from_version` | query | integer | yes |
| `to_version` | query | integer | yes |

### `POST /api/v1/tenant/config/validate`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  Dry run: every problem with a configuration, or what it would change. Saves nothing.

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `config` | object | yes |  |
| `comment` | string (optional) | no |  |

### `GET /api/v1/tenant/config/versions`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `limit` | query | integer | no |

### `GET /api/v1/tenant/config/versions/{n}`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `n` | path | integer | yes |

### `POST /api/v1/tenant/config/versions/{n}/restore`

**Needs:** `config_write` (`release_manager`, `administrator`).  Roll back by saving an old version's settings as a NEW version: history is never rewritten.

| Parameter | In | Type | Required |
|---|---|---|---|
| `n` | path | integer | yes |


## Uploads

### `POST /api/v1/uploads`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  

Request body:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes |  |

### `DELETE /api/v1/uploads/{uid}`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `uid` | path | string | yes |

### `PUT /api/v1/uploads/{uid}/archive`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  The whole zip as the raw request body (streamed to disk, never held in memory).

| Parameter | In | Type | Required |
|---|---|---|---|
| `uid` | path | string | yes |
| `strip_root` | query | string | no |

### `POST /api/v1/uploads/{uid}/commit`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  

| Parameter | In | Type | Required |
|---|---|---|---|
| `uid` | path | string | yes |

### `POST /api/v1/uploads/{uid}/files`

**Needs:** `repo_write` (`engineer`, `release_manager`, `administrator`).  A batch of folder files: parts named 'files' (and, in the same order, 'paths' with each relative path).

| Parameter | In | Type | Required |
|---|---|---|---|
| `uid` | path | string | yes |


## Webhooks

### `POST /api/v1/webhooks/ado`

**Needs:** public.  Azure DevOps service hooks (pull request created / updated). Answers at once; the analysis is queued.

### `POST /api/v1/webhooks/github`

**Needs:** public.  GitHub pull_request events. Returns quickly: verification and matching happen here, the work is queued.


## Who am I

### `GET /api/v1/me`

**Needs:** any signed-in principal.  


## Web interface

### `GET /`

**Needs:** public.  

### `GET /analyses/{aid}`

**Needs:** `read` (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`).  The shareable report page (UC1-16) that the PR comment and check run link to. Same access rule as the JSON API.
