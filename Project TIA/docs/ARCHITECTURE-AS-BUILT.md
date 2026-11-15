# TIA architecture as built

The system as it is coded today: 100 Python modules (26,644 lines) in 10 layers, 29 PostgreSQL tables, three processes from one container image. Everything here is read from the code by `tools/gen_architecture.py`; the design written before the build is [ARCHITECTURE.md](ARCHITECTURE.md), and section 7 says where the two differ.

**Status: Built.** The layer rule in section 2 is checked against the real imports on every test run (`tests/test_architecture_doc.py`), so this document cannot silently go out of date.

---

## 1. The system in one picture

<!-- figure: system -->

| Process | Command | Database role | Listens on, and does |
|---|---|---|---|
| API server | `python -m tia.server` | `tia_app` | 8080 (HTTP), web UI, REST, `/mcp`, `/query/tia`, `/healthz`, `/metrics` |
| Worker | `python -m tia.worker` | `tia_worker` | 9090 (health and metrics only); claims jobs, fetches from git hosts |
| Migration | `python -m tia.migrate` | owner (admin DSN) | none; runs once before every install or upgrade (Helm pre-install hook, Compose `migrate` service) |
| Command line | `tia analyze` (`tia.cli:main`) | `tia_app`, optional | none; an analysis from a terminal, with or without a database |

All three run the same image (`deploy/Dockerfile`, entry point `python -m`, non-root user, read-only file system). The API and the worker scale independently. They share two things only: the PostgreSQL database and the `repos` volume. Nothing else holds state.

## 2. Layers

<!-- figure: layers -->

**The rule:** a module may use only modules in its own layer or in the layers below it. Requests come in at the top; data and rules sit at the bottom. So the impact engine cannot call a git host or ServiceNow (connectors are above it), and storage cannot call the engine.

| # | Layer | Modules | Lines | Responsible for |
|---|---|---|---|---|
| 1 | Entry points | 4 | 376 | The processes and commands that start everything: the API server, the queue worker, the command line and the schema migration. |
| 2 | HTTP API, web UI and agent surfaces | 10 | 4,295 | Every way in: the REST API and web UI, sign-in, permissions on each request, the shareable report page, MCP for coding assistants and the Knowledge Fabric adapter. Requests are authenticated, classified and bound to one tenant here, before any work starts. |
| 3 | Orchestration, ingestion and jobs | 8 | 1,797 | Turns a request into work: resolves revisions, registers and syncs repositories, queues jobs and runs an analysis end to end, the same way for the UI, the API and a pull-request webhook. |
| 4 | Source-control and change-management connectors | 8 | 1,549 | Talks to the outside on the platform's behalf: webhooks in, pull-request comments and check runs out, repository permissions mirrored from the host, ServiceNow change records. Nothing below this layer calls a git host or ITSM. |
| 5 | Impact engine and tester views | 17 | 4,975 | The deterministic core: diff the change, walk the graph within bounds, attach evidence, score with the rule table, and turn the result into the report and the tester views (test scope, regression test selection, configuration checks, coverage, automation inventory, tester pack, graph questions). |
| 6 | Graph and identity resolution | 4 | 791 | Connects facts from many repositories into one typed graph: who calls which endpoint, which code reads which table, with the resolution strategy and evidence on every edge. Versioned graphs are stored, never edited. |
| 7 | Extraction (language adapters) | 21 | 7,137 | Reads code as bytes and parses it into facts (endpoints, call sites, functions, schemas, tables, tests, UI elements). Never imports, runs or builds customer code. Facts are cached per git blob, so an unchanged file is never parsed twice. |
| 8 | Tenancy, identity and persistence | 11 | 2,984 | Everything stored, and who it belongs to: tenant-bound database transactions, the analysis and evidence store with its audit chain, tenants and their configuration, users, roles, secrets and customer keys, retention and backup. |
| 9 | Rules (deterministic, versioned) | 5 | 861 | The rule tables and their evaluation: severity, release policy, contract and event compatibility, team coordination. They depend on nothing but the foundations. Tenants can override them through configuration; every finding records which rule version produced it. One module here also acts: `coordination` delivers the team notifications its rules call for (chat webhook or e-mail, through the air-gap and outbound-safety guards). |
| 10 | Foundations | 12 | 1,879 | Shared building blocks with no business logic: the data model, read-only git access, secret scrubbing, safe outbound connections, air-gap guard, transport security, logging, metrics and tracing. |

### Imports between layers

How many module imports go from each layer (row) to each layer (column), counted from the code. Every count is on or to the right of the diagonal (a layer using itself or a layer below it), except the recorded exceptions below.

| From \ to | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1. Entry points | · | 3 | 3 | 4 | 5 | 1 | · | 6 | · | 12 |
| 2. HTTP API, web UI and agent surfaces | · | 9 | 7 | 7 | 11 | 2 | 2 | 15 | 1 | 11 |
| 3. Orchestration, ingestion and jobs | 1 | · | 5 | 7 | 5 | 3 | 2 | 6 | 1 | 8 |
| 4. Source-control and change-management connectors | · | · | · | 8 | 4 | · | · | 1 | 3 | 15 |
| 5. Impact engine and tester views | · | · | · | · | 21 | 8 | 4 | 2 | 4 | 15 |
| 6. Graph and identity resolution | · | · | · | · | · | 3 | 2 | 1 | · | 4 |
| 7. Extraction | · | · | · | · | · | · | 29 | 1 | 2 | 19 |
| 8. Tenancy, identity and persistence | · | · | 1 | · | · | · | · | 17 | 5 | 13 |
| 9. Rules | · | · | · | · | · | · | · | · | 1 | 4 |
| 10. Foundations | · | · | · | · | · | · | · | · | · | 12 |

### Recorded exceptions to the rule

| Import | Why it is allowed |
|---|---|
| `analysis_service` uses `worker` (layer 3 to 1) | Imports the `PermanentError` exception class so a job handler can tell the worker not to retry; no worker code runs. |
| `store` uses `repos` (layer 8 to 3) | Lazy import of the `Repo` record type (a dataclass) to rebuild registry rows; no repository sync is called from storage. |

Any other import that crosses the rule stops `python tools/gen_architecture.py` and fails the test suite.

## 3. Every module, by layer

What each module does is its own description (the first sentence of its docstring). *Uses* and *Used by* are its imports of other TIA modules.

### 3.1 Entry points

The processes and commands that start everything: the API server, the queue worker, the command line and the schema migration.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`server`](../tia/server.py) | 105 | Run the TIA server: python -m tia.server | 11 modules (layer 2: 2, layer 3: 1, layer 4: 2, layer 8: 2, layer 10: 4) | none |
| [`worker`](../tia/worker.py) | 113 | Worker loop: claims jobs from the queue and runs handlers. | `analysis_service`, `credentials`, `db`, `health`, `jobs`, `metrics`, `obs`, `tracing`, `transport`, `webhooks` | `analysis_service` |
| [`cli`](../tia/cli.py) | 121 | tia command line: analyze | `automation`, `coverage`, `db`, `github`, `graph`, `impact`, `report`, `scrub`, `store`, `testscope` | none |
| [`migrate`](../tia/migrate.py) | 37 | Apply database migrations and manage tenants | `credentials`, `db`, `transport` | none |

### 3.2 HTTP API, web UI and agent surfaces

Every way in: the REST API and web UI, sign-in, permissions on each request, the shareable report page, MCP for coding assistants and the Knowledge Fabric adapter. Requests are authenticated, classified and bound to one tenant here, before any work starts.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`api`](../tia/api.py) | 1,983 | TIA HTTP API and web UI (TFD section 8). | 50 modules (layer 2: 7, layer 3: 7, layer 4: 7, layer 5: 11, layer 6: 2, layer 7: 2, layer 8: 8, layer 9: 1, layer 10: 5) | `server` |
| [`authapi`](../tia/authapi.py) | 618 | Authentication, authorisation and the sign-in/administration endpoints (PL-001, PL-002). | `identity`, `jwtverify`, `netsafe`, `oidc`, `rbac`, `saml` | `api`, `server` |
| [`oidc`](../tia/oidc.py) | 133 | OpenID Connect (PL-001): browser sign-in by the authorization-code flow with PKCE, and bearer-token verification for API clients. | `identity`, `jwtverify`, `netsafe` | `authapi` |
| [`saml`](../tia/saml.py) | 216 | SAML 2.0 service provider (PL-001): SP-initiated sign-in, HTTP-Redirect request, HTTP-POST response. | `identity` | `authapi` |
| [`ratelimit`](../tia/ratelimit.py) | 112 | Request rate limiting (SECURITY T17, PRD 11 SaaS operations). | none | `api` |
| [`gate`](../tia/gate.py) | 92 | Request-path enforcement of the tenant lifecycle (UC1-20): is this tenant allowed to act, and is there storage room? | `db`, `tenants` | `api` |
| [`health`](../tia/health.py) | 214 | Health, readiness and scrape-time metrics (PL-007). | `credentials`, `db`, `metrics` | `api`, `worker` |
| [`reportpage`](../tia/reportpage.py) | 417 | The shareable report page for one analysis (UC1-16): what the PR comment and the check run link to. | none | `api` |
| [`mcp`](../tia/mcp.py) | 119 | MCP server for coding assistants (PRD FR-037, AC-032): read-only tools over the Model Context Protocol (JSON-RPC 2.0, the streamable-HTTP transport answered with single JSON responses) at POST /mcp, behind the same authentication ... | none | `api` |
| [`kfadapter`](../tia/kfadapter.py) | 391 | Knowledge Fabric V2.5 adapter: a finished Change Impact Report in the shape KF agents already consume (QueryResponse). | none | `api` |

### 3.3 Orchestration, ingestion and jobs

Turns a request into work: resolves revisions, registers and syncs repositories, queues jobs and runs an analysis end to end, the same way for the UI, the API and a pull-request webhook.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`analysis_service`](../tia/analysis_service.py) | 479 | Resolve -> execute an impact analysis. | 23 modules (layer 1: 1, layer 3: 3, layer 4: 7, layer 5: 3, layer 6: 2, layer 7: 2, layer 8: 3, layer 9: 1, layer 10: 1) | `api`, `worker` |
| [`jobs`](../tia/jobs.py) | 184 | PostgreSQL job queue (UC1-03). | `db`, `saas`, `scrub` | `analysis_service`, `api`, `worker` |
| [`repos`](../tia/repos.py) | 463 | Repository ingestion: register and sync repositories from GitHub, Azure DevOps, any git URL, or a local path. | `airgap`, `credentials`, `gitutil`, `metrics`, `netsafe`, `tracing`, `upload` | `analysis_service`, `api`, `store`, `upload` |
| [`upload`](../tia/upload.py) | 258 | Code upload (UC1 ingestion): a zip archive or a whole folder from the user's own drive becomes a git repository in TIA's storage, so uploaded code gets the same revisions, diffs, graph and impact analysis as a cloned repository. | `repos` | `api`, `repos`, `server` |
| [`relationships`](../tia/relationships.py) | 124 | Relationship history and staleness (UC1-12, AC-011, FR-005). | `graph`, `store` | `analysis_service` |
| [`incidents`](../tia/incidents.py) | 96 | Incident association and historical investigation (PRD FR-031, FR-032, AC-034). | `evidence`, `testscope` | `api` |
| [`temporal`](../tia/temporal.py) | 72 | Temporal relationships and release comparisons (PRD FR-011, FR-030, AC-033). | none | `api` |
| [`calibration`](../tia/calibration.py) | 121 | Severity-rule calibration from validated outcomes (PRD FR-044, AC-038) and change-risk trends (FR-043). | none | `api` |

### 3.4 Source-control and change-management connectors

Talks to the outside on the platform's behalf: webhooks in, pull-request comments and check runs out, repository permissions mirrored from the host, ServiceNow change records. Nothing below this layer calls a git host or ITSM.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`webhooks`](../tia/webhooks.py) | 114 | SCM webhook receiving (UC1-05): signature verification, pull-request event parsing, repository matching, and the server-side connector configuration used by the webhook endpoint and the worker. | `credentials`, `transport` | `access`, `analysis_service`, `api`, `githubapp`, `publish`, `server`, `worker` |
| [`github`](../tia/github.py) | 178 | Publish the advisory PR comment (PRD FR-035 P0) and a check run. | `airgap`, `metrics`, `policy`, `report`, `tracing` | `access`, `cli`, `githubapp`, `publish` |
| [`githubapp`](../tia/githubapp.py) | 257 | GitHub App connector (UC1-04): the platform's own GitHub App replaces personal access tokens. | `airgap`, `github`, `jwtverify`, `webhooks` | `analysis_service`, `api` |
| [`publish`](../tia/publish.py) | 92 | Publish an analysis to a GitHub pull request: one advisory comment and one advisory check run, both updated in place. | `github`, `metrics`, `report`, `webhooks` | `analysis_service`, `api` |
| [`scmhosts`](../tia/scmhosts.py) | 273 | GitLab and Bitbucket connectors (PRD FR-039, AC-036): the same capabilities as GitHub - receiving merge/pull-request events, revision-aware analysis, one advisory comment updated in place plus a commit status, and the permission ... | `airgap`, `credentials`, `metrics`, `policy`, `report` | `access`, `analysis_service`, `api` |
| [`ado`](../tia/ado.py) | 277 | Azure DevOps connector (UC1-06 service hooks, UC1-15 pull-request thread and status). | `airgap`, `credentials`, `metrics`, `policy`, `report` | `access`, `analysis_service`, `api`, `server` |
| [`itsm`](../tia/itsm.py) | 133 | Change-management integration: ServiceNow first (PRD FR-038, AC-031). | `netsafe` | `analysis_service`, `api` |
| [`access`](../tia/access.py) | 225 | Per-repository access control (PL-002, AC-016): which repositories a principal may read, mirrored from the source-code host. | `ado`, `github`, `identity`, `scmhosts`, `webhooks` | `analysis_service`, `api` |

### 3.5 Impact engine and tester views

The deterministic core: diff the change, walk the graph within bounds, attach evidence, score with the rule table, and turn the result into the report and the tester views (test scope, regression test selection, configuration checks, coverage, automation inventory, tester pack, graph questions).

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`impact`](../tia/impact.py) | 987 | Change-impact engine. | 26 modules (layer 5: 8, layer 6: 4, layer 7: 3, layer 8: 2, layer 9: 4, layer 10: 5) | `analysis_service`, `api`, `cli` |
| [`enrich`](../tia/enrich.py) | 376 | Evidence enrichment of an analysis (PRD FR-022 to FR-029): CI results, coverage, runtime observations and deployments, read from TIA's evidence store (never queried live) and pinned in the analysis's evidence snapshot. | `evidence`, `graph`, `models` | `api`, `impact` |
| [`evidence`](../tia/evidence.py) | 433 | Ingested evidence (PRD FR-022 CI results, FR-023 coverage, FR-026/027 runtime, FR-028/029 deployments): parsers and the tenant store. | `models`, `scrub` | `analysis_service`, `api`, `enrich`, `incidents` |
| [`report`](../tia/report.py) | 386 | Report rendering and audience filtering (PRD section 8, 9.1, 11.1). | `configkeys`, `diagram`, `regression`, `testscope` | `ado`, `analysis_service`, `api`, `cli`, `github`, `publish`, `scmhosts` |
| [`findingfilter`](../tia/findingfilter.py) | 98 | Report filters (PRD 9.3): severity; service or repository; direct versus transitive impact; contract, data, test, runtime and operational impact; evidence status; environment; policy result; unresolved or stale evidence. | none | `api` |
| [`findings_catalog`](../tia/findings_catalog.py) | 98 | Explainable finding catalog packaging (Pulse RULE_CATALOG shape, TIA content). | none | `impact`, `structure` |
| [`structure`](../tia/structure.py) | 175 | Structural risk amplifiers on the evidence graph (Pulse cycles / coupling / layers port). | `findings_catalog` | `impact` |
| [`communities`](../tia/communities.py) | 99 | File-level communities and god nodes (Pulse Leiden port, pure-Python label propagation). | `models` | `impact` |
| [`diagram`](../tia/diagram.py) | 99 | Mermaid architecture block for impact reports (Pulse architecture-diagram port). | none | `impact`, `report` |
| [`testscope`](../tia/testscope.py) | 396 | Test scope (UC-1 in docs/CASE-STUDY.md): what a change means for a tester, in things a tester can test without reading code. | `models` | `api`, `cli`, `coverage`, `impact`, `incidents`, `regression`, `report`, `testerpack` |
| [`coverage`](../tia/coverage.py) | 343 | Technical coverage (UC-2, docs/CASE-STUDY.md): "are my test cases enough?" on the technical side. | `automation`, `graph`, `models`, `testscope` | `api`, `cli`, `regression`, `testerpack` |
| [`automation`](../tia/automation.py) | 401 | Automation inventory (UC-3, docs/CASE-STUDY.md): what an automation tester needs from the code, without reading it. | `models` | `api`, `cli`, `coverage`, `testerpack` |
| [`testerpack`](../tia/testerpack.py) | 101 | The tester pack of one analysis (docs/CASE-STUDY.md: one change, three answers from the same code facts). | `automation`, `coverage`, `testscope` | `api` |
| [`graphquery`](../tia/graphquery.py) | 156 | Direct questions to the code graph, for testing agents and people (no change needed) | `models` | `api` |
| [`graphview`](../tia/graphview.py) | 264 | Code / dependency graph views for the UI (PRD 9.2 supporting visualisation). | `graph`, `models`, `resolve` | `api` |
| [`regression`](../tia/regression.py) | 150 | Regression test selection (docs/MEETING-PAWAN.md, use case 1): "out of your 10 test cases, these 4 must be regression-tested". | `coverage`, `testscope` | `impact`, `report` |
| [`configkeys`](../tia/configkeys.py) | 413 | Configuration and environment-variable changes (docs/MEETING-PAWAN.md, ask 1a: "he also added a new environment variable that other methods use - test these too"). | `exclude`, `gitutil` | `impact`, `report` |

### 3.6 Graph and identity resolution

Connects facts from many repositories into one typed graph: who calls which endpoint, which code reads which table, with the resolution strategy and evidence on every edge. Versioned graphs are stored, never edited.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`graph`](../tia/graph.py) | 203 | In-memory typed dependency graph built from repository extracts. | `data`, `extract_python`, `models`, `resolve` | `analysis_service`, `api`, `cli`, `coverage`, `enrich`, `graphstore`, `graphview`, `impact`, `relationships` |
| [`resolve`](../tia/resolve.py) | 163 | Identity and relationship resolution (TFD 5.4 / 5.5). | `models` | `graph`, `graphview`, `impact` |
| [`graphstore`](../tia/graphstore.py) | 48 | Persisted, versioned dependency graph (UC1-11). | `__init__`, `graph`, `models`, `store` | `analysis_service`, `api`, `impact` |
| [`data`](../tia/data.py) | 377 | Data layer of the graph and data-change detection (FR-006, FR-006a, FR-015, AC-024). | `models` | `graph`, `impact` |

### 3.7 Extraction (language adapters)

Reads code as bytes and parses it into facts (endpoints, call sites, functions, schemas, tables, tests, UI elements). Never imports, runs or builds customer code. Facts are cached per git blob, so an unchanged file is never parsed twice.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`extraction`](../tia/extraction.py) | 68 | Incremental extraction (UC1-11, AC-021): per-file facts are cached by git blob id, so a commit that changes one file re-extracts only that file. | `__init__`, `exclude`, `extract_python`, `gitutil`, `models`, `store`, `telemetry`, `tracing` | `analysis_service`, `impact` |
| [`tia (package root)`](../tia/__init__.py) | 11 | TIA - Technical Impact Analysis. | `adapters` | `analysis_service`, `api`, `extract_data`, `extract_java`, `extract_openapi`, `extract_python`, `extraction`, `graphstore`, `impact`, `lang/common` |
| [`adapters`](../tia/adapters.py) | 130 | Language adapter registry (PRD FR-040, adapter SDK). | `lang`, `models` | `__init__`, `extract_python` |
| [`extract_python`](../tia/extract_python.py) | 1,114 | Python source adapter: endpoints, schemas, outbound call sites, functions, tests. | 15 modules (layer 7: 12, layer 9: 1, layer 10: 2) | `extraction`, `graph` |
| [`extract_django`](../tia/extract_django.py) | 502 | Django and Django REST framework routes (PRD FR-004: "Python: FastAPI / Flask / Django REST (routes)"). | `models` | `extract_python` |
| [`extract_java`](../tia/extract_java.py) | 1,151 | Java source adapter (Spring Boot / Spring Web): endpoints, DTO schemas, outbound HTTP call sites, functions, tests. | `__init__`, `events`, `extract_data`, `models` | `extract_python` |
| [`extract_data`](../tia/extract_data.py) | 1,149 | Data-layer adapter: schema migrations and assembly-time resolution of ORM mappings (FR-006, FR-006a). | `__init__`, `models` | `extract_java`, `extract_orm`, `extract_python`, `sqllineage`, `streams` |
| [`extract_orm`](../tia/extract_orm.py) | 360 | Python ORM adapter: SQLAlchemy (declarative, Mapped/mapped_column, Flask-SQLAlchemy), SQLModel and Django models. | `extract_data`, `models` | `extract_python` |
| [`extract_openapi`](../tia/extract_openapi.py) | 146 | OpenAPI / Swagger adapter (PRD FR-017 "external catalogs enrich and corroborate", FR-018 full comparison). | `__init__`, `models` | `extract_python` |
| [`extract_ui`](../tia/extract_ui.py) | 620 | Front-end adapter (UC-3, docs/CASE-STUDY.md): pages, their UI elements, how to locate each one uniquely, whether it is mandatory, its input rules, the values a dropdown offers, and the API calls a page makes. | `models` | `extract_python` |
| [`sqllineage`](../tia/sqllineage.py) | 296 | SQL and stored-procedure data lineage (PRD FR-041, AC-037). | `extract_data` | `extract_python` |
| [`streams`](../tia/streams.py) | 189 | Event-stream and data-pipeline lineage (PRD FR-042). | `extract_data`, `models` | `extract_python` |
| [`testcases`](../tia/testcases.py) | 79 | Written (manual) test cases kept in a repository: `testcases/*.csv` / *.yaml / *.yml / *.json (UC-2, docs/CASE-STUDY.md). | none | `extract_python`, `impact` |
| [`registry`](../tia/registry.py) | 109 | Ownership and criticality registry connector (PRD 7.3 criticality, FR-033 owner approval, FR-045 owning teams). | none | `api`, `extract_python` |
| [`exclude`](../tia/exclude.py) | 57 | Tenant-configurable path exclusion for extraction (Pulse exclude-patterns port). | none | `configkeys`, `extraction` |
| [`lang`](../tia/lang/__init__.py) | 2 | Language adapters beyond Python and Java (PRD FR-040): C#/.NET, TypeScript/JavaScript, Go, Kotlin. | none | `adapters` |
| [`lang/common`](../tia/lang/common.py) | 128 | Shared helpers for the tree-sitter based language adapters. | `__init__`, `models` | `lang/csharp`, `lang/golang`, `lang/kotlin`, `lang/typescript` |
| [`lang/csharp`](../tia/lang/csharp.py) | 323 | C#/.NET adapter (PRD FR-040: ASP.NET Core, HttpClient). | `lang/common`, `models` | none |
| [`lang/golang`](../tia/lang/golang.py) | 236 | Go adapter (PRD FR-040: net/http, chi, gin; also echo and gorilla/mux). | `lang/common`, `models` | none |
| [`lang/kotlin`](../tia/lang/kotlin.py) | 197 | Kotlin adapter (PRD FR-040: Spring). | `lang/common`, `models` | none |
| [`lang/typescript`](../tia/lang/typescript.py) | 270 | TypeScript/JavaScript adapter (PRD FR-040: Express, NestJS, fetch, axios). | `lang/common`, `models` | none |

### 3.8 Tenancy, identity and persistence

Everything stored, and who it belongs to: tenant-bound database transactions, the analysis and evidence store with its audit chain, tenants and their configuration, users, roles, secrets and customer keys, retention and backup.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`db`](../tia/db.py) | 109 | PostgreSQL access: connection pool with tenant-bound transactions, migrations, tenant registry. | none | 13 modules (layer 1: 4, layer 2: 3, layer 3: 1, layer 8: 5) |
| [`store`](../tia/store.py) | 329 | Tenant-scoped persistence on PostgreSQL: evidence cache, analyses, repository registry, audit chain. | `db`, `identity`, `models`, `repos`, `scrub`, `tenantconfig` | `analysis_service`, `api`, `cli`, `extraction`, `graphstore`, `impact`, `relationships`, `retention` |
| [`tenants`](../tia/tenants.py) | 532 | Tenant lifecycle (UC1-20, PL-013, AC-039): provision, suspend / resume, SCM installation mapping, storage quota, offboarding with verified deletion. | `credentials`, `db`, `identity`, `kms`, `saas`, `secretstore`, `tenantconfig` | `gate`, `secretstore` |
| [`tenantconfig`](../tia/tenantconfig.py) | 409 | Per-tenant configuration, versioned (UC1-24; PRD 7.3 configurable severity, FR-016 configurable limits, PL-003 per-tenant configuration). | `contracts`, `coordination`, `events`, `models`, `policy`, `retention`, `scrub`, `severity` | `analysis_service`, `api`, `impact`, `retention`, `store`, `tenants` |
| [`identity`](../tia/identity.py) | 585 | Identity store (PL-001, PL-002): identity providers, users, browser sessions, service-account tokens, and the Principal every request runs as. | `db`, `jwtverify`, `netsafe`, `rbac`, `scrub` | `access`, `api`, `authapi`, `oidc`, `saml`, `store`, `tenants` |
| [`rbac`](../tia/rbac.py) | 126 | Roles, permissions and the route table (PL-002). | none | `authapi`, `identity` |
| [`secretstore`](../tia/secretstore.py) | 231 | Per-tenant secret storage with envelope encryption (PL-011; the key hierarchy of PL-016 without customer-managed keys). | `credentials`, `db`, `tenants` | `analysis_service`, `api`, `server`, `tenants` |
| [`kms`](../tia/kms.py) | 208 | Customer-managed keys (PRD PL-016 BYOK): a tenant's data-encryption key wrapped by a key in the CUSTOMER's KMS. | `airgap`, `credentials` | `api`, `tenants` |
| [`saas`](../tia/saas.py) | 118 | SaaS operations per tenant: usage metering and plan limits (PRD PL-014), data residency (PL-015, AC-041). | none | `api`, `jobs`, `tenants` |
| [`retention`](../tia/retention.py) | 143 | Data retention (PRD 11.4) and audit reporting. | `credentials`, `db`, `store`, `tenantconfig` | `api`, `tenantconfig` |
| [`backup`](../tia/backup.py) | 194 | Backup, restore and disaster recovery (PRD PL-005). | `credentials` | none |

### 3.9 Rules (deterministic, versioned)

The rule tables and their evaluation: severity, release policy, contract and event compatibility, team coordination. They depend on nothing but the foundations. Tenants can override them through configuration; every finding records which rule version produced it. One module here also acts: `coordination` delivers the team notifications its rules call for (chat webhook or e-mail, through the air-gap and outbound-safety guards).

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`severity`](../tia/severity.py) | 78 | Severity rules (PRD 7.3). | `models` | `impact`, `tenantconfig` |
| [`policy`](../tia/policy.py) | 277 | Release policy (PRD FR-033 configurable rules, FR-034 evaluation, FR-036 advisory and enforcement modes, AC-013, AC-014, AC-030). | `coordination` | `ado`, `api`, `github`, `impact`, `scmhosts`, `tenantconfig` |
| [`contracts`](../tia/contracts.py) | 147 | REST contract comparison (PRD FR-018 basic and full). | `models` | `impact`, `tenantconfig` |
| [`events`](../tia/events.py) | 162 | Event and message contracts (PRD FR-019, FR-020 events, AC-028). | none | `extract_java`, `extract_python`, `impact`, `tenantconfig` |
| [`coordination`](../tia/coordination.py) | 197 | Multi-team change coordination (PRD FR-045). | `airgap`, `netsafe` | `analysis_service`, `policy`, `tenantconfig` |

### 3.10 Foundations

Shared building blocks with no business logic: the data model, read-only git access, secret scrubbing, safe outbound connections, air-gap guard, transport security, logging, metrics and tracing.

| Module | Lines | What it does | Uses | Used by |
|---|---|---|---|---|
| [`models`](../tia/models.py) | 267 | Canonical data structures (TECHNICAL-FRAMEWORK.md section 5). | none | 32 modules (layer 5: 9, layer 6: 4, layer 7: 15, layer 8: 2, layer 9: 2) |
| [`gitutil`](../tia/gitutil.py) | 136 | Read-only git access. | none | `analysis_service`, `api`, `configkeys`, `extraction`, `impact`, `repos` |
| [`scrub`](../tia/scrub.py) | 179 | Keep secrets out of TIA's records and reports (PL-011; PRD 11.4 P0: "secrets, credentials ... must not be copied into graph records or reports"; TFD 11: credentials found in source files are never copied). | none | 11 modules (layer 1: 1, layer 2: 1, layer 3: 1, layer 5: 2, layer 7: 1, layer 8: 3, layer 10: 2) |
| [`credentials`](../tia/credentials.py) | 248 | Where TIA's own secrets come from (PL-011): an approved secrets manager, never source code and never the database in clear. | `airgap` | 15 modules (layer 1: 3, layer 2: 1, layer 3: 1, layer 4: 3, layer 8: 5, layer 10: 2) |
| [`transport`](../tia/transport.py) | 68 | Encryption in transit (PL-011): refuse insecure transport instead of discovering it later. | `airgap`, `credentials` | `migrate`, `server`, `tracing`, `webhooks`, `worker` |
| [`netsafe`](../tia/netsafe.py) | 154 | Outbound fetches to addresses a tenant administrator controls (identity-provider discovery, JWKS, token endpoint). | `airgap` | `authapi`, `coordination`, `identity`, `itsm`, `oidc`, `repos` |
| [`airgap`](../tia/airgap.py) | 62 | Air-gapped operation (PRD deployment option: no connection outside the customer's network). | none | 11 modules (layer 3: 1, layer 4: 4, layer 8: 1, layer 9: 1, layer 10: 4) |
| [`jwtverify`](../tia/jwtverify.py) | 186 | JSON Web Token verification for OpenID Connect (PL-001), on the `cryptography` package, no other dependency. | none | `authapi`, `githubapp`, `identity`, `oidc` |
| [`metrics`](../tia/metrics.py) | 227 | Platform and analysis metrics in the Prometheus text exposition format (PL-007, TFD 14). | none | `ado`, `api`, `github`, `health`, `publish`, `repos`, `scmhosts`, `telemetry`, `tracing`, `worker` |
| [`telemetry`](../tia/telemetry.py) | 53 | Turn finished work into metrics: what an analysis and an extraction produced. | `metrics` | `extraction`, `impact` |
| [`tracing`](../tia/tracing.py) | 207 | Distributed tracing (PL-007): W3C Trace Context in and out, spans around the work that matters, optional OTLP/HTTP export. | `airgap`, `credentials`, `metrics`, `scrub`, `transport` | `api`, `extraction`, `github`, `impact`, `obs`, `repos`, `server`, `worker` |
| [`obs`](../tia/obs.py) | 92 | Structured logging (PL-007): one JSON object per line, with the request and trace it belongs to, and no secrets. | `scrub`, `tracing` | `api`, `server`, `worker` |

## 4. How work flows through the layers

### 4.1 Someone runs an analysis (web UI or REST)

1. `api` receives `POST /api/v1/impact-analyses`. The middleware in `authapi` classifies the route with `rbac` (a route with no entry is refused), `ratelimit` and `gate` check the caller and the tenant, and the database transaction is bound to that tenant (`db`).
2. `analysis_service` resolves the request into exact commits (`repos`, `gitutil`) and caps its limits with the tenant's configuration (`tenantconfig`). With `?async=true` it is queued instead (`jobs`) and a `worker` picks it up; the steps below are the same.
3. `extraction` lists the changed files with `gitutil` and parses only blobs it has not seen (`extract_python`, `extract_java`, `extract_data`, `extract_ui`, the `lang` adapters); facts are cached per blob in `file_extracts`.
4. `graph` and `resolve` connect the facts into one typed graph; `graphstore` records its version in `graph_versions`.
5. `impact` diffs the change (`contracts`, `data`, `events`), walks the graph within bounds, adds evidence (`enrich`, `evidence`), scores each finding (`severity`), evaluates release policy (`policy`) and builds the tester views (`testscope`, `coverage`, `automation`).
6. `report` renders the result; `store` saves it in `analyses` with its evidence snapshot id and appends to the `audit_log` hash chain. The web UI shows it; `reportpage` serves the shareable page; `testerpack` answers the tester-pack call.

### 4.2 A pull request is opened or updated

1. The host's webhook reaches `api`: `webhooks` (GitHub, with `githubapp` for App installations), `scmhosts` (GitLab, Bitbucket) or `ado` (Azure DevOps). The signature is verified and the repository matched before anything is queued.
2. `jobs` queues one analysis per pull request; a newer push supersedes the older queued job. `worker` claims it with `FOR UPDATE SKIP LOCKED` under a lease, and retries with backoff on failure.
3. `analysis_service` runs exactly the same analysis as above.
4. `publish` updates one advisory comment and one check run in place (`github`, or the matching connector); `access` keeps findings from repositories the pull request's readers cannot see out of the comment; `itsm` attaches the report to a ServiceNow change record when one is configured.

### 4.3 CI, deployments and runtime send evidence

1. Pipelines post test results, coverage, deployments and observed calls to `api` (`/api/v1/evidence/*`). `evidence` parses them, removes secrets (`scrub`) and stores them in `evidence_records`.
2. Nothing is fetched during an analysis: the next analysis reads those records through `enrich` and pins their ids in its snapshot (`evidence_snapshots`). Ingest first, then analyse.

### 4.4 An AI agent or another tool asks a question

1. Coding assistants and testing agents call `mcp` (JSON-RPC over `POST /mcp`, read-only tools only). Knowledge Fabric calls `kfadapter` through `POST /query/tia`. Scripts use the Python SDK in `sdk/python`. All of them go through `api`, so the same sign-in, permissions and tenant binding apply.
2. Direct questions about the code (who consumes this, what does it depend on, which tests cover it) are answered by `graphquery` from the stored graph; an ambiguous name returns the candidates instead of a guess.

## 5. Storage

One PostgreSQL database, shared by all tenants. Every tenant-owned row carries `tenant_id`; forced row-level security means a query sees only the rows of the tenant its transaction is bound to (`set_config('tia.tenant_id', ..., true)` in `tia/db.py`), and a transaction bound to no tenant sees nothing. The runtime roles are created without the right to bypass it.

| Table | Created by | Tenant isolation |
|---|---|---|
| `tenants` | `0001_init.sql` | no row-level security: a platform registry the runtime roles cannot read at all; they call narrow SECURITY DEFINER functions instead |
| `repositories` | `0001_init.sql` | forced row-level security |
| `extractions` | `0001_init.sql` | forced row-level security |
| `analyses` | `0001_init.sql` | forced row-level security |
| `audit_log` | `0001_init.sql` | forced row-level security |
| `jobs` | `0002_jobs.sql` | forced row-level security |
| `file_extracts` | `0003_file_extracts.sql` | forced row-level security |
| `graph_versions` | `0004_graphs.sql` | forced row-level security |
| `graph_heads` | `0004_graphs.sql` | forced row-level security |
| `relationships` | `0005_relationships.sql` | forced row-level security |
| `evidence_snapshots` | `0006_snapshots.sql` | forced row-level security |
| `tenant_installations` | `0007_tenant_lifecycle.sql` | no row-level security: a platform registry the runtime roles cannot read at all; they call narrow SECURITY DEFINER functions instead |
| `tenant_events` | `0007_tenant_lifecycle.sql` | no row-level security: a platform registry the runtime roles cannot read at all; they call narrow SECURITY DEFINER functions instead |
| `tenant_keys` | `0008_tenant_secrets.sql` | forced row-level security |
| `tenant_secrets` | `0008_tenant_secrets.sql` | forced row-level security |
| `worker_heartbeats` | `0009_observability.sql` | no row-level security: holds no tenant data, and the runtime roles are granted it directly |
| `tenant_config_versions` | `0010_tenant_config.sql` | forced row-level security |
| `tenant_idps` | `0011_identity.sql` | forced row-level security |
| `tenant_users` | `0011_identity.sql` | forced row-level security |
| `auth_sessions` | `0011_identity.sql` | forced row-level security |
| `auth_login_states` | `0011_identity.sql` | forced row-level security |
| `saml_assertions_seen` | `0011_identity.sql` | forced row-level security |
| `service_tokens` | `0011_identity.sql` | forced row-level security |
| `repo_access` | `0012_repo_access.sql` | forced row-level security |
| `repo_grants` | `0012_repo_access.sql` | forced row-level security |
| `scm_permissions` | `0012_repo_access.sql` | forced row-level security |
| `scm_sync_status` | `0012_repo_access.sql` | forced row-level security |
| `evidence_records` | `0014_evidence.sql` | forced row-level security |
| `audit_anchors` | `0017_releases_incidents_retention.sql` | forced row-level security |

Outside the database there is one volume, `/var/lib/tia/repos`: a bare git clone per registered repository and each uploaded folder or zip turned into a git repository. Code there is read as bytes and parsed; it is never imported, executed or built.

## 6. Deployment

| File | What it is |
|---|---|
| [`deploy/Dockerfile`](../deploy/Dockerfile) | the one image: Python, TIA and its parsers, non-root, health check |
| [`deploy/README.md`](../deploy/README.md) | how to deploy |
| [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) | one machine: PostgreSQL on an internal network, the migration, the API and a worker |
| [`deploy/helm/tia/Chart.yaml`](../deploy/helm/tia/Chart.yaml) | the Helm chart |
| [`deploy/helm/tia/templates/_helpers.tpl`](../deploy/helm/tia/templates/_helpers.tpl) | shared labels and pod security settings |
| [`deploy/helm/tia/templates/api.yaml`](../deploy/helm/tia/templates/api.yaml) | API Deployment and Service |
| [`deploy/helm/tia/templates/platform.yaml`](../deploy/helm/tia/templates/platform.yaml) | migration hook Job, repos volume claim, Ingress, NetworkPolicy |
| [`deploy/helm/tia/templates/worker.yaml`](../deploy/helm/tia/templates/worker.yaml) | worker Deployment |
| [`deploy/helm/tia/values-dedicated.yaml`](../deploy/helm/tia/values-dedicated.yaml) | values for a dedicated single-tenant deployment |
| [`deploy/helm/tia/values-region-eu.yaml`](../deploy/helm/tia/values-region-eu.yaml) | values for an EU data-residency region |
| [`deploy/helm/tia/values.yaml`](../deploy/helm/tia/values.yaml) | default Helm values |
| [`deploy/terraform/azure/README.md`](../deploy/terraform/azure/README.md) | how to apply the Azure Terraform |
| [`deploy/terraform/azure/main.tf`](../deploy/terraform/azure/main.tf) | Azure resources (Terraform) |
| [`deploy/terraform/azure/outputs.tf`](../deploy/terraform/azure/outputs.tf) | Terraform outputs |
| [`deploy/terraform/azure/variables.tf`](../deploy/terraform/azure/variables.tf) | Terraform inputs |

Verification: the Compose stack was built and run on Docker Desktop (migration, server, worker, tenant creation, an analysis, upgrade, backup and restore). The Helm chart and the Azure Terraform are written but not deployed to a real cluster or subscription.

## 7. Design versus build

| Design ([ARCHITECTURE.md](ARCHITECTURE.md)) | As built | Status |
|---|---|---|
| API and backend: Python + FastAPI | Python 3, FastAPI app in `tia/api.py` served by uvicorn (`tia/server.py`) | Built as designed |
| Web UI: React | One HTML page with plain JavaScript (`tia/web/index.html`) and Cytoscape.js vendored under `tia/web/vendor/`; no build step and no CDN, so it works air-gapped. The shareable page (`tia/reportpage.py`) is server-rendered with no script at all. | Built differently |
| Graph store: PostgreSQL first | Facts cached per git blob in PostgreSQL; the typed graph is built in memory for each analysis (`tia/graph.py`) and its versions recorded in PostgreSQL (`tia/graphstore.py`). No separate graph database. | Built as designed |
| Evidence store: S3-compatible object storage | Evidence records and snapshots are PostgreSQL tables; source code is kept as bare git clones and uploads on one volume. There is no object store. | Built differently |
| Background jobs: PostgreSQL-backed queue | `jobs` table with leases, retries, per-tenant caps and `FOR UPDATE SKIP LOCKED` (`tia/jobs.py`, `tia/worker.py`) | Built as designed |
| Graph writer is the only writer of the graph | The graph is a pure function of the cached facts and configuration; a version is written once and never edited | Built as designed |
| Impact engine never calls external systems | `tia/impact.py` reads only the stores and the local git clones; connectors sit in a higher layer and the layer rule below is checked on every test run | Built as designed |
| API gateway with SSO, tenant resolution and rate limits | No separate gateway: sign-in, tenant binding and rate limits run inside the API (`authapi`, `gate`, `ratelimit`); TLS ends at the ingress or reverse proxy | Built differently |
| Tenant admin service | A module and command (`tia/tenants.py`, `python -m tia.tenants`) plus admin API routes, not a separate service | Built differently |
| GitHub App + webhooks | Built (`tia/githubapp.py`, `tia/webhooks.py`); the PR comment verified live on github.com, the App installation flow against a stand-in only | Verified (simulated) |
| GitLab, Bitbucket, Azure DevOps, ServiceNow, customer KMS | Built; verified against stand-ins, not the real services | Verified (simulated) |
| Containers, customer-controlled deployment | One image (`deploy/Dockerfile`), Docker Compose (run on Docker Desktop), Helm chart, Terraform for Azure (written, not applied) | Built |
| Optional LLM summary, never inventing edges | No LLM or AI library is used anywhere in `tia/`; every result comes from parsing and fixed rules | Built as designed |

## 8. Keeping this document true

```
python tools/gen_architecture.py            # rewrite docs/ARCHITECTURE-AS-BUILT.md and .html
python tools/gen_architecture.py --check    # what the test suite runs
```

Adding a module means placing it in a layer in `tools/gen_architecture.py`; the generator refuses to run until it is placed, and refuses an import that breaks the layer rule unless the reason is recorded there.
