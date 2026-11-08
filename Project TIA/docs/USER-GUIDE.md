# TIA user guide

For engineers, testers and release managers who use TIA to understand what a change affects. Administrators read the [administrator guide](ADMIN-GUIDE.md);
programmers read the [API reference](API.md).

## What TIA tells you

For a proposed change to one repository, TIA answers: **what else may be affected** (other services, endpoints, tests, database tables), **why** (the evidence path),
**how sure it is** (the evidence class), and **what it does not know**. The answer is a *Change Impact Report*. It is advisory: it never blocks a merge.

Evidence classes, from strongest to weakest:

| Class | Meaning |
|---|---|
| `source_confirmed` | found in source code: an actual call to the changed endpoint, resolved to it |
| `contract_confirmed` | a declared consumer contract names the changed endpoint |
| `inferred` | a likely match (for example a unique path), always labelled, **never presented as confirmed** |
| `stale` | seen earlier, but the repository could not be re-read; the relationship may no longer hold |
| `unknown` | TIA could not tell; listed under *Unknowns* |

The report's status says whether to trust its completeness: `complete`, `complete_with_gaps` (finished, but there are unknowns), `partial` (incomplete: a limit was reached, a file did not parse,
or a source was unreachable) or `failed`. **Read the unknowns.** A clean-looking report with unknowns about unparseable files or unreachable repositories is not a clean bill of health.

## Signing in

Open TIA's address. If your organisation requires sign-in you will see **Sign in**: type your organisation's name, choose the provider, and sign in there.
What you can do depends on your role (shown next to your name): *viewer* reads; *engineer* also analyses and ingests; *release manager* also changes the configuration; *auditor* reads the audit log;
*administrator* manages people and access. Buttons you may not use are disabled.

You only see repositories you are allowed to read on the source-code host. For everything else TIA behaves as if it did not exist, and a report shows
"N finding(s) in repositories you cannot access were omitted". If you see no repositories at all, ask an administrator whether your GitHub login is on your TIA profile.

## 1. Bring in repositories

* **Git URL** (GitHub, Azure DevOps or any git server): paste the URL (and a token for a private repository; it is used for the clone and not stored).
* **Whole project or organisation**: discover and ingest everything you can access.
* **Upload a zip or a whole folder** from your computer (up to 2 GiB). Each upload is stored as a revision. Upload the same name again with changed code and use **Analyse vs previous upload**;
  no git host is involved. Folders such as `.git`, `node_modules` and virtual environments are skipped.
* **Local path** (development setups only).

TIA reads the code; it never builds or runs it.

## 2. Look at the graph

The graph shows the dependencies TIA extracted from every repository's default branch: services, endpoints and schemas, functions and call sites, tests, database tables and columns.
Edge colour is the evidence. Click a node for details; double-click to focus on its neighbourhood; search by name, path or file.

## 3. Analyse a change

Choose the repository that contains the change and say what the change is: a **branch, tag or commit** (compared with the base, by default the default branch) or a **pull request number**.
Press **Run analysis**.

What TIA looks at: changed endpoints and their compatibility (removed or moved routes, schema fields added, removed, retyped or made required), changed functions,
changed database migrations and the tables and columns they touch, and then who depends on those things, across repositories.

## 4. Read the report

* **Severity** combines the kind of change with how critical the affected service is. **Material** findings are the ones worth your attention; the others are listed for completeness.
* **Evidence** shows the class and how the link was resolved: R1 a shared or generated client, R2 a call whose host or configuration key resolves to a service (through the service map `tia-services.yaml`, or because the host is the service's name), R3 a declared consumer contract, R5 inferred from a unique path.
* **Tests** lists the tests that exercise the affected code, found by static association; it does not say they pass.
* **Unknowns** lists everything TIA could not see or evaluate: unreachable or stale repositories, files that did not parse, unsupported HTTP clients, consumers outside the analysed repositories,
  event consumers, compatibility aspects not evaluated, call sites it could not resolve, and credentials it withheld.
* **Show on graph** draws the impact paths; **Show full JSON** shows everything. Every material finding links to its evidence; `GET /api/v1/impact-analyses/{id}/evidence` returns the evidence snapshot.
* A report is **reproducible**: the same revisions, extractor versions and configuration give the same report; re-running it on its evidence snapshot gives an identical result even after the repositories have moved on.
* Each analysis has a shareable page, `/analyses/<id>`, which pull-request comments link to. Opening it needs the same permission as the rest of TIA.

## 4a. What to test (for testers)

In the web UI the tester's view is in two places:
* the **What to test** block at the top of each analysis report;
* section **5. Tester views**, with these tabs:
  * *Common components & rule gaps*;
  * *Technical coverage*;
  * *Automation: APIs*;
  * *Automation: pages & locators*;
  * *Check one test case*.

The same information is available from the API and the command line described below.

The report starts with **What to test**: the things you can test without reading code. It has four parts.

* **Entry points to test**: every API endpoint the change reaches, in the changed service and in every analysed service that calls it. Each row gives:
  * the plain-language name its owners gave it, if any (see below);
  * **why** it is reached (the chain of code from the change to it);
  * the **change effect**;
  * the **tests found** for it and for the code between it and the change.

  Code that the change reaches but no analysed endpoint calls is listed separately, under *no analysed API endpoint*. It may be used by a screen, a job or a channel TIA does not analyse.
* **Change effect** of each changed function, and so of each entry point (the strongest on its path):

  | Effect | Meaning |
  |---|---|
  | `breaking` | a contract breaks (removed route or field, changed type, new required field), a removed function is still called, or a function's signature changed and its callers were not updated |
  | `behaviour change` | the logic changed (or the signature changed and its callers were updated): retest |
  | `compatible change` | a compatible addition (optional field, new function, removed code nothing calls) |
  | `no functional change` | documentation only (docstrings, comments, formatting): nothing to retest |
  | `unknown` | a file TIA could only compare as a whole |

* **Consistency checks**: two or more services in other repositories take data from the same changed endpoint. Call them for the same input and check they show the same records.
* **Possible rule gaps**: a shared endpoint's consumers do not all apply the same rule to its data.
  * Example: the statement PDF leaves out disputed payments (`not p['disputed']`) and the payments screen does not.
  * TIA names the shared endpoint, the rule, where it is applied (function, file and line) and where it is missing.
  * If the rule should apply everywhere, this is a defect; if not, confirm the difference is intended.
  * When the change moves the rule into the shared service itself, it says **Rule moved into the shared service**: test every consumer, including those that already applied it.
  * A rule counts only when it tests a field of the shared endpoint's **declared response schema**. Only confirmed relationships are used.

To give entry points plain-language names, add them to the owning repository's `tia-services.yaml`. Each key is the HTTP method (any case) and the route as declared in the code:

```yaml
entry_points:
  get /statements/{account_id}/pdf: Monthly statement PDF
```

**Before any change**, `GET /api/v1/graph/shared-components` (or `tia shared-components --repo NAME=PATH ...`) lists:
* the endpoints and functions most entry points depend on, so a change to them needs the widest testing;
* every rule gap in the landscape.

## 4b. Automation inventory (for automation testers)

`GET /api/v1/automation/api` (or `tia automation --repo NAME=PATH ...`) lists every API endpoint, as TIA reads it from the code. For each endpoint:
* **Inputs:** each path, query and header parameter and each request-body field. For each input:
  * mandatory or optional, and multi-valued (a list) or not;
  * the values it allows (enums, `Literal`, Java enums);
  * its limits: lengths, ranges, patterns, e-mail format.
* **Valid and invalid values** generated from those rules, including boundaries such as the lowest and highest allowed amount.
  * A pattern too complex to generate a value for is stated, not guessed.
* **Test matrix:**
  * one valid baseline request;
  * one case per invalid value (expected: rejected, 422 for FastAPI, 400 for Spring);
  * one case per boundary or allowed value (expected: accepted).
* **Error statuses** the handler answers with (for example 404 unknown account, 409 duplicate reference).

`GET /api/v1/automation/ui` (or `tia automation --ui ...`) lists every page of the front ends TIA can read (Angular, React, Vue, plain HTML). For each page:
* **Route.**
* **Elements**, each with:
  * a **locator** checked to be unique on the page, best first: test id, id, name, form control, aria label, label, text, placeholder. It comes in CSS, XPath and Playwright form;
  * whether it is **mandatory** (a `required` attribute or `Validators.required`);
  * its input rules, with valid and invalid values;
  * for a dropdown, its **values**: static, from a literal list in the component, or "dynamic" when they are loaded at run time.
  * An element that has no unique locator is flagged `position (fragile)`. An element repeated for each row of a list is flagged too.
* **APIs the page calls**, linked to the analysed endpoints.

## 4c. Technical coverage: are my test cases enough?

`GET /api/v1/coverage` (or `tia coverage --repo ...`) lists the **technical aspects** each endpoint and page implies:
* it is exercised at all;
* each mandatory input left out is rejected;
* each input that breaks its rule is rejected;
* each allowed value is accepted;
* each error status is answered;
* each data rule on its path holds (for example, disputed payments left out).

For each aspect it lists which tests address it. Two kinds of test count:
* **automated tests** in the repositories: what they call, the request bodies and values they send, and the statuses they assert;
* **written test cases** kept in a repository under `testcases/` as CSV (a test-management export), YAML or JSON.
  * Columns are matched loosely: *ID*, *Title*/*Summary*, *Steps*, *Expected result*, *Entry point* (an API given as method and path, such as `POST` `/accounts/{id}/payments`; a page route such as `/accounts/:id/payments`; or an entry point's plain name), *Priority*.

Each aspect is `covered`, `partial` (some allowed values tested) or `missing`. Each endpoint and the whole landscape get a percentage. Written test cases that match nothing are listed.

**One test case as a query:** `POST /api/v1/coverage/test-case` with `title`, `steps`, `expected` and `entry_point` (or `tia coverage --test-case FILE`). It returns what that test case covers in its area and what it misses.

Coverage here is **static**. "Covered" means a test addresses the aspect, not that it passes.
* A written test case is matched by its text: it must name the field and use words such as "missing" or "invalid".
* Functional coverage against business requirements is a separate tool.

## 4d. Evidence beyond the code (CI, coverage, runtime, deployments)

When your pipelines push evidence to TIA (see the [administrator guide](ADMIN-GUIDE.md#15-evidence-ci-results-coverage-runtime-deployments)), the report also says:

* **For every relevant test**, these states, never merged into one another:
  * it exists;
  * it is associated (and how);
  * it `passed` / `failed` / was `skipped` in a named CI build, and whether that build was of the analysed revision;
  * it `executed` the component (per-test coverage);
  * it `validated_contract` (it calls the affected endpoint and passed on the analysed revision).
  * Without CI results it says `no_ci_results`; without per-test coverage, whether a test executed a component is *unknown*, not "no".
* **Recommended tests**, each with the reason it is relevant: priority 1 executed the changed code, 2 is associated with an affected component, 3 by
  naming only. Tests not listed are **not** shown to be safe to skip.
* **Without test evidence**: affected entry points and material findings for which no relevant test evidence was found in the analysed sources.
* **Observed at runtime**: a relationship seen in telemetry is `corroborated` (source and runtime agree) or `runtime_observed` (only seen at runtime, for
  example a caller outside the analysed repositories), with the environments, time window and versions. A dependency **not** observed is still reported:
  absence of an observation is not proof.
* **Deployments**: per environment, the revision deployed and whether it is the analysed, the base or the proposed revision. A consumer running an older
  revision in production is called out on its finding.

## 4e. Contracts and events

* **REST contracts** are compared in full:
  * removed or moved routes;
  * removed or retyped fields;
  * fields or parameters that became required;
  * allowed values removed from a request (narrowing) or added to a response (widening);
  * tightened limits (shorter maximum length, higher minimum, a new pattern);
  * a response that changes between an object and a list;
  * the success status;
  * authentication and authorisation requirements.

  Your organisation can set each rule to *breaking* or *compatible*. OpenAPI/Swagger files in a repository count like code; where the code declares the
  same endpoint, the code wins.
* **Events (Kafka)**:
  * Producers and consumers are found in Python and Java code, or declared in `tia-services.yaml`.
  * Avro and JSON Schema files are tied to their topic.
  * A removed or retyped field, a new enum value or a topic no longer produced reaches every known consumer, with the evidence (`E1` code, `E2` declaration).
  * A topic that no analysed repository consumes is reported as an unknown.

## 4f. Release policy

If your organisation configured a release policy, the report, the PR comment and the check run show each rule's outcome:
* `pass`;
* `fail` (with the failing requirement and its evidence);
* `not_evaluated` (a data source such as CI results is missing: never treated as a pass);
* `requires_review`.

In *advisory* mode nothing is blocked. In *enforcing* mode a failed mandatory rule makes the TIA check fail, and your branch protection decides what that blocks.
An owning team approves with `POST /api/v1/impact-analyses/{aid}/approvals`.

## 4g. Assistants (MCP)

Coding assistants can read reports through TIA's MCP server (`POST /mcp`):
* `get_impact_report` / `find_impact_report` / `get_test_scope` / `get_policy_outcome`;
* read-only, showing what your token may see;
* an assistant should cite the analysis id and narrate only what the report contains.

## 4h. Releases, environments and incidents

* **Releases**: `POST /api/v1/releases` with `{"name": "2026.11", "revisions": {"orders-service": "v4.2", ...}}` records a release (commits, tags or branches,
  resolved to commits) and the dependency graph at exactly those revisions.
  * `GET /api/v1/releases/compare?base=2026.10&head=2026.11` lists the dependencies **introduced**, **removed** and **modified** (same two ends, different evidence) between them.
  * `GET /api/v1/releases/{name}/consumers?component=GET%20/prices/{}` answers "who was known to use this API, schema or topic in that release".
* **What ran where**: `GET /api/v1/environments/at?at=2026-10-20T12:00:00Z` gives, per repository and environment, the revision deployed at that moment (from deployment records).
* **Incidents**: push an incident with `POST /api/v1/incidents`, link a change you have validated with `POST /api/v1/incidents/{iid}/links`, and open
  `GET /api/v1/incidents/{iid}/investigation`. Each candidate change says **how** it is associated:
  * `directly_recorded` (the incident names it);
  * `time_correlated` (deployed to the incident's service and environment shortly before; 72 hours by default, `window_hours` to change);
  * `dependency_derived` (deployed to a service the incident's service depends on);
  * `manually_validated` (a person linked it, with who and when).

  TIA never names a root cause: the candidates are leads for your investigation, not a conclusion.

## 4i. Teams, verdicts and trends

* **Coordination**: when a change materially affects components other teams own, those teams are notified (chat, e-mail or a ServiceNow task, as your
  organisation configured). Acknowledge for your team with `POST /api/v1/impact-analyses/{aid}/acknowledgements`; `GET /api/v1/impact-analyses/{aid}/coordination`
  shows who was told and who has acknowledged. A release policy can require every affected team's acknowledgement.
* **Verdicts**: if a finding was right, wrong or not worth the attention, say so with `POST /api/v1/impact-analyses/{aid}/verdicts`
  (`confirmed`, `false_positive`, `irrelevant`). Verdicts measure each rule's precision (`GET /api/v1/calibration`); they never change the report.
* **Trends**: `GET /api/v1/trends` shows, per application or team and month, how many changes were analysed, how many were breaking, and how often
  dependencies were unresolved or affected components had no test. These are past figures, not a forecast.

## 5. Pull requests

When your organisation has connected GitHub, GitLab, Bitbucket or Azure DevOps, opening or updating a pull (merge) request makes TIA analyse it automatically and post **one comment**, updated in place on every push,
plus a **neutral** check run. A newer push supersedes an older run; an old run never overwrites a newer result. The comment links to the full report.
By default the comment names only the pull request's own repository; whether it may mention other repositories is a setting your organisation chooses.

You can also publish an existing analysis to a pull request by hand: `POST /api/v1/impact-analyses/{id}/publish`.

## 6. Configuration (release managers and administrators)

The **Configuration** section edits the organisation's limits, severity overrides, service criticality, URL prefixes to ignore and comment audience. *Validate* lists every problem without saving;
*Save as new version* records who changed what and why; any earlier version can be restored. See the [administrator guide](ADMIN-GUIDE.md#7-tenant-configuration) for each setting.
Two settings matter most for accuracy:

* **Criticality** of a service (`critical`) raises the severity of anything that affects it.
* **URL prefixes**: if a service is called through a gateway or a servlet context path set in external configuration, list the prefix so calls and routes line up.

## 7. Limits to keep in mind

* TIA analyses what is **in the repositories you connected**. Consumers outside them are reported as an unknown, not as absent.
* HTTP clients it does not understand are listed, not guessed.
* Languages: Python, Java, Go, C#/.NET, Kotlin and TypeScript/JavaScript (Express, NestJS) back ends; Angular, React and Vue front ends.
* Compatibility is evaluated for routes, fields, constraints, enums, success status codes, response shape and authentication; headers and error response bodies are listed as *not evaluated*.
* Runtime behaviour and deployed versions come only from the evidence pushed to TIA; a dependency not observed in that window may still exist.
* An *inferred* finding is a lead, not a fact: check it.
* **What to test**:
  * Screens are named through the labels you declare, or found in the front-end code you connected.
  * Tests are found by static association and are not run by TIA; their results come from the CI results you push.
  * Rule gaps cover item filters in Python (comprehension conditions, `filter`, loops that skip or collect items) and Java (stream `filter`, `removeIf`). Other ways of applying a rule (SQL `WHERE` clauses, validation annotations, `if` branches that return early) are not compared.
