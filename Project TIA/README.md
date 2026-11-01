# TIA - Technical Impact Analysis (Phase 1 build)

Cross-repository change-impact analysis for a pull request, producing an evidence-backed
**Change Impact Report** (JSON + PR-comment markdown). Built from
`../revised/PRD.md` v2.1, `ARCHITECTURE.md` v1.1, `TECHNICAL-FRAMEWORK.md` v1.1.

## Quick start

```bash
pip install pyyaml pytest fastapi httpx      # or: pip install -e ".[api,dev]"
python -m pytest -q                           # 29 tests, builds a 6-repo git landscape on the fly
python tools/demo.py                            # breaking PaymentStatus change -> PR comment
python tools/demo.py --branch impl --format json
python -m tia.cli analyze --repo payments-service=./payments --repo recon=./recon \
    --target payments-service --base main --head feature --pr 42 --format both
```

`analyze` accepts `--readable a,b` (audience filter), `--max-depth`, `--max-results`, and
`--github-repo OWNER/REPO` (posts/updates one PR comment; needs `$GITHUB_TOKEN`).

## How it works

`git base..head` -> AST extraction per repo (cached by commit + extractor version) ->
identity resolution (R1 generated client, R2 resolved HTTP call via `tia-services.yaml`,
R3 declared contract, R5 unique path match = *inferred*) -> bounded two-pass traversal
(confirmed edges first, then inferred) -> basic REST contract diff -> deterministic severity
rules -> report with unknowns, coverage and resolution rate.

The impact graph is built from the **base** revision of the changed repo (consumers depend on
the contract as it was); the head revision is used only for diffing.

Modules: `extract_python` · `resolve` · `graph` · `contracts` · `severity` · `impact` ·
`report` · `store` (SQLite + `audit.jsonl`) · `github` · `api` · `cli`.

## Service map (`tia-services.yaml`, owner-maintained, one per repo)

```yaml
service: reconciliation-service
owner: team-recon
criticality: critical            # enables SEV-*-CRITICAL rules
config_keys: {PAYMENTS_BASE_URL: payments-service}   # base-URL key -> provider (R2)
hosts: {payments.internal: payments-service}
packages: {payments_client: payments-service}        # generated client package -> provider (R1)
```

## What is implemented vs the PRD

| Area | State |
|---|---|
| Python (FastAPI / Flask, requests / httpx) and Java (Spring Web, RestTemplate / WebClient / OpenFeign) adapters | Implemented and tested; the Java adapter is also checked on real Spring code |
| FR-006 / FR-006a data layer (ORM + migrations) | Implemented: JPA / Spring Data, SQLAlchemy / SQLModel / Django models; Flyway, Liquibase, Alembic, Django migrations. Raw SQL and stored procedures are reported as "data access not analysed" (FR-041 is not built) |
| AC-024 data impact | Implemented: a migration that drops a column is traced to every service that maps it (shared database declared in `tia-services.yaml` or named by `spring.datasource.url`), then on to API consumers |
| FR-018 basic REST diff (P0 list) | Implemented; anything else reported as "compatibility not evaluated" |
| Report status, unknowns, coverage, resolution rate, redaction, audit, determinism | Implemented and tested (AC-001..006, 009, 010, 012, 015..024) |
| FR-003 / FR-005 persisted versioned graph, incremental extraction, stale relationships, freshness, evidence snapshots | Implemented on multi-tenant PostgreSQL (forced RLS); verified on real PostgreSQL and a live GitHub sandbox |
| GitHub: clone, PR fetch, comment upsert, signed webhook receiver | Verified live against GitHub with a personal access token; webhook delivery is only simulated (no public endpoint yet); a neutral check run and a publish endpoint are built but only simulated (they need a GitHub App); Azure DevOps is not built |
| Permalink report page (`/analyses/{id}`) | Implemented; tested in a real browser at desktop and mobile widths |
| Tenant lifecycle and storage quota (suspend, resume, offboard with deletion certificate) | Implemented; verified live on PostgreSQL through the admin CLI, API and worker |
| Secrets (envelope encryption, secret references, scrubbing, TLS enforcement) | Implemented; Vault only tested against a fake, PostgreSQL TLS not verified (embedded PostgreSQL has none), no KMS wrappers |
| Observability (Prometheus metrics, JSON logs, traces to OTLP, health/readiness) | Implemented; OTLP only tested against a fake collector |
| Per-tenant configuration (limits, severity overrides, criticality, gateway prefixes; versioned) | Implemented; verified live through admin CLI, API, worker and browser |
| Code upload (zip / folder, 2 GiB cap) | Implemented and tested in a real browser |
| Phase 2+ (CI, coverage, events, runtime, policy, ITSM, MCP) | Not started |
| GitHub App connector | Implemented: App JWT, installation tokens (comments, **real check runs**, clone/fetch), webhooks routed to the tenant owning the installation, linking by the operator or by an administrator who proves access through GitHub, repository discovery. Verified against a GitHub platform stand-in (live through the real processes); **no real GitHub App has been registered or used** |
| Sign-in and permissions | Implemented: OIDC (browser and bearer tokens), SAML 2.0, scoped service tokens, five roles on a deny-by-default route table, per-repository access mirrored from GitHub (unreadable repositories answer 404; reports redacted per reader; a PR comment names another repository only if all its readers may read it); the credential names the tenant. Verified against our own OIDC/SAML provider and a fake GitHub API (live in a real browser); **no real identity provider has been used**, Azure DevOps permission mirroring is not built, rate limiting is left to a reverse proxy |
| Accuracy (PRD 15.3) | Measured with `tools/accuracy.py` against curated truth: two **independent** real-code Java landscapes (spring-petclinic-microservices, piggymetrics) and a synthetic Python one; every measured target met. Small samples; no independent Python reference yet; the assessment-time target is not measured; context paths from external config must be set by hand |
| CI | `.github/workflows/tia-ci.yml` and `tools/ci.py` (tests, package, migration and tenant-table gates, secret scan, dependency audit); **never run on GitHub Actions** |
| Performance (PRD 12) | `tools/benchmark.py`: at 500 generated repositories / 0.6 M lines, PR analysis P95 5.3 s, report P95 0.07 s, a 50-job burst P95 163 s; the 20 M LOC scan and availability targets are **not demonstrated** (see `docs/PERFORMANCE.md`) |
| Security | `docs/SECURITY.md`: threat model and an independent code review (2 high, 8 medium fixed except one open medium); **not an external penetration test** |
| Documentation | `docs/ADMIN-GUIDE.md`, `docs/USER-GUIDE.md`, `docs/API.md` (generated), all checked against the code by tests |

## Known limits

- Static call resolution only: dynamic URLs, env-supplied hosts not in the service map, and
  wrapper clients stay *unresolved* (listed with file/line) or *inferred* - never guessed.
- Intra-repo `CALLS` match by simple name; ambiguous matches are counted as an unknown, not linked.
- Test association is static (import+call, HTTP-call to endpoint, or naming = inferred); no CI results.
- The demo landscapes are synthetic; precision/recall have **not** been measured on real multi-repository landscapes.
- Data impact: tables belong to a database, and two services are linked only when they name the same
  database (`database:` in `tia-services.yaml`, or a literal `spring.datasource.url`); otherwise each service's
  database is private and the report says so. Only columns an analysed entity maps are traced.
- Python is parsed with the server's own interpreter, so syntax newer than that interpreter is reported as a parse
  failure (the analysis is then marked partial).
