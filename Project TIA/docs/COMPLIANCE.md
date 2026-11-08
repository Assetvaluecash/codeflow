# TIA control mapping: SOC 2 and ISO/IEC 27001:2022

Written for security, risk and procurement reviewers at customer banks, and for the TIA team preparing an audit (PRD PL-018).

**Status.** This is a readiness mapping: which product controls exist and where the evidence is. TIA has **not** been audited; no SOC 2 Type II
report or ISO 27001 certificate exists. Organisational controls (HR screening, vendor management, the ISMS itself, physical security of the
hosting provider) are the operating organisation's and are listed as such.

Legend: **Product**: implemented in TIA and covered by automated tests. **Ops**: an operating procedure, documented in [OPERATIONS.md](OPERATIONS.md)
or the [administrator guide](ADMIN-GUIDE.md), that the operator must run and evidence. **Org**: organisational, outside the product.

## 1. Access control

| SOC 2 | ISO 27001 Annex A | Control | Type | Evidence |
|---|---|---|---|---|
| CC6.1 | 5.15, 8.3 | Deny-by-default route permissions; five roles; per-repository access mirrored from the SCM | Product | `tia/rbac.py`, `tests/test_auth.py`, `tests/test_access.py` |
| CC6.1 | 5.17, 8.5 | Single sign-on (OIDC, SAML) with signed assertions; no local passwords; service tokens hashed, scoped, expiring | Product | `tests/test_auth.py`, `tests/test_saml.py` |
| CC6.2, CC6.3 | 5.18 | Role changes and disabling end sessions; tokens revocable; user count bounded by plan | Product | `tests/test_auth_api.py` |
| CC6.1 | 8.2 | Administration separated: the admin database role is used only by migrate/tenant/retention/backup commands | Product | `docs/ADMIN-GUIDE.md` section 1 |
| CC6.6 | 8.20, 8.22 | Tenant isolation by forced row-level security; region pinning; dedicated deployments | Product | `tests/test_storage.py`, `tests/test_saas.py` |

## 2. Cryptography and data protection

| SOC 2 | ISO 27001 | Control | Type | Evidence |
|---|---|---|---|---|
| CC6.1, C1.1 | 8.24 | Tenant secrets AES-256-GCM, per-tenant keys wrapped outside the database; customer-managed keys (AWS KMS, Azure Key Vault, Vault Transit) | Product | `tests/test_secretstore.py`, `tests/test_saas.py` |
| CC6.7 | 8.24 | TLS required to the database off-machine and for credential-carrying outbound calls; HSTS | Product | `tia/transport.py`, `tests/test_credentials.py` |
| C1.1 | 8.12 | Credentials found in source, logs, evidence or reports are withheld | Product | `tests/test_secretstore.py` |
| C1.2, P4.2 | 8.10 | Retention per tenant; offboarding deletes every tenant row and its keys (crypto-shredding of backups) | Product + Ops | `tia/retention.py`, `tests/test_release3.py`, `tests/test_tenants.py` |
| — | 5.34 | Data residency by region; air-gapped installation | Product | `tests/test_saas.py`, `tests/test_airgap.py` |

## 3. Logging, monitoring and integrity

| SOC 2 | ISO 27001 | Control | Type | Evidence |
|---|---|---|---|---|
| CC7.2 | 8.15 | Per-tenant hash-chained audit log, optionally keyed; every action names its actor; verification and period reports via API; retention keeps an anchor | Product | `tests/test_storage.py`, `tests/test_release3.py` |
| CC7.2 | 8.16 | Structured logs, Prometheus metrics, OpenTelemetry traces, readiness probes | Product + Ops | `tests/test_observability.py` |
| PI1.4 | 8.15 | Every analysis pinned to an evidence snapshot and reproducible | Product | `tests/test_snapshots.py` |
| CC7.3–CC7.5 | 5.24–5.28 | Incident response runbooks, breach notification | Org + Ops | to be written by the operating organisation |

## 4. Availability and recovery

| SOC 2 | ISO 27001 | Control | Type | Evidence |
|---|---|---|---|---|
| A1.1 | 8.6, 8.14 | Horizontal scaling, leased jobs, PodDisruptionBudget, plan limits | Product | `deploy/helm`, `tests/test_jobs.py` |
| A1.2 | 8.13 | PITR of the database; encrypted tenant export/restore/verify | Product + Ops | `tests/test_release3.py`, [OPERATIONS.md](OPERATIONS.md) |
| A1.3 | 5.30 | Restore drill quarterly; RPO ≤ 1 h / RTO ≤ 4 h targets | Ops | not yet measured |

## 5. Change and vulnerability management

| SOC 2 | ISO 27001 | Control | Type | Evidence |
|---|---|---|---|---|
| CC8.1 | 8.25, 8.32 | Every change through CI: tests, `pip-audit`, credential scan; schema migrations versioned | Product + Ops | `tools/ci.py`, `.github/workflows/tia-ci.yml` (repository root) |
| CC7.1 | 8.8 | Dependency vulnerability scanning (`pip-audit`); container image scanning | Product + Ops | image scanning is the operator's |
| CC4.1 | 8.29 | Threat model with tests per threat; independent penetration test before GA | Product + Org | [SECURITY.md](SECURITY.md); penetration test not yet done |

## 6. Gaps to close before an audit

* Penetration test by an independent firm (PRD PL-018) — not done.
* Incident response and breach-notification runbooks — organisational, not written.
* A measured restore drill (RPO/RTO) — not done.
* WAF and a global rate limit at the gateway: written as Terraform (`deploy/terraform/azure`), not yet deployed; TIA's own limiter is per replica (SECURITY.md T17).
* GitHub Actions pinned by commit SHA (SECURITY.md T19).
* An operating period of evidence collection (SOC 2 Type II needs 3–12 months of operation).
