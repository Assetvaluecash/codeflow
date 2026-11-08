# TIA language adapter SDK

Written for engineers adding a language or framework to TIA (PRD FR-040).

## 1. What an adapter does

An adapter reads **one source file** and returns the facts TIA needs from it, as a partial `RepoExtract`:

| Fact | Helper (`tia.lang.common`) | Used for |
|---|---|---|
| Endpoint (method, path, handler, request/response schema, params, auth, response shape) | `add_endpoint` | provider side of HTTP relationships, contract diffs |
| Outbound call (method, path, base: host or configuration key) | `add_callsite` | consumer side (strategy R2/R4) |
| Function (qualified name, calls, signature, body hash) | `add_function` | call graph, "logic changed / signature changed" |
| Schema (name, fields with `type`, `required`, `multi`, `nullable`, `constraints`) | `add_schema` | breaking/compatible field changes |
| Test (name, calls, HTTP calls made) | `add_test` | test scope and recommendations |

Everything else (merging files, resolving relationships across repositories, impact traversal, severity, reports) is done by TIA. An adapter
never resolves another repository and never guesses: a call whose base or path cannot be read statically is recorded with a `reason`
(the third element of the tuple passed to `add_callsite`), and it then appears in the report as an unknown rather than being dropped.

Paths are written as the framework writes them (`/orders/{id}`, `/orders/:id`); TIA normalises them. Field names are the **wire** names
(the JSON name, not the property name).

## 2. Built-in adapters

| Name | Version | Files | Recognises |
|---|---|---|---|
| `go` | go-1 | `.go` | net/http (Go 1.22 method patterns), chi, gin, echo, gorilla/mux; `os.Getenv` bases; json/validate/binding tags; `Test*` + httptest |
| `csharp` | cs-1 | `.cs` | ASP.NET Core controllers and minimal APIs, `[Authorize]`, HttpClient and named clients, DataAnnotations, Entity Framework, xUnit/NUnit/MSTest |
| `kotlin` | kt-1 | `.kt`, `.kts` | Spring annotations, `@Value("${key}")` bases, RestTemplate, WebClient, Bean Validation, MockMvc |
| `typescript` | ts-1 | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` | Express, NestJS (`@UseGuards` as auth), fetch, axios, `process.env.X` bases, class-validator DTOs, interfaces, jest/mocha + supertest |

Python and Java are built into the core. Front-end files (Angular, React, Vue) are additionally read for pages and form rules.
Generated and vendored files (`node_modules`, `vendor`, `bin`, `obj`, `dist`, `build`, `*.d.ts`, `*.pb.go`, `*.g.cs`, minified bundles) are skipped.

## 3. Writing a third-party adapter

```python
# acme_tia_ruby/__init__.py
from tia.adapters import Adapter
from tia.lang.common import add_endpoint, new_extract

def extract_file(repo, service, path, text, strip):
    out = new_extract(repo, service)
    out.files_analysed = 1
    # ... parse `text`, then for each route:
    # add_endpoint(out, service, repo, path, "GET", "/orders/{id}", "OrdersController#show", f"{repo}:{path}::OrdersController#show", line, strip)
    return out

ADAPTER = Adapter(name="ruby", version="rb-1", extensions=(".rb",), extract_file=extract_file, frameworks=("Rails",))
```

```toml
# pyproject.toml of the adapter package
[project.entry-points."tia.adapters"]
ruby = "acme_tia_ruby:ADAPTER"
```

Install the package into the same environment as the TIA server **and** worker, then restart both.

Rules the registry enforces:

* A third-party adapter cannot replace a built-in one: a name or extension already handled (including `.py` and `.java`) is ignored with a warning.
* Its `version` becomes part of `EXTRACTOR_VERSIONS`: every analysis records it, and changing it invalidates cached extracts, so bump it whenever
  the facts it produces change.
* If `extract_file` raises, the file is reported in `files_failed` with the adapter's name; the rest of the repository is still analysed and the
  report is marked incomplete accordingly.
* `extract_file` must be deterministic and depend only on its arguments (no network, no environment, no clock), so a reproduction gives the
  same result.

## 4. Testing an adapter

Follow `tests/test_adapters.py`: unit tests on source strings for each fact type, then a small git landscape
(`fixtures/build_polyglot_fixture.py`) where a provider change must reach a consumer in your language with `source_confirmed` evidence.

## 5. Limits of the built-in adapters

* Static reading only: routes registered in loops, URLs built at run time, and reflection are reported as unresolved, not inferred.
* Go response types are read from `json.NewEncoder(w).Encode(v)` and `c.JSON(status, v)` when `v`'s type is visible in the handler.
* Entity Framework is read from DbContext `DbSet<T>` properties, `[Table]`, `ToTable()`, `[Column]`, `[Key]` and DbSet reads/writes; Go database/sql and TypeORM/Prisma entities are not mapped (their SQL strings are read by the SQL lineage, FR-041).
* Kotlin coroutines/Ktor routing and Fastify/Koa/Hapi routes are not recognised.
