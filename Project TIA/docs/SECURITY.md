# TIA security review and threat model

*Status: first review, written alongside the implementation (UC1-27). It is a self-review plus one independent read-only code review by a separate reviewer (an AI code-review agent
that had no part in writing the code); it is **not** a penetration test, not an external audit, and not a certification. Findings and their disposition are in section 5.*

## 1. What is protected

| Asset | Why it matters |
|---|---|
| Customers' source code, call graphs, evidence and reports | A bank's architecture and its weak points; the product's whole value |
| Tenant isolation | One customer must never read another's data, or act on its behalf |
| Credentials: identity-provider client secrets, GitHub tokens and App private key, session cookies, service tokens, the audit key, the master key | Compromise would let an attacker impersonate people or read code |
| The audit log | Evidence of who did what; must be tamper-evident |
| Availability of analysis | Pull-request advice that arrives late or never is ignored |

## 2. Actors and trust boundaries

```
Browser / CI script ──(1)──> API server ──(3)──> PostgreSQL (row-level security)
GitHub ──(2) webhook──────────┘      │  └────(4)──> identity providers (OIDC discovery, JWKS, token endpoint)
                                     │  └────(5)──> GitHub API (installation tokens, comments, check runs)
Queue (PostgreSQL) ──> Worker ──(6)──> cloned repositories (untrusted code, parsed only)
```

1. **Callers are untrusted** until a credential is verified. The credential names the tenant. 2. **Webhooks** are authenticated by an HMAC signature only. 3. **The database is trusted
to enforce tenant isolation** (forced row-level security; the runtime roles are not owners and cannot bypass it) and every query also names the tenant explicitly. 4. **Identity-provider
endpoints are controlled by tenant administrators** and are therefore treated as hostile network targets. 6. **Repository content is hostile input**: it is only read and parsed, never built or executed.

## 3. Threats and what stops them

| # | Threat | Mitigation (and where it is tested) |
|---|---|---|
| T1 | A caller reads or writes another tenant's data | Tenant comes only from the verified credential (never from a header, host or body); per-tenant service objects and workspaces; forced row-level security on every tenant table; explicit `tenant_id` in every query; cross-tenant routes answer 404. `tests/test_storage.py`, `tests/test_auth_api.py::TestTenantRouting`, `tests/test_tenants.py` (catalog check that every tenant table is registered) |
| T2 | Authentication bypass | Deny-by-default route table: a route that is not classified is refused; a test fails if a route is added without a permission. Sessions/tokens are 256-bit random, only SHA-256 stored. `tests/test_auth.py::TestRoutes` |
| T3 | Privilege escalation between roles | Permission per route; roles additive; role changes and disabling end sessions; `default_role` can never be administrator; service tokens cannot be administrators; the audit and admin routes need their own permission. `tests/test_auth_api.py::TestRoles` (every role against every sensitive route) |
| T4 | Reading a repository one may not read | Per-repository access (SCM mirror / grants / tenant); unreadable repositories answer 404 everywhere; reports redacted per reader; PR comments name another repository only when every reader of the PR provably may read it; the mirror fails closed. `tests/test_access.py` |
| T5 | Forged or replayed identity: JWT | Asymmetric algorithms only, key chosen by `kid` from the provider's key set (never from the token), `none`/HS* refused before any key lookup, embedded `jwk`/`jku`/`x5u` ignored, issuer/audience/expiry/not-before/nonce checked, 2048-bit RSA minimum. `tests/test_auth.py::TestJwt` |
| T6 | Forged or replayed identity: SAML | Assertion must be signed by a configured certificate (SHA-1 refused), exactly one assertion, encrypted assertions and any DOCTYPE refused, response must answer our own request (InResponseTo, bound to the browser that started it), destination/issuer/audience/recipient/validity checked, assertion id accepted once. `tests/test_saml.py` (signature wrapping x2, XXE, entity bomb, replay, unsolicited, wrong key, tampering) |
| T7 | CSRF and login CSRF | `SameSite=Lax` `HttpOnly` cookies; state-changing calls need an `X-TIA-CSRF` value derived from the session and a matching `Origin`; sign-in state is single use and bound to the starting browser by a cookie. `tests/test_auth_api.py::TestBrowserSignIn` |
| T8 | SSRF through identity-provider URLs | `netsafe`: https only, every resolved address must be public (loopback, private, link-local and metadata ranges refused), the connection goes to the address that was checked, no redirects, size and time caps. `tests/test_auth.py::TestNetsafe` |
| T9 | Open redirect after sign-in | `return_to` must be a local path |
| T10 | Claiming someone else's GitHub App installation | Linking needs GitHub's own user authorisation proving the caller can see the installation; the claim function never takes an installation owned by another tenant and never reveals its owner. `tests/test_githubapp.py::TestLinking` |
| T11 | Using another tenant's installation token | A token is issued only for an installation mapped to the calling tenant; a repository of another tenant's installation is refused. `tests/test_githubapp.py::TestTokenSource` |
| T12 | Secrets leaking into logs, audit, job payloads, reports | Credential-shaped values are withheld everywhere they could be recorded (graph, reports, logs, audit, job errors); installation tokens, client secrets and private keys never enter the database or logs (checked by tests and in the live run). `tests/test_secretstore.py`, `tests/test_githubapp.py`, `tests/test_observability.py` |
| T13 | Secret theft from the database | Tenant secrets are AES-256-GCM encrypted under per-tenant keys wrapped by a key held outside the database (or in Vault); the tenant, name and key version are bound into each ciphertext. `tests/test_secretstore.py` |
| T14 | Tampering with the audit log | Hash-chained, append-only (runtime role cannot update or delete), optionally keyed with a secret held outside the database; every authenticated action records its actor. `tests/test_storage.py` |
| T15 | Malicious repository content | Parsed only: no build, no execution; archive extraction rejects traversal, absolute and device paths, symlinks, encrypted and colliding names, with byte/file caps. `tests/test_upload.py` |
| T16 | SQL injection | All queries are parameterised; the two dynamic identifiers (table names for tenant offboarding and quota) come from the catalog / a fixed list |
| T17 | Resource exhaustion, credential guessing | Token buckets per principal, per tenant and per client address; failed authentications are charged to the address; analyses and evidence cost more; 429 with Retry-After; pending sign-ins capped; request bodies capped. Limits are per replica: a gateway limit and WAF are still recommended (`deploy/terraform/azure`). `tests/test_ratelimit.py` |
| T18 | A compromised or buggy worker reading across tenants | The worker role cannot bypass row-level security; each job runs bound to its own tenant; the few cross-tenant lookups are narrow SECURITY DEFINER functions |
| T20 | Malicious evidence uploads (CI XML, coverage, OTLP JSON, deployments) | Bodies capped at 25 MB; XML with a DOCTYPE or entities refused (no XXE, no entity bombs); JSON parsed with the standard library; counts capped (200,000 tests); records scrubbed for credential-shaped values; the repository must be registered and readable; `evidence_write` permission. `tests/test_evidence.py` |
| T21 | Evidence injected to make a policy pass | A rule never passes on missing data (`not_evaluated`); a test result counts only for the analysed revision; every record names who ingested it, is audited, and is pinned by id in the analysis snapshot, so a later record cannot change a stored report. `tests/test_policy.py`, `tests/test_evidence.py::test_a_reproduction_reads_the_pinned_evidence` |
| T22 | ServiceNow integration used to change records | Change records: only lookup, attachment listing and one attachment; no update/approve/transition code path. Team coordination (FR-045) can create one task per analysis and assignment group, only when a `servicenow_task` channel is configured, and never edits it; the record number is validated; outbound through `netsafe` (https, public addresses, no redirects) to the configured instance only; credentials in the tenant secret store. `tests/test_integrations.py` (a request log proves the only write) |
| T23 | An assistant writing through MCP, or reading what its user may not | The MCP server has no write tool (calls to any other tool are refused); every tool goes through the same visibility and redaction as the REST API; `read` permission. `tests/test_integrations.py` |
| T24 | Customer key misuse (BYOK) | The data key is wrapped by the customer's KMS with the tenant binding as encryption context (AWS) or an AAD seal (Azure), so a wrapped key moved to another tenant does not open; a disabled key fails closed; switching keys re-wraps in one transaction. `tests/test_saas.py` (against KMS stand-ins only) |
| T25 | A tenant's data served or written outside its region | A pinned tenant is refused (HTTP 421) before any of its data is read or written, by the API and by the job claim; each region's deployment holds only its own database credentials. `tests/test_saas.py` |
| T26 | Enforcing mode used to block merges unexpectedly | Enforcement must be configured explicitly (`policies.mode`); only a failed MANDATORY rule fails the check; TIA never changes branch protection. `tests/test_policy.py` |
| T27 | Data leaving an air-gapped installation | With `TIA_AIRGAPPED` every outbound path (GitHub, Azure DevOps, git clone, identity-provider discovery, ServiceNow, Vault, KMS, trace export) passes one guard that refuses hosts outside `TIA_AIRGAP_ALLOWED_HOSTS` before connecting, and fails loudly. `tests/test_airgap.py` (a network stub proves no connection is attempted) |
| T28 | Retention used to hide audit tampering | Only the admin role can delete audit entries or write the anchor (the runtime role has select on `audit_anchors` only); the anchor keeps the last removed hash, so an edit to any remaining entry is still detected; the purge is itself audited. `tests/test_release3.py` |
| T29 | A stolen or altered tenant backup | AES-256-GCM under `TIA_BACKUP_KEY` with the tenant id as associated data; any altered byte or wrong key fails; secrets inside stay wrapped by the key-encryption key, which is not in the backup; a restore never merges into an existing tenant. `tests/test_release3.py` |
| T30 | A malicious or broken third-party language adapter | Adapters are installed by the operator only (Python entry points, no upload path); they cannot replace a built-in language; an adapter exception is contained to its file and reported. Adapter code runs in the TIA process: install only reviewed packages. `tests/test_adapters.py` |
| T31 | Incident links used to blame a change | Every link names who made it and when, is audited, and is labelled `manually_validated`; TIA never ranks or labels a candidate as the cause. `tests/test_release3.py` |
| T32 | Forged GitLab / Bitbucket events | GitLab: the secret token compared in constant time; Bitbucket: HMAC-SHA256 of the body; refused calls are not audited (no log flooding); an event only queues an analysis of a registered repository. `tests/test_scmhosts.py` |
| T33 | GitLab / Bitbucket token sent elsewhere | Calls carrying the token go only to the configured host over https (http only to this machine); a token never enters the job queue. `tests/test_scmhosts.py::test_tokens_go_only_to_the_configured_host` |
| T34 | Notifications leaking reports or reaching internal hosts | A team receives only the findings about the components it owns; webhook URLs are tenant secrets; webhooks go through `netsafe` (public addresses, no redirects) and the air-gap guard; e-mail requires STARTTLS off this machine. `tests/test_release4.py` |
| T35 | Verdicts or trends used to rewrite history | Verdicts copy the finding's rules and never touch the report; rule changes apply to new analyses only (the stored report stays byte-identical); trends are read-only aggregates of what the caller may read. `tests/test_release4.py` |
| T19 | Supply chain | Few dependencies; CI runs `pip-audit` and a credential scan (`tools/ci.py`); actions are pinned by tag, not by commit SHA (open item) |

## 4. Not covered or accepted

* **No account lockout and no global rate limit** across replicas (each API replica limits on its own; use the gateway's limit too). Tokens and session secrets are 256-bit random values, so guessing is not feasible.
* **Development mode** (`TIA_AUTH_REQUIRED` unset) makes everyone an administrator; the server refuses to listen on a network address in that mode unless explicitly told to.
* **No external penetration test**; no formal threat-modelling workshop with the customer's security team; no review of customer-specific identity-provider quirks.
* **Real GitHub App, identity providers, Vault and OpenTelemetry collectors have not been used**; their behaviour is simulated.
* Group changes at the identity provider apply at the next sign-in; an administrator's edits reach bearer-token users within 30 seconds (cache).
* The repository-access mirror depends on what GitHub reports; a person removed on GitHub keeps access until the next sync (bounded by `TIA_ACCESS_MAX_AGE_S`).
* A tenant administrator can change repository access modes and grants (audited), which is how they grant themselves access to repositories they cannot otherwise read.

## 5. Review findings and disposition

The independent reviewer read the authentication, tenant-routing, access-control, GitHub App and SQL code and found **no authentication bypass, no cross-tenant read through
row-level security or SQL injection, no JWT algorithm-confusion gap, no SAML signature-wrapping or CSRF break**. It did find the following; each is fixed and has a regression
test in `tests/test_security_review.py` unless stated.

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| H1 | high | A tenant administrator could point an identity provider's `client_secret_ref` at any secret in the tenant's store (e.g. `github/token`) and have it POSTed to a token endpoint they control | **Fixed**: a provider may reference only `idp/<its name>/client-secret`. Also fixed in passing: `token_endpoint` and similar valid settings were wrongly rejected as "secret-like" |
| H2 | high | Any GitHub user who could *see* an installation (e.g. an outside collaborator on one repository) could link it to their own tenant | **Fixed**: after the visibility check, the user must own the account (organisation owner, or the user themself) |
| M1 | medium | An anonymous caller could write `login_failed` rows into any tenant's audit chain, create workspace directories and grow an in-memory cache by sending forged sign-in state | **Fixed**: failures are audited only after the single-use state proved the tenant; the identity-store cache is bounded |
| M2 | medium | A slow identity provider could hold request threads (per-read timeout only; failed discovery retried on every request) | **Fixed**: 10-second wall-clock deadline per outbound request (the socket is shut down, not just closed); failed discovery is remembered for 30 s |
| M3 | medium | Unauthenticated bodies (GitHub webhook, SAML ACS) were read into memory without a limit | **Fixed**: hard caps (25 MB webhook, 1.5 MB ACS) enforced while streaming |
| M4 | medium | A user with repository-write could use `discover`'s `api_base` or an arbitrary clone URL to probe the internal network or `file:` URLs | **Fixed**: `api_base` must be http(s); repository hosts must resolve only to public addresses unless local access is enabled (development) |
| M5 | medium | Metadata for repositories the caller cannot read: graph versions, rescan results, registration/discovery responses | **Fixed**: filtered by what the caller may read; the registrar of a hosted repository they cannot read gets only its name. Residual: the `total_nodes`/`total_edges` counts in graph responses still count unreadable repositories (counts only) |
| M6 | medium | Access rows survived repository deletion, so a re-registered name could inherit an old, more permissive mode | **Fixed**: deleting a repository deletes its access rows |
| M7 | medium | First-come squatting of a public (issuer, audience) pair by a malicious tenant administrator | **Mitigated, not eliminated**: the uniqueness error no longer says which pairs exist. A real fix needs proof of control of the client id or a tenant-specific audience; operator-provisioned providers (CLI) should be created first for any shared public provider. Open item |
| M8 | medium | Azure DevOps / plain-git repositories defaulted to tenant-wide readability | **Fixed**: only uploads and local paths default to tenant-wide; GitHub defaults to the host's list; every other hosted repository is `restricted` until an administrator grants it |
| L1 | low | Messages distinguished "App not installed" from "installed for another customer", revealing other customers' repositories | **Fixed**: one message |
| L2 | low | A tenant administrator controls `sso_url` and the discovery authorization endpoint, so `/auth/login` redirects to a URL of their choosing (trusted-domain redirector) | **Accepted**: only an administrator of that tenant can set it, and only users of that tenant's organisation name reach it; mitigate with a reverse-proxy allow-list if required |
| L3 | low | Just-in-time user rows were created before the role check (unbounded rows from a public identity provider) | **Fixed**: no row for someone who would get no role |
| L4 | low | The 30-second user/provider caches are per process: disabling a user can take up to 30 s with several processes | **Accepted**, documented |

**Not yet reviewed** (the reviewer's own list of areas it ran out of time for, which I have not covered either): archive/upload extraction and `commit_tree`, the worker and job handlers
(including the PR-comment audience rule when it runs without a request), git command/argument handling for tenant-supplied URLs and refs (`repos._auth_env`, ssh transport, option injection),
tenant offboarding and the secret-store cryptography, and stored XSS through repository content in the report page and comment renderer. The upload and report-page code has its own
tests (`tests/test_upload.py`, `tests/test_reportpage.py`) but has had no independent review.

**Conclusion.** No critical or high finding is open. Medium finding M7 is open (mitigated). The "no open critical/high" gate of PRD section 19 is met **for the code reviewed**; it is not a
claim about the areas listed above, and it is not a substitute for an external penetration test.
