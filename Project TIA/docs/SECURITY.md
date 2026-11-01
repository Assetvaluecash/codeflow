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
| T17 | Unauthenticated resource exhaustion | Pending sign-ins capped per tenant; request bodies are capped on the unauthenticated webhook; everything else is behind a credential. Rate limiting is **not** implemented (reverse proxy) |
| T18 | A compromised or buggy worker reading across tenants | The worker role cannot bypass row-level security; each job runs bound to its own tenant; the few cross-tenant lookups are narrow SECURITY DEFINER functions |
| T19 | Supply chain | Few dependencies; CI runs `pip-audit` and a credential scan (`tools/ci.py`); actions are pinned by tag, not by commit SHA (open item) |

## 4. Not covered or accepted

* **No rate limiting or account lockout** (use a reverse proxy). Tokens and session secrets are 256-bit random values, so guessing is not feasible, but request floods are not stopped here.
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
