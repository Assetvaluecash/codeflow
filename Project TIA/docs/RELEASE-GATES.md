# Release quality gates: assessment

Written for the people who sign off a TIA release (product owner, engineering lead, security). PRD section 19 defines six gates; this page
records the evidence for each, per release, as of 2026-11-08. It is an assessment, not the sign-off: the sign-off lines at the end are for you.

Status words: **Met** = the criterion holds on evidence produced here. **Met (simulated)** = holds against stand-ins for external systems
(GitHub, GitLab, Bitbucket, ServiceNow, cloud KMS, mail/chat), not the real ones. **Not met** = something is missing; the reason says what.

## Functional — all acceptance criteria of the release pass in automated tests

| Release | Status | Evidence |
|---|---|---|
| R1 | Met (simulated) | AC-001…026, 035, 039, 040 automated; AC-020 (PR comment) was verified live on github.com, but its check run only against a stand-in |
| R2 | Met (simulated) | AC-007, 008, 013, 014, 027…032, 041: `test_evidence`, `test_contracts_events`, `test_policy`, `test_integrations`, `test_saas` |
| R3 | Met | AC-033, 034: `test_release3` |
| R4 | Met (simulated) | AC-036 (`test_scmhosts`), AC-037 (`test_sql_lineage`), AC-038 (`test_release4`) |
| PRD v3.2 tester outputs | Met | AC-042 (`test_testscope`), AC-043 and AC-044 (`test_tester_views`), plus real-Chrome checks of the tester views |

Whole suite on 2026-11-06: 1168 passed, 0 failed, on real PostgreSQL 16 and real git (summary.md).

## Accuracy — section 15.3 targets on the reference landscape

| Release | Status | Evidence / gap |
|---|---|---|
| R1 | Met | `tools/accuracy.py` on two independent real-code Java landscapes and a synthetic one: all measured targets met (PERFORMANCE.md, tracker) |
| R2–R4 | **Not met** | Events, SQL lineage and the Go/C#/Kotlin/TypeScript adapters are tested on samples written for the tests. They have not been measured against a labelled reference landscape of real code. Needed: a customer or open-source landscape with a hand-labelled ground truth. |

## Performance — section 12 targets on the reference landscape

| Release | Status | Evidence / gap |
|---|---|---|
| R1 | Met at the size measured | 500 generated repositories (0.6 M lines): PR P95 5.3 s, report P95 0.07 s, 50-PR burst comment P95 163 s (targets 120 s / 5 s / 180 s) |
| R2–R4 | Met at the size measured | `benchmark.py --enterprise` on the same 500 repositories, with 3,000 evidence records, an enforcing policy, owners and automatic notification: PR P95 8.9 s, report P95 0.19 s, 50-PR burst comment P95 147.5 s (PERFORMANCE.md). **Not covered:** SQL/event lineage and the new languages in the generated landscape; 20 M lines of code. |

## Security — review passed, no open critical/high vulnerabilities, threat model updated

| Release | Status | Evidence / gap |
|---|---|---|
| R1–R4 | **Partly met** | Threat model T1–T35 with tests per threat (SECURITY.md); `pip-audit`: no known vulnerabilities (2026-11-06, after raising setuptools to ≥ 84.1); in-app rate limiting added (T17). **Missing:** an independent security review and penetration test; GitHub Actions not pinned by commit SHA (T19). |

## Operability — install, upgrade, backup/restore and observability verified on the supported topologies

| Release | Status | Evidence / gap |
|---|---|---|
| R1–R4 | **Partly met** | **Single-machine topology (Compose), verified 2026-11-06/08:**<br>- image built;<br>- 17 migrations;<br>- tenant created;<br>- API and worker ready;<br>- an authenticated upload-and-analyse run (6 repositories, 5 findings);<br>- audit chain verified;<br>- upgrade re-run on the live system;<br>- encrypted backup restored into a second database and verified (26 tables).<br>**Kubernetes (Docker Desktop, one node), verified 2026-11-08:**<br>- chart installed (migration hook, 2 API + 2 worker replicas);<br>- `helm upgrade` rolled the pods;<br>- end-to-end analysis through the service; audit chain verified;<br>- killed pods replaced without failed health checks.<br>**Not verified:** multi-node cluster and node failure, ReadWriteMany workspace, the Azure Terraform, a timed restore drill (RPO/RTO). |

## Documentation — administrator, user and API documentation complete for the scope

| Release | Status | Evidence |
|---|---|---|
| R1–R4 | Met | ADMIN-GUIDE, USER-GUIDE, generated API.md and openapi.json (kept current by `test_docs` / `test_sdk`), OPERATIONS, COMPLIANCE, SECURITY, ADAPTER-SDK, GRAPH-STORE |

## What closes the open gates

1. **Accuracy (R2–R4):** a labelled reference landscape that exercises events, SQL and the new languages; run `tools/accuracy.py` on it.
2. **Performance:** extend the generator to SQL/event lineage, the new languages and more lines of code (20 M).
3. **Security:** an independent review and penetration test; pin the CI actions by SHA.
4. **Operability:** apply the Terraform in a subscription and run the OPERATIONS.md drills on a multi-node cluster there (node failover, timed restore).
5. **External systems (all "simulated" rows):** trials with real GitHub App, GitLab, Bitbucket, ServiceNow, KMS and SMTP credentials.

## Sign-off

| Release | Product owner | Engineering lead | Security | Date |
|---|---|---|---|---|
| R1 | | | | |
| R2 | | | | |
| R3 | | | | |
| R4 / GA | | | | |
