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

## 5. Pull requests

When your organisation has connected GitHub, opening or updating a pull request makes TIA analyse it automatically and post **one comment**, updated in place on every push,
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
* Compatibility is evaluated for routes and for the fields of request and response bodies; status codes, authentication, enum narrowing and headers are listed as *not evaluated*.
* Runtime behaviour, events, deployed versions and release policy are not part of this release.
* An *inferred* finding is a lead, not a fact: check it.
* **What to test** is limited to API entry points:
  * Screens are named only through the labels you declare. Front-end code (JavaScript/TypeScript) is not analysed.
  * Tests are found by static association and are not run.
  * Rule gaps cover item filters in Python (comprehension conditions, `filter`, loops that skip or collect items) and Java (stream `filter`, `removeIf`). Other ways of applying a rule (SQL `WHERE` clauses, validation annotations, `if` branches that return early) are not compared.
