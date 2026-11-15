# TIA end-to-end technical demo: code walkthrough and UI walkthrough

**Audience:** the presenters of a customer evaluation, and the customer's engineers, architects and testers watching. Assumes you read Python and SQL.

**Length:** about 100 minutes in three modules (A: how it works, 45 min; B: the screens, 35 min; C: agents, operations and questions, 20 min). Every module stands alone.

**Companion documents:** [DEMO-DATA.md](DEMO-DATA.md) (exact inputs and expected numbers for every screen), [CASE-STUDY.md](CASE-STUDY.md) (why the three tester use cases exist), [GRAPH-STORE.md](GRAPH-STORE.md), [SECURITY.md](SECURITY.md), [OPERATIONS.md](OPERATIONS.md).

> **Status words used here.** *Built*: the code exists and has automated tests. *Verified (simulated)*: exercised against stand-ins for external systems, not the real ones. *Verified (live)*: exercised against the real system. Section 9 lists what is not verified. Say these words to the customer; do not round them up.

---

## 0. Before the customer arrives (10 minutes)

```
# terminal 1: TIA on an embedded PostgreSQL, with the 3-repository demo landscape
python tools/run_dev.py --demo --landscape lanes --data demo-data/lanes
# terminal 2: the evidence a CI pipeline, a deploy tool and an incident system would send
python tools/demo_seed.py
```

Open `http://127.0.0.1:8080` and press **Ctrl+F5** (an old cached page is the most common demo failure).

| Check | You should see |
|---|---|
| `curl -s -o /dev/null -w "%{http_code}" localhost:8080/readyz` | `200` (the body lists the database, schema and workspace checks) |
| Section 1 table | `customer-portal`, `payments-service` (7 branches), `statement-service` |
| `demo_seed.py` output | releases `2026.10`, `2026.11`; test results `{"passed": 3, "failed": 1}`; 4 deployments; 3 runtime edges; incident `INC-1001` |

| If you see | Cause | Fix |
|---|---|---|
| a single box on the graph | one repository selected in the filter | clear the *Repositories* selection |
| other repositories, or doubled evidence | an old data folder, or the seed run twice | stop, delete `demo-data/lanes`, repeat both commands |
| 4 findings where this document says 5 | the seed was not run (the 5th is `mobile-app`, known only from runtime traffic) | run `demo_seed.py` |

This is **development mode**: no sign-in, you act as administrator, local repository paths are allowed. Production uses OIDC or SAML sign-in plus scoped service tokens (module C).

---

## 1. The example used throughout

Three small services, one shared API.

```
 customer-portal ──GET /portal/accounts/{id}/payments──┐            Angular page /accounts/:id/payments
   (screen)                                            ▼            shows EVERY payment      <-- no rule
                                         payments-service  GET /accounts/{account_id}/payments -> List[Payment]
 statement-service ──GET /statements/{id}/pdf──────────┘            React page /statements; PDF drops disputed payments
   (PDF)                                                             <-- rule: not p['disputed']
```

| Repository | What it is | Relevant code |
|---|---|---|
| `payments-service` | FastAPI provider. `Payment` has `payment_id, status, amount, disputed`. `PaymentRequest` has rules: `amount` > 0 and ≤ 10000, `currency` in EUR/USD/GBP, `reference` 1-18 chars of `A-Z0-9`. Answers 404 and 409. | `app.py`, `tests/test_payments.py` (4 tests) |
| `statement-service` | Calls payments-service, **filters out disputed payments**, renders a PDF | `client.py:9`, `app.py:10` (`[p for p in payments if not p["disputed"]]`), React page, written cases `TC-201..204` |
| `customer-portal` | Calls payments-service, shows **everything** | `client.py:9` and `:13`, Angular page, written cases `TC-101..105` |

Both consumers reach the provider through `PAYMENTS_BASE_URL`, declared in each repository's `tia-services.yaml` as `config_keys: {PAYMENTS_BASE_URL: payments-service}`.

**The change under review (a "pull request").** Each branch of `payments-service` stands in for a PR:

| Branch | What the developer did | Expected result |
|---|---|---|
| `contract` | removed `disputed` from the `Payment` response model | **5 high**, breaking, all consumers |
| `behaviour` | changed the sort order inside `ordered()` | 5 medium, 1 low, behaviour change |
| `signature_unfixed` | added a required parameter to `ordered()`, did not update its caller | 5 high, 1 low |
| `signature` | the same, caller updated | 5 medium, 1 low |
| `move_rule` | moved the disputed filter into the shared endpoint | 5 medium |
| `docstring` | changed a comment | **0 findings**: "nothing to retest" |

The worked example for module A is **`contract`**: the developer removes `disputed: bool` from `Payment`. statement-service reads `p["disputed"]` for every row, so a real consumer breaks. IDs and SHAs differ on each run of the demo builder; the shapes below do not.

---

## 2. Architecture in one page

```
 Browser / CI / webhook / agent
        │  HTTPS (session cookie + CSRF, or Bearer token)
        ▼
 ┌─ API process (FastAPI, tia/api.py) ────────────────────────────┐      ┌─ Worker process (tia/worker.py) ─┐
 │ auth + RBAC middleware (authapi.py, rbac.py, identity.py)      │      │ claim job (FOR UPDATE SKIP LOCKED)│
 │ routes ► analysis_service.resolve() ► impact.analyze()         │      │ lease 300 s, heartbeat, retry     │
 │ tester views, graph queries, MCP, Knowledge Fabric door        │      │ same analyze() code as the API    │
 └──────────────┬─────────────────────────────────────────────────┘      └───────────────┬──────────────────┘
                │  every query inside a transaction that sets tia.tenant_id               │
                ▼                                                                          ▼
        PostgreSQL 16 (shared schema, FORCED row-level security)        local git clones (workspace dir): read-only, never executed
```

**The pipeline inside `analyze()`, and the data structure handed from each step to the next:**

| # | Step | Code | Input | Output handed on |
|---|---|---|---|---|
| 0 | Request | `api.py::create`, `analysis_service.py::resolve` | UI/API JSON | `AnalysisRequest` (exact SHAs, scope, limits) |
| 1 | Git | `gitutil.py` | branch names | SHAs, changed files, blob ids, file bytes |
| 2 | Extract | `extraction.py`, `extract_*.py`, `lang/*` | files at a SHA | `RepoExtract` per repository (cached per file) |
| 3 | Graph | `resolve.py`, `graph.py` | all `RepoExtract`s | `Graph` (typed nodes, evidence-tagged edges) |
| 4 | Diff | `impact.py::_analyze` step 2, `contracts.py`, `testscope.py::classify_function` | base and head `RepoExtract` | `seeds`, `changed_components` |
| 5 | Traverse | `impact.py::bfs` | `Graph` + seeds | `reached` (node → path, edges, seeds) |
| 6 | Findings | `severity.py`, `enrich.py`, `policy.py` | `reached` + config + evidence | `findings`, `unknowns` |
| 7 | Tester views | `testscope.py`, `coverage.py`, `automation.py`, `testerpack.py` | extracts, graph, `reached` | test scope, coverage gate, matrices, locators |
| 8 | Report + audit | `report.py`, `store.py` | all of the above | stored report, snapshot id, audit entry |

**There is no AI in any step.** Parsers produce facts; fixed, versioned rules turn facts into severities. Same commits + same configuration + same evidence give the same report; module A step 8 proves it.

For the whole system (every module in its layer, the layer rule the code is checked against, every table and its tenant isolation, deployment), open [ARCHITECTURE-AS-BUILT.html](ARCHITECTURE-AS-BUILT.html), generated from the code.

---

## MODULE A: How it works, layer by layer (45 minutes)

Run the example once so everything below is on screen: section 3 of the page, **Repository** `payments-service`, **Head** `contract`, **Base** `main`, **Run analysis**. Or from a terminal:

```
curl -s -X POST localhost:8080/api/v1/impact-analyses -H 'content-type: application/json' \
     -d '{"target":"payments-service","head":"contract","base":"main"}'
```

### A0. The request (`api.py`, `analysis_service.py`)

1. `POST /api/v1/impact-analyses` arrives. The middleware in `authapi.py` classifies the route with `rbac.classify(method, template)`. **A route with no permission entry is refused (403 "route is not classified")**, so a forgotten endpoint is closed, not open. This one needs `analyze`.
2. `require_repo(target)` answers 404 for a repository the caller cannot read, exactly as if it did not exist. `within_plan("analyses")` enforces the tenant's plan limits.
3. `analysis_service.resolve()` turns "head=contract" into a **concrete request**: SHAs for base and head, the repositories in scope, and the limits. Limits are *ceilings*: `max_depth = min(int(p.get("max_depth") or cfg.max_depth), cfg.max_depth)`; a caller can ask for less, never more than the tenant's configuration.
4. Synchronous by default. With `?async=true` the request is queued instead: `jobs.enqueue(tenant, "impact_analysis", payload, job_key, group_key)`. `job_key` de-duplicates (`on conflict (tenant_id, key) do nothing`); `group_key` is the pull request, so a newer push **supersedes** older queued jobs for the same PR. A worker runs `Worker.run_once()`: `reap_expired()` re-queues jobs whose lease expired, `claim()` takes a job with `FOR UPDATE SKIP LOCKED` (so many workers never take the same job) and a per-tenant cap, runs the handler under a 300 s lease with heartbeats, and on failure retries with exponential backoff up to `max_attempts` (`PermanentError` skips the retries).
5. **The PR path.** GitHub (and GitLab, Bitbucket, Azure DevOps) `pull_request` webhooks arrive at `/api/v1/webhooks/*`. The signature is verified (`verify_signature`) and the repository matched before anything is queued; the worker then runs the same `analyze()` and publishes the PR comment and an advisory check run. *Verified (live) on github.com for the comment; GitLab, Bitbucket and ADO against stand-ins.*

**Say:** "One code path, two doors. A tester clicking Run and a webhook both end in `analyze()`."

### A1. Reading Git (`gitutil.py`)

```python
def list_tree(repo_path, rev):          # git ls-tree -r -z <rev>
    ...   # -> {path: blob_sha} for regular files (symlinks and submodules skipped)
def read_blobs(repo_path, shas):        # ONE git process: git cat-file --batch
```

For the example: `resolve_rev` gives `main = 80d082d…`, `contract = 482b101…`; `changed_files(main, contract)` returns `[('M', 'app.py')]`; the tree is `app.py, tests/test_payments.py, tia-services.yaml`.

**Two properties worth stating.** (1) The **blob id is the content identity**: it is the cache key in A2, so unchanged files are never re-parsed. (2) **Customer code is only ever read as bytes and parsed. It is never imported, run or built.** A hostile repository cannot execute anything inside TIA.

### A2. Extraction: code becomes facts (`extraction.py`, `extract_python.py`)

`extraction._extract_commit()`:

```python
keys = {p: _file_key(repo, service, strip, tree[p], p, exclude) for p in py}
#   key = repo | service | strip_prefixes | exclude_patterns | EXTRACTOR_VERSION | blob_sha | path
have = store.get_file_extracts(list(keys.values()))     # one query
missing = [p for p in py if keys[p] not in have]         # only these are parsed
```

The example returned `{'files': 2, 'reused': 0, 'parsed': 2}`; run it again and it is `reused: 2, parsed: 0`. A commit that changes one file re-parses one file, and **the incremental result is tested to equal a full rescan** (`tests/test_incremental.py`). `EXTRACTOR_VERSION` is `py-7+java-7+data-2+go-1+cs-2+kt-1+ts-1`: upgrading any extractor changes every key, so stale facts cannot survive an upgrade.

Parsing: Python with the standard `ast` module (`_FileVisitor`); Java with tree-sitter (`extract_java.py`); Go, C#, Kotlin and TypeScript through an adapter registry (`adapters.py`, also open to third-party adapters); Angular, React, Vue and HTML templates in `extract_ui.py`; ORM entities, SQL and Kafka in `extract_orm.py`, `sqllineage.py`, `events.py`, `streams.py`. The result is one `RepoExtract` per repository (`models.py`). The real records for the example:

**An endpoint** (the provider's door):
```json
{"id": "payments-service:GET /accounts/{}/payments", "method": "GET", "path": "/accounts/{account_id}/payments",
 "norm_path": "/accounts/{}/payments", "handler_id": "payments-service:app.py::list_payments", "file": "app.py", "line": 48,
 "response_schema": "Payment", "response_shape": "list",
 "params": [{"name": "account_id", "in": "path", "required": true},
            {"name": "status", "in": "query", "type": "Optional[PaymentStatus]", "required": false}]}
```
`normalize_path` turns `{account_id}`, `${id}` and `:id` into `{}`, so a provider's `/accounts/{account_id}/payments` and a consumer's f-string `/accounts/{account_id}/payments` meet at `/accounts/{}/payments`.

**A schema, before and after the change:**
```
Payment @main      : payment_id:str, status:str, amount:float, disputed:bool     (all required)
Payment @contract  : payment_id:str, status:str, amount:float
```
**Field constraints read from `Field(...)`:** `amount: {exclusive_minimum: 0, maximum: 10000}`, `reference: {min_length: 1, max_length: 18, pattern: "^[A-Z0-9]+$"}`. These later become test aspects and generated test values.

**A function with two fingerprints:**
```json
{"id": "payments-service:app.py::ordered", "hash": "b48df46b01ebd275", "body_hash": "94f1befefafab504",
 "signature": {"params": [{"name": "rows", "kind": "positional", "default": false}], "returns": null}, "calls": ["sorted"]}
```
`hash` covers the whole function including its docstring; `body_hash` excludes the docstring. Equal `body_hash` means a documentation-only change; that is how the `docstring` branch yields zero findings.

**A test**, as facts: `test_create_payment_requires_a_reference` has `http_calls: [["POST","/accounts/123/payments"]]`, `statuses: [422]`, and `bodies` showing it sends only `amount` and `currency`. The test itself is not run; its calls and assertions are what is recorded.

**An outbound call** (the consumer's side), `statement-service/client.py:9`:
```json
{"id": "statement-service:client.py:9", "function_id": "statement-service:client.py::fetch_payments", "method": "GET",
 "norm_path": "/accounts/{}/payments", "base_kind": "config_key", "base_value": "PAYMENTS_BASE_URL"}
```
The extractor read `requests.get(f"{PAYMENTS_BASE_URL}/accounts/{account_id}/payments")` and kept the *key* `PAYMENTS_BASE_URL` instead of guessing a host.

**A business rule**, found by `extract_python.item_filters`:
```python
# for each function: comprehension conditions, filter(lambda), and loops that skip or collect items
for gen in n.generators:
    for cond in gen.ifs:
        add(cond, gen.target.id, n.lineno)          # only conditions on the ITEM's fields count
```
In `statement_rows` (line 10) it records `{"expr": "not p['disputed']", "fields": ["disputed"]}`. This is the raw material of the **rule gap** in A7. `customer-portal`'s equivalent function has no such filter.

**The service map** from `tia-services.yaml` carries `service`, `owner`, `criticality`, `config_keys` and `entry_points` (the human label "Monthly statement PDF" for `GET /statements/{account_id}/pdf`).

**Anything unparseable** goes to `files_failed` with the error, and later to the report's *Unknowns*; it is never silently skipped.

### A3. Resolution and the graph (`resolve.py`, `graph.py`)

`resolve_consumers()` is **the only place a cross-repository relationship is created**, and only through enumerated strategies:

| Strategy | Meaning | Evidence status |
|---|---|---|
| R1 | generated or shared client package mapped to a provider | `source_confirmed` |
| R2 | fully resolved HTTP call: base from a config key or host, plus method and path | `source_confirmed` |
| R3 | declared consumer contract (pact-like JSON) | `contract_confirmed` |
| R5 | unique path match across services, no base known | **`inferred`** (`INF-PATH-UNIQUE`) |
| runtime | seen in OpenTelemetry traces | `runtime_observed` |

```python
elif cs.norm_path is not None and cs.base_kind:                    # R2
    prov = smap["config_keys"].get(cs.base_value)                  # PAYMENTS_BASE_URL -> payments-service
    cands = [e for e in endpoints_by_service[prov] if e.norm_path == cs.norm_path and (cs.method is None or e.method == cs.method)]
    if len(cands) == 1:
        edge = Edge(cs.id, "CONSUMES_API", cands[0].id, SOURCE_CONFIRMED, "R2", None, ev(cs))
    elif cands:  reason = f"call matches {len(cands)} endpoints in {prov}"        # ambiguous: no edge
```

**Matching names alone never creates an edge.** A call that cannot be resolved goes to `graph.unresolved` with a reason. In the demo, 7 outbound call sites, **3 resolved by R2** (rate 0.429); the rest are front-end calls with relative URLs, for example `payments.component.ts:25`, "base URL not present in call". They appear as grey dotted edges on the graph and in *Unknowns*. TIA says what it could not see.

The three real `CONSUMES_API` edges, each with evidence:
```
statement-service:client.py:9   -> payments-service:GET /accounts/{}/payments   R2  source_confirmed
customer-portal:client.py:9     -> payments-service:GET /accounts/{}/payments   R2  source_confirmed
customer-portal:client.py:13    -> payments-service:POST /accounts/{}/payments  R2  source_confirmed
```

`build_graph()` then adds the structural relationships. For the demo landscape: **49 nodes, 40 edges** (`CALLS` 11, `CONSUMES_API` 3, `EXPOSES` 5, `HANDLES` 5, `ISSUES` 7, `TESTS` 6, `USES_SCHEMA` 3). `TESTS` edges are *static associations* (a test's HTTP call matches an endpoint, or it imports and calls a function); a match by file naming alone is `inferred`. A call whose name matches several functions in one repository becomes one **inferred** `CALLS` edge per candidate (`INF-CALL-AMBIGUOUS`), not a guess.

**The graph answers "who is affected" by storing every edge in both directions of use:**
```python
_IMPACT_DST = ("CALLS", "INHERITS", "USES_SCHEMA", "CONSUMES_API", "CONSUMES_EVENT", "ISSUES", "TESTS", "MAPS_TO", "MAPS_COLUMN", "READS", "WRITES")
def add_edge(self, e):
    if e.rel in ("HANDLES", "PUBLISHES"):   self._impact[e.src].append((e.dst, e))      # handler change -> endpoint
    elif e.rel in _IMPACT_DST:              self._impact[e.dst].append((e.src, e))      # callee/provider change -> caller/consumer
```
`impacted_by("ordered")` returns `list_payments` (a `CALLS` edge, `source_confirmed`). The graph is **content-addressed** (`graphstore.get_or_build`): the same extracts and configuration give the same graph id, so it is built once and reused.

### A4. Detecting the change (`impact.py`, `contracts.py`, `testscope.py`)

`_analyze()` extracts base and head separately, then diffs three kinds of thing:

1. **Contracts** (`contracts.diff_contracts`): endpoints, schemas, parameters, events, with a compatibility rule per kind of change (tenant-overridable). Our change:
```json
{"type": "api_schema", "name": "payments-service:schema:Payment", "change": "schema_field_removed",
 "breaking": true, "effect": "breaking", "detail": "Payment.disputed removed", "file": "app.py", "line": 12}
```
2. **Data** (`diff_data`): migrations, tables, columns, stored procedures.
3. **Functions**: compare `hash`; `classify_function()` then splits the change:

| Result | Meaning | Example branch |
|---|---|---|
| `no_functional_change` | same `body_hash`: documentation only. **Skipped: reaches nothing** | `docstring` |
| `breaking` | a signature change that unchanged callers no longer match | `signature_unfixed` |
| `behaviour_change` | any other body change, e.g. `('behaviour_change', 'logic changed (same signature)')` | `behaviour` |

Each becomes a **seed**: `{"node", "kind": breaking|nonbreaking|unclassified, "effect", "label"}`. The seed for our change is the schema node `payments-service:schema:Payment`. **A file that parses at base but not at head is left out of the comparison and reported as an unknown**, instead of being counted as deleted (which would call every consumer broken).

### A5. Traversal (`impact.py`, the `bfs` function)

```python
def bfs(allow_inferred):
    for si, s in enumerate(seeds):
        dq = deque([(s["node"], [s["node"]], [])])
        while dq:
            node, path, edges = dq.popleft()
            if len(edges) >= req.max_depth: ... truncated = True; continue
            if g.nodes[node]["type"] == "test": continue                  # a test is an answer, not a path
            for nxt, e in g.impacted_by(node):
                if nxt in visited or (e.status == INFERRED and not allow_inferred): continue
                ...  reached[nxt] = {"path": path + [nxt], "edges": edges + [e], "seeds": [si]}
bfs(False)             # confirmed edges first
if not truncated: bfs(True)   # then inferred ones, for what is still unreached
```

For our seed it walks **schema → (USES_SCHEMA) endpoint → (CONSUMES_API) call site** in each consumer, then on to their endpoints: **14 nodes reached, depth ≤ 8, not truncated**. Bounds are explicit and reported: `max_depth` 8, `max_results` 200, `time_budget_seconds` 60; hitting one sets `traversal.truncated` and the report status becomes `partial`.

### A6. Findings and severity (`severity.py`, `enrich.py`, `policy.py`)

Each reached call site or endpoint outside the changed repository becomes a finding. The severity is **one pure function of four inputs**:

```python
def compute(change_kind, status, cross_repo, criticality, policy=None):
    if change_kind == "unclassified": return "unknown", "SEV-UNCLASSIFIED"
    if not cross_repo:                return "low", "SEV-LOCAL"
    if change_kind == "breaking":     sev, rule = ("critical","SEV-BREAKING-CRITICAL") if critical else ("high","SEV-BREAKING-CONFIRMED")
    else:                             sev, rule = ("high","SEV-NONBREAKING-CRITICAL")   if critical else ("medium","SEV-NONBREAKING-CONFIRMED")
    if status not in CONFIRMED:       cap at policy.inferred_cap (default medium) -> rule SEV-INFERRED-CAP / SEV-STALE-CAP
```

*Criticality alone never produces Critical.* A path's status is **the weakest link** (`_path_status`): one inferred edge makes the whole path `inferred`, capped at medium.

The real finding **F1** for our change (trimmed):
```json
{"id": "F1", "component": "customer-portal", "node": "customer-portal:client.py:9", "node_type": "callsite", "impact_type": "contract",
 "severity": "high", "severity_rule": "SEV-BREAKING-CONFIRMED", "evidence_status": "source_confirmed", "resolution_strategy": "R2",
 "reason": "customer-portal depends on the changed provider through a fully resolved HTTP call at customer-portal:client.py:9. Change: Payment.disputed removed.",
 "path": ["payments-service:schema:Payment", "payments-service:GET /accounts/{}/payments", "customer-portal:client.py:9"],
 "path_edges": [{"rel": "USES_SCHEMA", "status": "source_confirmed", "strategy": "extract"},
                {"rel": "CONSUMES_API", "status": "source_confirmed", "strategy": "R2"}],
 "tests": [{"name": "test_portal_payments", "association": "test-http-call", "result": "no_ci_results", "states": ["exists", "associated"]}],
 "required_verification": ["Run: test_portal_payments (tests/test_portal.py)", "Verify compatibility with the deployed consumer version"],
 "explanation": "A breaking change reaches a downstream component through confirmed evidence.", "rule_id": "SEV-BREAKING-CONFIRMED"}
```

Every field is traceable: the rule id names the rule that produced the severity, `evidence.file:line` is the source line, `path` is the proof. **A tenant can change a severity** (`severity.overrides`), but the finding then records `{"rule", "default", "configured"}`: *overrides are never silent*. **Show it:** import the owner catalog (module B, section 6) marking `statement-service` critical, re-run `contract`, and its two findings become **critical** (`SEV-BREAKING-CRITICAL`) while `customer-portal` stays high.

`enrich.py` adds, from ingested evidence: CI results per test (`passed` / `failed` / `no_ci_results`), coverage, deployments, and **runtime-only consumers** (that is how `mobile-app` appears with `runtime_observed` and "outside the analysed repositories"). `policy.py` evaluates the tenant's release-policy rules (`pass`/`fail`/`requires_review`/`not_evaluated`); the report is advisory and never blocks a merge.

### A7. The three tester use cases (`testscope.py`, `coverage.py`, `automation.py`, `extract_ui.py`, `testerpack.py`)

All three read the same `RepoExtract`s and `Graph`; none needs a change (except to scope the result).

**UC-1, what to retest.** `testscope.test_scope()` projects reached nodes onto **entry points** (endpoints, labelled from `tia-services.yaml`). For `contract`: 3 entry points (Payments screen, Monthly statement PDF, the shared `GET /accounts/{account_id}/payments`), 4 linked tests, all `breaking`. `rule_gaps()` compares what each consumer of a shared endpoint does with its response, using the `item_filters` facts from A2:

```json
{"shared_entry_point": "GET /accounts/{account_id}/payments", "rule": {"fields": ["disputed"], "expr": "not p['disputed']"},
 "applied_by": [{"entry_point": "GET /statements/{account_id}/pdf", "function": "statement_rows", "function_line": 10}],
 "missing_in": [{"entry_point": "GET /portal/accounts/{account_id}/payments", "label": "Payments screen"}]}
```
That is the stakeholder's "rule implemented in one lane only" defect, found from code. A rule counts only if it tests fields of the shared endpoint's declared response schema.

**UC-2, are my tests enough.** `coverage.api_aspects()` builds a checklist from the extracted constraints: `amount` > 0, ≤ 10000 yields *missing input* and *invalid value*; the `currency` enum yields *allowed values*; each `HTTPException(409)` found by walking the handler yields an *error status*. Automated tests are matched by what they do (`_auto_covers`: which endpoint they call, which body keys they send or omit, which status they assert). Written cases (`testcases/*.csv|yaml|json`) are matched by rules over their text (`_written_covers`: the entry point must match; the steps must name the field and use words like "empty" or "too long"; the expected result must say refused or name the status). Demo: **35 aspects, 13 covered, 3 partial, 19 missing, 41.4%**. *"Covered" means a test addresses the aspect; it does not mean the test passes.*

**UC-3, automation inventory.** `automation.values()` turns rules into boundary values: `amount` (gt 0, le 10000) → valid `0.01`, `10000`; invalid `0`, `10000.01`, `abc`, *(left out)*. `reference` → valid `A` and 18×`A`; invalid empty, 19×`A`, `!@#`. Demo: 5 endpoints, 14 inputs, **29 generated cases**. UI side (`extract_ui.py`): Angular, React, Vue and HTML pages; each element gets candidate locators in a fixed order (test id such as `data-testid`, `id`, `name`, Angular `formControlName`, `aria-label`, label, visible text, placeholder, and last of all position) and the first one that is **unique on the page and not positional** is chosen: the two identical *Export* buttons have no such locator and are flagged `position (fragile)`. Every locator is given as CSS, XPath and Playwright. Dropdown values come from the template or a literal list; runtime-loaded values are reported *dynamic*. 2 pages, 14 elements.

**The tester pack** (`testerpack.build_pack`) packages the three for **one change**: only the entry points the change reaches and the pages that call them, with a coverage **gate** per entry point (`covered` / `gaps` / `not_evaluated`). For `contract`: *gaps*: 2 covered, 3 with gaps, 1 not evaluated (`mobile-app`, which is not in the analysed code and so cannot be measured: said, never counted as covered). The pack describes the changed repository **at its proposed revision**.

### A8. The report, the snapshot, the audit chain, reproducibility (`report.py`, `store.py`, `analysis_service.py`)

The report (`status`, `summary`, `change`, `findings`, `test_scope`, `unknowns`, `coverage`, `policy_results`, plus derived `structural_risk`, `communities`, `architecture_diagram`) is stored with an **evidence snapshot id**:

```python
snap_src = sorted((r, e.commit, e.extractor_version) for r, e in extracts.items()) + [("head", target, head_sha)]
snap_src += [("graph", graph_version), ("stale", ...), ("freshness", ...), ("evidence", sorted((k, sorted(ids)) ...))]
snapshot_id = "snap-" + sha256(repr((snap_src, cfg.ident(CONFIG_VERSION))))[:12]
```
Commits, extractor versions, graph version, evidence record ids and configuration all feed it.

**Reproducibility, live:** `POST /api/v1/impact-analyses/{id}/reproduce` runs `analysis_service.reproduce()`: it re-runs the analysis with `persist=False` on the *pinned* revisions, configuration and evidence, and compares: `{"identical": true, "versions_match": true}`. It refuses if a revision is gone ("revision … is no longer available") rather than answering from something else. This is the answer to "how do I trust it": the same inputs give the same report, and you can check.

**The audit log is a hash chain** (`store.audit`): every security-relevant event (analysis requested, evidence ingested, config changed, owner approval, tenant created, purge…) is appended as
`hash = HMAC-SHA256(audit_key, prev_hash ‖ seq ‖ ts ‖ event ‖ payload)`. `GET /api/v1/audit/verify` re-walks the chain and recomputes every hash; editing or deleting a row breaks it at that sequence number. Retention purges move an anchor so a legitimately purged chain still verifies. **Show it:** `curl localhost:8080/api/v1/audit/verify` → `{"ok": true, ...}`.

**Multi-tenancy** (`migrations/0001_init.sql`, `db.py`): every tenant-owned table has `tenant_id` and **forced row-level security**:
```sql
alter table %I enable row level security;  alter table %I force row level security;
create policy tenant_isolation on %I
    using      (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);
```
`Database.tx(tenant)` runs `select set_config('tia.tenant_id', …, true)` (transaction-local) on every connection use. The runtime roles are created `nosuperuser nobypassrls`, so even a coding mistake cannot read another tenant's rows; **a session with no tenant sees no rows**. Within a tenant, `report.redact()` removes findings, tests, paths and (since the structural overlays) services and files of repositories the reader may not read; `X-TIA-Readable-Repos` can only narrow, never widen. Tests for this: `test_tenants`, `test_access`, `test_acceptance` (AC-016).

### A9. How the page is built (`tia/web/index.html`)

One HTML file, no build step, vanilla JavaScript, Cytoscape.js vendored under `/vendor` (no CDN: it works air-gapped). `api()` / `tfetch()` send the session cookie and an `X-TIA-CSRF` header on every state-changing call. `show(rep, comment)` renders section 4 from the report JSON; `findingRows` and `whyHtml` build the Findings table (the "Why" is the report's reason split at `. Change: ` into labelled parts, with the path drawn as typed chips); `showPathGraph` draws one row's path with Cytoscape; `renderStructure` the architecture block; `renderPack` the tester pack; `askGraph` the direct questions. One map, `TYPE_COLOR`, colours every component type in the graphs, the tables and the legend. The shareable page `/analyses/<id>` is separate: **server-rendered HTML with no script at all** (`reportpage.py`), strict CSP, so it can be opened from a PR comment or printed; its graphs are inline SVG.

**Module A in one sentence for the customer:** "We parse your code into typed facts, connect services only through proven links and label everything we could not prove, diff the change, walk the graph with explicit bounds, and score with a fixed rule table: the same inputs always give the same report, and every claim carries the file and line it came from."

---

## MODULE B: The UI demo, step by step (35 minutes)

Use the seeded server from section 0. Numbers are measured and enforced by `tests/test_demo_seed.py`. The right-hand column is what to say about the machinery.

| # | Do | Expect | Say (the machinery) |
|---|---|---|---|
| **1. Repositories** | Show the table | 3 repositories; `payments-service` has 7 branches | "Registered from a local clone here; in production from GitHub, GitLab, Bitbucket, Azure DevOps, a zip or a folder upload. `GET /api/v1/repositories`." |
| **2. Graph** | Level **Services**, nothing selected, **Load graph** | 3 boxes, 2 arrows | "Aggregated `CONSUMES_API` edges. Edge colour is the evidence class." |
| | Level **Endpoints & schemas** | 15 nodes, 14 edges | "Arrows land on the exact door, `GET /accounts/{}/payments`. The grey dotted edge is the Angular call TIA refused to guess." |
| | Level **Functions, call sites, tests** | 49 nodes, 40 edges | "Types are colour-coded (legend above the graph). Click a node for file and line." |
| | **Ask the graph**: *Who depends on it?* `GET /accounts/{account_id}/payments` | services `customer-portal, statement-service`; 2 entry points; tests attached; **Limits** listed | "No change needed. Same answer for agents: `GET /api/v1/graph/query`." |
| | *Who depends on it?* `fetch_payments` | "2 components are called fetch_payments … TIA does not guess": choose one | "Ambiguity is never resolved silently." |
| **3. Analyse** | Repository `payments-service`, Head **`behaviour`**, Base `main`, **Run analysis** | status **complete with gaps**; 6 findings (5 medium, 1 low) | "A behaviour change: same signature, different logic. Medium, because the links are confirmed but the change is not breaking." |
| | Head **`contract`** | **5 high**, rule `SEV-BREAKING-CONFIRMED` | "A removed field is breaking, and both consumers are reached through R2-confirmed calls." |
| | Head **`docstring`** | **0 findings**; "nothing to retest" | "`body_hash` unchanged. A comment costs nobody a retest: no noise." |
| **4. Report** (use `contract`) | Findings table: read one row left to right | 7 columns: Severity, Component, Evidence, Why, What to do, Tests, Graph | "Why is split into *What changed / Why this is affected / Path from the change / Evidence / Limits*. Every row ends in a file and line." |
| | Press **Show** in a row's Graph column | that row's path drawn under it: red-ringed changed node, severity-filled affected node, labelled links | "The proof of the finding, as a picture." |
| | Filters: Severity `high` | 5 rows; Reach `direct` 3; Service `statement-service` 2 | "The server filters (`GET …/findings?…`); page and API agree." |
| | **Architecture and structure** | services diagram; changed outlined red, affected amber | "Derived context: it never adds or removes a finding. Cycles, hubs and layer violations come from the code's call graph." |
| | **Tester pack for this change** | gate **gaps**: 2 covered, 3 with gaps, 1 not evaluated | "One change, three answers: tab 1 retest and gate, tab 2 locators, tab 3 API matrix." |
| | **Show agent response (Knowledge Fabric JSON)** | chunks: 1 `coverage_gate`, 4 `coverage_gap`, 3 `api_matrix`, 2 `ui_locators`, plus findings, test scope, unknowns | "Exactly what an AI testing agent receives (`POST /query/tia`). Module C." |
| | **Team coordination** → *Notify the teams* | 2 teams, notification *failed*: "no channel configured" | "Owners come from `tia-services.yaml` or the catalog. Chat, email or ITSM channels are configured per tenant; none here, and TIA says so." |
| | **Acknowledge** `team-portal`; **Reviewer verdict**: finding 1 `confirmed`, finding 3 `false positive` | recorded | "Verdicts never change the report. They feed rule precision (5b) and are in the audit chain." |
| | **PR comment** tab: *Plain text* / *Markdown (for GitHub)* | the comment a PR would receive, with a Mermaid diagram | "This is the body posted by the webhook flow." |
| | Click the analysis id under the report | the shareable page: 5 columns, same Why, drawn paths | "No JavaScript at all, so it opens from a PR comment and prints." |
| **5. Tester views** | **Common components & rule gaps** | `list_payments`, `load_payments`, `ordered` each used by 3 entry points; **1 rule gap** | "Use case 1: the stakeholder's shared-component and rule-in-one-lane insights." |
| | **Technical coverage** | **41.4%** of 35 aspects; `POST /accounts/…/payments` 3 of 11 | "Use case 2: the gaps are named: amount limits, currency, reference format, the 409." |
| | **Automation: APIs** → `POST /accounts/{account_id}/payments` | `amount`: valid 0.01, 10000; invalid 0, 10000.01, abc | "Use case 3. Generated from the `Field(...)` rules, no hand-written matrix." |
| | **Automation: pages & locators** | 2 pages, 14 elements, **2 without a unique locator** | "The two *Export* buttons can only be found by position. TIA says so." |
| | **Check one test case**: title *Amount above the limit*, entry `POST /accounts/{id}/payments`, steps *Send a payment with amount 10001, currency EUR and reference INV778*, expected *The service refuses it with 422* | covers 40.9%, incl. "an invalid 'amount' is rejected" | "A test case as a query: what it covers, what it misses." |
| **5a. Releases etc.** | Releases: compare `2026.10` → `2026.11` | **Introduced 2**: customer-portal → payments `GET` and `POST` | "Temporal comparison of two graph versions." |
| | *What ran where*: `2026-11-02 10:00` | payments in **staging only** | "From ingested deployment records." |
| | Incidents → **Investigate** `INC-1001` | 2 candidate changes, never "the cause" | "Time-correlated and dependency-derived candidates; TIA does not assert causation." |
| **5b. Insights** | Trends; Rule precision | 6 changes, 1 breaking; `SEV-BREAKING-CONFIRMED` precision 50% "(too few)" | "Fed by the reviewer verdicts above." |
| **6. Configuration** | Section 7 → *Service catalog import* (CSV below) then re-run `contract` | findings: **2 critical + 3 high** | "Criticality is the tenant's, not the code's. The rule id changes to `SEV-BREAKING-CRITICAL`." |
| | Set `max_depth` to `0`, **Validate** | "Not valid: limits.max_depth must be between 1 and 50" | "Configuration is validated, versioned and audited; every save is a new immutable version." |
| | Set `max_depth` to `1`, save, re-run `behaviour` | **1 finding (low)** | "Depth 1 stops before the consumers: a visible trade-off. **Restore** version 1 afterwards." |
| **7. Administration** | Create a token: `ci-pipeline`, roles viewer + engineer | a `tia_…` token, shown once | "Scoped service accounts for CI and agents. Hashed at rest." |
| | Evidence held: kinds `deployment`, `test_run`, `incident`, `release` | 4, 1, 1, and the releases | "Everything CI and ops push is queryable and deletable (audited)." |
| **1b. Upload** | *Upload zip* `demo-data/inputs/loyalty-service-v1.zip` | 4 services; `loyalty-service → payments-service` found from code | "A fourth consumer, with no Git host involved." |
| | Re-run `contract` | `loyalty-service` now among the affected services | "The graph updated by itself; no one declared the dependency." |
| | Upload `…-v2.zip`, **Analyse vs previous upload** | 1 finding (low); test scope `GET /loyalty/{account_id}/points` | "Change impact between two uploads: the local PR." |

Catalog CSV for step 6:
```
service,owner,criticality
statement-service,team-statements,critical
customer-portal,team-portal,high
```

---

## MODULE C: Agents, operations and the hard questions (20 minutes)

### C1. Consuming the graph programmatically

Everything the UI shows is JSON first. In a deployment with sign-in, add `Authorization: Bearer tia_…`.

```
# the finished tester pack of one analysis (what to retest + gate, locators, API matrix)
curl -s localhost:8080/api/v1/impact-analyses/$AID/tester-pack

# a direct graph question: who depends on this endpoint?
curl -sG localhost:8080/api/v1/graph/query --data-urlencode 'question=consumers' \
     --data-urlencode 'component=GET /accounts/{account_id}/payments'

# the Knowledge Fabric agent answer (chunks and paths); the question text is never parsed
curl -s -X POST localhost:8080/query/tia -H 'content-type: application/json' -d "{\"analysis_id\": \"$AID\"}"

# the MCP server for coding and testing assistants: 11 read-only tools
curl -s -X POST localhost:8080/mcp -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

```python
from tia_client import TiaClient
tia = TiaClient("http://localhost:8080")
rep = tia.analyze(target="payments-service", head="contract")
pack = tia.tester_pack(rep["analysis_id"])               # pack["gate"]["outcome"] == "gaps"
tia.consumers_of("GET /accounts/{account_id}/payments")  # {"status": "ok", "results": {"services": [...], ...}, "limits": [...]}
tia.covered_by_test("test_statement_pdf")                # what one test exercises
```

| Channel | Tools / calls | Contract |
|---|---|---|
| REST | everything; OpenAPI in `docs/openapi.json`, kept in step by a test | permissions in the route table (`rbac.py`) |
| MCP (`/mcp`) | `get_impact_report`, `find_impact_report`, `get_test_scope`, `get_policy_outcome`, `get_tester_pack`, `get_coverage`, `get_api_inventory`, `get_ui_inventory`, `check_test_case`, `get_shared_components`, `query_graph` | **no write tool exists**; instructions tell the assistant to narrate only returned fields |
| Knowledge Fabric (`/query/tia`) | categories `tia_finding`, `test_scope`, `rule_gap`, `consistency_check`, `coverage_gate`, `coverage_gap`, `api_matrix`, `ui_locators`, `unknown` | serves **finished analyses only**; never upgrades `inferred`; always returns unknowns |
| Python SDK / CLI | `tia_client`; `tia analyze`, `coverage`, `automation`, `shared-components` | same answers as REST |

**Rules that make the answers safe to automate.** An answer with status `ambiguous` must be asked again with one candidate (never guessed). Inferred rows are leads, not facts. `not_evaluated` is not "safe". Every graph answer lists its **limits** ("consumers outside the analysed repositories are not visible"; "test links are static").

### C2. Operating it

| Topic | What is there | Status |
|---|---|---|
| Deployment | Docker image, Compose (single machine), Helm chart (API, worker, migration job), Terraform for Azure | Compose and a single-node Kubernetes install **verified (live)**: migration hook, upgrade, an authenticated analysis, killed pods replaced. **Terraform and multi-node failover not verified** |
| Health | `/healthz`; `/readyz` checks the database, the schema (migrations applied), the workspace disk, and reports *degraded* when jobs are queued but no worker has been seen recently; Prometheus `/metrics`; OpenTelemetry traces that continue from the API into the worker | Built |
| Backup | encrypted tenant backup and restore; a restore into a second database was verified (26 tables) | Verified (live, Compose). A timed restore drill is not done |
| Retention | per-tenant days for graphs, analyses, runtime, incidents, audit; purge keeps the audit chain verifiable | Built, tested |
| Scale | 500 generated repositories (0.6 M lines): PR P95 **8.9 s**, report page P95 **0.19 s**, 50-PR burst comment P95 **147.5 s** (targets 120 s, 5 s, 180 s) | Measured on generated code. **20 M lines not measured** |
| Air-gap | with `TIA_AIRGAPPED=true` every outbound connection (source-host APIs, git fetches, identity-provider discovery, ServiceNow, KMS, trace export) is refused unless its host is this machine or on the allow-list: a refused call fails loudly (`AirGapError`), never silently. Analysis itself never needs the network; the UI libraries are vendored (`airgap.py`) | Built, tested |
| Secrets | master key and per-tenant secrets encrypted (`secretstore.py`, `kms.py`); source-hosting tokens are not stored; findings never echo secret-like values (`scrub.py`) | Built |

### C3. Questions the customer's engineers will ask

| Question | Honest answer |
|---|---|
| Is there AI or an LLM in the analysis? | **No.** Parsers and a fixed rule table. That is why a result is reproducible and every claim cites a file and line. AI agents consume the output through MCP or Knowledge Fabric. |
| Which languages? | Back end: Python, Java, Kotlin, C#, Go, TypeScript/JavaScript (Express, NestJS). Front end: Angular, React, Vue, HTML. Data: JPA, SQLAlchemy, Django, Entity Framework, SQL files and stored procedures, Kafka. Others through the adapter SDK (`docs/ADAPTER-SDK.md`). |
| How accurate is it on *our* code? | Unmeasured. Accuracy targets were met on two independent real-code Java landscapes and a synthetic one (`tools/accuracy.py`). Events, SQL lineage and the Go/C#/Kotlin/TypeScript adapters were tested on **samples written for the tests**, not on a labelled real landscape. Proposed next step: a joint run on a labelled slice of your code. |
| What does it miss? | Anything not statically visible: URLs assembled at run time, reflection, service meshes that rewrite routes, consumers outside the analysed repositories. It reports these as **unknowns** (the demo shows four unresolved calls). |
| Can it be fooled by names? | Name matching alone never makes a confirmed link. Unique-path matches are `inferred` and capped at medium. |
| What if a repository does not parse? | The file goes to `files_failed` and the report's unknowns; unreadable files are excluded from the diff, never counted as deleted. |
| Can it run our code or leak it? | It never executes anything. Source is read from git objects. Secrets found in source are withheld from records. Findings only name repositories the reader may read. |
| Is it multi-tenant safe? | Forced row-level security, non-bypass roles, transaction-local tenant setting; tests per threat (`SECURITY.md`, T1-T35). **No independent penetration test has been done.** |
| What does it cost to run? | PostgreSQL plus an API process and one or more workers; no GPU, no external service. |
| Does it block merges? | No. Advisory by design; release policy outcomes are reported, and an owner approval is recorded. |
| What is verified against real systems? | The GitHub PR comment (live). GitLab, Bitbucket, ServiceNow, cloud KMS, mail and chat were verified against stand-ins only. See section 9. |

---

## 8. White label: what is true today

**White-labelling is not a built feature.** There is no branding setting. The product name is fixed in code:

| Where the name "TIA" is hard-coded today |
|---|
| page title and header (`tia/web/index.html`: `<title>`, `<h1>TIA</h1>`), shareable page title (`reportpage.py`) |
| PR comment heading `### TIA impact analysis` and the marker `<!-- tia-analysis -->` that lets TIA find and update its own comment (`report.py`) |
| MCP `serverInfo` (`name: "tia"`) and the OpenAPI title (`api.py`) |
| URL paths (`/query/tia`), the Python package `tia`, the SDK `tia_client`, the CLI `tia`, `TIA_*` environment variables, the Helm chart name |
| documentation, container image names |

A customer-facing re-brand therefore needs a small piece of work, not a configuration change. A reasonable scope is a **per-tenant branding setting** (product name, logo, accent colour, favicon, PR comment heading and marker, MCP and OpenAPI titles) stored in the existing versioned tenant configuration, plus a build-time rename of packages and environment variables where the customer's standards demand it. Decide with the customer which of these they need before promising a date.

## 9. What is not verified (state it up front)

| Area | Status |
|---|---|
| Accuracy on the customer's code; events, SQL lineage and the four added languages on real labelled code | **Not measured** |
| GitLab, Bitbucket, ServiceNow, cloud KMS, SMTP and chat integrations | Verified (simulated) only |
| Independent security review and penetration test; CI actions pinned by commit SHA | **Not done** |
| Azure Terraform; multi-node cluster and node failure; ReadWriteMany workspace; timed restore drill | **Not verified** |
| 20 M lines of code performance; SQL/event lineage and new languages in the benchmark landscape | **Not measured** |
| The Knowledge Fabric adapter against the real retriever (the field lists follow the integration document; `retriever_api.py` was not available) | Unverified against the real system |
| Business-requirement mapping (Pulse's job, deliberately out of scope) | Not in TIA |

The test suite is large (1,190+ tests on real PostgreSQL 16 and real git) and is the first thing to run in front of a technical audience: `python -m pytest -q` (about 50 minutes; `tests/test_demo_seed.py` alone checks every demo number in about a minute).

---

## Appendix A: the 100-minute run sheet

| Time | Module | Content |
|---|---|---|
| 0:00 | Intro | the example (section 1), the architecture page (section 2), "no AI" |
| 0:10 | A0-A2 | request, git, extraction with the real records |
| 0:25 | A3-A5 | resolution, graph, diff, traversal on `contract` |
| 0:40 | A6-A8 | severity, tester use cases, snapshot, reproduce, audit chain, RLS |
| 0:55 | B | the UI table: sections 1 → 7 and the upload finale |
| 1:30 | C | agents (C1), operations and questions (C2, C3), sections 8 and 9 |

## Appendix B: file map

| Area | Files |
|---|---|
| API, access | `api.py`, `authapi.py`, `identity.py`, `rbac.py`, `access.py`, `tenants.py`, `tenantconfig.py` |
| Jobs | `jobs.py`, `worker.py`, `analysis_service.py`, `webhooks.py`, `scmhosts.py`, `githubapp.py`, `ado.py` |
| Git, extraction | `gitutil.py`, `extraction.py`, `extract_python.py`, `extract_java.py`, `lang/*`, `adapters.py`, `extract_ui.py`, `extract_orm.py`, `extract_data.py`, `sqllineage.py`, `events.py`, `streams.py`, `models.py` |
| Graph | `resolve.py`, `graph.py`, `graphstore.py`, `graphview.py`, `graphquery.py`, `data.py` |
| Impact | `impact.py`, `contracts.py`, `severity.py`, `enrich.py`, `policy.py`, `coordination.py`, `structure.py`, `communities.py`, `diagram.py`, `findings_catalog.py` |
| Tester | `testscope.py`, `coverage.py`, `testcases.py`, `automation.py`, `testerpack.py` |
| Evidence and ops | `evidence.py`, `temporal.py`, `incidents.py`, `calibration.py`, `retention.py`, `backup.py`, `store.py`, `db.py`, `migrations/*.sql`, `metrics.py`, `tracing.py`, `health.py` |
| Delivery | `report.py`, `reportpage.py`, `web/index.html`, `kfadapter.py`, `mcp.py`, `sdk/python/tia_client`, `cli.py` |
| Deployment | `deploy/` (Dockerfile, Compose, Helm, Terraform) |
