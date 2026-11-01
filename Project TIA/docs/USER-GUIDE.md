# TIA user guide

For engineers and release managers who use TIA to understand what a change affects. Administrators read the [administrator guide](ADMIN-GUIDE.md);
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
