# Case study: what Lane 0 (code ingestion) must give testers

**Source:** a recorded discussion with the testing stakeholder on 2026-11-02 (17 minutes). The full transcript is in Appendix A.
**Status:** requirement, marked **priority**.
* **UC-1:** built 2026-11-02.
* **UC-2 and UC-3:** built 2026-11-03.
* All three are tested on our own sample applications:
  * automated tests (`tests/test_testscope.py`, `tests/test_tester_views.py`);
  * a live local run on 2026-11-03. The real TIA server, with PostgreSQL and a background worker, ran every tester feature over HTTP (`tools/run_dev.py --demo --landscape lanes`).
  * a real-Chrome check of the web UI on 2026-11-03: **27/27 checks passed**.
    * The report's What to test block, with its rule gap and consistency check.
    * Each tester tab: common components, coverage, API inventory, pages and locators, and the one-test-case form.
    * No sideways scroll at phone width; no console errors.
* Not yet tried on a customer code base or real test repositories.
* The "What TIA does today" table (§5) is kept as a record of where we started, with each row's current state.

**Scope (decided 2026-11-02): TIA's Lane 0 only.** Linking code to business areas (requirements, features and rules held in Pulse) is too broad for now and is **out of scope**. TIA reports impact in terms a tester can test without reading code: endpoints, screens, reports/PDFs, shared components and rule gaps. Anything mapping these to business requirements is left to Pulse or a later phase.

## 1. The point of the discussion

Testers work black-box. Pulse already reads the **documentation**, so it knows the functional picture. Lane 0 adds what **only the code** shows, and it must give that to testers **in business and testing terms**.

- **P0 (necessary, not the goal):** ingesting code from Azure DevOps and GitHub.
- **The business objective:** open up and dissect the whole code base and tell the testing community:
  - how the system is really implemented;
  - what matters when they design, check or automate tests. For example: "there are 25 common methods / 25 common endpoints used across the system; any change to them has significant impact".
- **The demo must show business value**, not technical capability ("how the UI works", "which sources we can read").

## 2. The worked example: Keka work anniversaries

**Illustrative only.** Keka (an HR application with attendance, swipe-in/swipe-out and employee records) is used as a familiar example. We have no access to Keka's code and do not need it. Any application with the same shape works for the demo: one shared service called by two consumers, one of which applies a business rule the other does not (B6).

**Feature:** a work-anniversary module. The module interacts with the CRM and the swipe-in/swipe-out (attendance) systems, and produces a PDF. A new feature also shows work anniversaries and birthdays on the UI.

**What Pulse already does from documentation:** it knows the anniversaries shown on the UI must match those in the PDF, and that the module touches the CRM, the swipe systems and the PDF.

**What only the code shows (three insights):**

1. **Hidden shared component.** Previously the PDF API queried the database directly. Now a microservice endpoint ("employees whose work anniversary is this month") is called by **both** the UI's business API and the PDF API. **Any change to that microservice means both the UI and the PDF must be tested.** A tester cannot know this from the outside.
2. **Business rule implemented in one lane only (a defect).** Suppose the rule is "employees serving their notice period are not listed". It is implemented in the PDF lane, which filters them out after calling the microservice. The UI lane calls the same microservice and **prints the result without the filter**, so the UI shows people the PDF leaves out.
   - Today this is found only by manual, after-the-fact comparison.
   - Code reading should say directly: *"the UI and the PDF both call this common method, but the notice-period rule exists only on the PDF side."*
3. **The fix creates a new shared dependency.** The developer moves the notice-period filter into a generic method. Now the UI and the PDF both depend on it. The insight to give: *"both now call this reusable code; validate the UI and the PDF generation."*

## 3. The three use cases to demonstrate

### UC-1: Change impact during test-case design
- A developer changes a method. The method is used in three places, in these files, which **correspond to these business nodes** (features, requirements, rules).
- When there is a change, for whatever requirement, **the retriever (Knowledge Fabric) must return those business nodes as the impacted area**, from the technical impact analysis.
- This includes the Keka insights in §2: shared components, rules present in one lane and missing in another, and new shared dependencies.

### UC-2: Technical coverage of a test repository
- With **no change**, the tester asks: *"Is my test case repository sufficient?"*
- **Functional coverage** against business rules and requirements is the *functional coverage agent* another team member (Pratik) is building.
- **Lane 0 adds the technical side:** "here are the 25 technical aspects this area should cover". Given a test case as a query, say whether it covers them all.

### UC-3: Test automation inventory
- **APIs:**
  - every endpoint, with its request fields;
  - for each field: mandatory or optional, multi-valued or not, and its restrictions (for example, 0–9 digits only, Boolean only, one of five valid values);
  - from these, valid and invalid values and the permutations to automate.
- **UI:**
  - each page and the UI elements on it;
  - for each element: mandatory or optional, the **locator** that uniquely identifies it, and for a dropdown its possible values.
- **Stakeholder's estimate:** this alone saves about **20% of an automation tester's effort**.

## 4. What is *not* valuable (the IDE already does it)

- "This function changed and it is called in three other places." If the analysis stops there, it has **no value**. The developer's IDE, including AI IDEs such as Amazon Kiro, already gives this.
- The dependencies a file needs, its imports and libraries.
- How exactly a rule is implemented. That may matter later in a deep dive, but not now. **Only whether the rule is implemented or not.**

**The tester's problem statements, which Pulse must address top down:**
1. Are all my requirements met?
2. What is the significant impact of a change elsewhere?
3. Can I automate my tests faster?
4. Have I covered all permutations and combinations in my automation?
5. I inherited 100,000 test cases: do they cover every aspect of the implementation?

## 5. What TIA does today (checked in the code)

| Capability | Today | Where |
|---|---|---|
| Ingest code from GitHub and Azure DevOps (the P0) | **Yes.** Azure DevOps tested against a stand-in only | `tia/repos.py`, `tia/githubapp.py`, `tia/ado.py` |
| Changed method → the endpoint it serves → every service calling that endpoint, across repositories | **Yes**, for Python and Java (Spring) | `tia/graph.py`, `tia/impact.py` |
| Insight 1: find the **shared microservice** used by both the UI's backend and the PDF API | **Yes, if both are Python/Java services** whose HTTP calls TIA resolves. They appear as two findings on one change | `tia/resolve.py` |
| Answers to the Knowledge Fabric retriever | **Yes:** `POST /query/tia` returns a finished analysis as KF chunks and paths. Built from the integration document's field list; not yet checked against KF's real code | `tia/kfadapter.py` |
| Map code to **business nodes** (features, requirements, rules) | **No.** Out of scope for Lane 0 (see Scope) | — |
| Insight 2: **a rule present in one caller and missing in another** | **Yes (built 2026-11-02).** Rule gaps between the consumers of a shared endpoint. A rule counts only when it tests fields of the endpoint's declared response schema, using confirmed relationships only. Also recognised when a change moves the rule into the shared service | `tia/testscope.py` |
| "Shared components" list (most-used methods and endpoints) | **Yes (built 2026-11-02).** `GET /api/v1/graph/shared-components`, `tia shared-components` | `tia/testscope.py`, `tia/api.py`, `tia/cli.py` |
| **Entry points to test** for a change, with why, change effect and tests found; reached code with no analysed endpoint listed separately; consistency checks | **Yes (built 2026-11-02).** In the report (`test_scope`), PR comment ("What to test"), report page and Knowledge Fabric answer | `tia/testscope.py`, `tia/report.py`, `tia/reportpage.py`, `tia/kfadapter.py` |
| Breaking vs non-breaking for API contracts and database migrations | **Yes** | `tia/contracts.py`, `tia/extract_data.py` |
| Breaking vs non-breaking for a changed **method** | **Yes (built 2026-11-02):** `breaking` (removed but still called; signature changed and callers not updated), `behaviour_change`, `compatible_change`, `no_functional_change` (docstring/comments only). Before, every modified or removed method was "non-breaking" | `tia/testscope.py` (`classify_function`) |
| Tests linked to a change | Static association only; tests are not run and results are not shown | `tia/graph.py` lines 100–130 |
| UC-2: technical aspects an area should cover, and checking a test case against them | **Yes (built 2026-11-03).** Aspects per endpoint and page; automated tests and written test cases (`testcases/*.csv\|yaml\|json`) matched to them; one test case as a query | `tia/coverage.py`, `tia/testcases.py` |
| UC-3 APIs: endpoints with request fields, type, required or not | **Yes** | `tia/automation.py` |
| UC-3 APIs: allowed values, formats, ranges, valid and invalid examples | **Yes (built 2026-11-03).** Python (Pydantic `Field`, `Literal`, `Enum`, `constr`, `EmailStr`, FastAPI `Path`/`Query`/`Header`) and Java (Bean Validation, enums, `@PathVariable`/`@RequestParam`); a test matrix with expected outcomes; the error statuses each handler answers with | `tia/extract_python.py`, `tia/extract_java.py`, `tia/automation.py` |
| UC-3 UI: pages, elements, locators, mandatory flags, dropdown values | **Yes (built 2026-11-03).** Angular, React, Vue and plain HTML. Locators are checked for uniqueness on the page; dropdown values come from the template or a literal list (values loaded at run time are reported as dynamic); each page's API calls are linked to endpoints | `tia/extract_ui.py`, `tia/automation.py` |
| External systems (CRM, swipe-in/out) | **No**, unless they are analysed repositories | — |

**In short:** TIA already has the hard base for UC-1: cross-service impact, the shared-component finding and a Knowledge Fabric answer. The missing parts, within Lane 0, are:
- naming impact as **tester-visible entry points** (endpoints, screens, reports) instead of functions;
- **rule divergence** between callers;
- results stated for **testers**, not developers.

UC-2 and UC-3 are mostly new work. The API half of UC-3 builds on what is already extracted.

## 6. Recommendation: start with UC-1, using the Keka example as the demo

**Why:**
- The stakeholder's own example is UC-1.
- It is TIA's lane: technical impact analysis.
- Most of the machinery exists, including the Knowledge Fabric adapter.

**Demo storyline:**
1. Change the shared anniversary microservice method. TIA returns **both** the UI anniversary screen's API and the PDF generation API as the impacted entry points to test, through the KF retriever, with the reason ("both call this common endpoint").
2. **Rule divergence:** the notice-period rule is in the PDF lane only. Flag it as a likely defect *before* any change is made.
3. Move the rule into a generic method. TIA says "the UI and the PDF now both depend on this; validate both".
4. Show the **shared-components list** ("N common methods/endpoints; changes here have significant impact").

**What to build:**

| # | Work item |
|---|---|
| B1 | **Entry points to test.** Roll every finding up to the externally visible entry points it reaches: API endpoints, UI-facing backend routes, report/PDF endpoints. Each can have an optional plain-language label in `tia-services.yaml` (for example, "Anniversary PDF"). No link to Pulse business nodes (out of scope) |
| B2 | **Rule divergence.** When two or more callers use the same shared component, compare the filtering and validation each applies to its result, and report rules present in one and absent in the other |
| B3 | **Shared-components report.** Rank methods and endpoints by how many entry points reach them |
| B4 | **Tester-facing output.** One row per entry point to retest: why, breaking / behaviour change / safe, tests found. In the report, the PR comment and the KF answer |
| B5 | **Classify method changes** (removed, signature changed, body only, cosmetic). Used as an input to B4, not shown on its own |
| B6 | **Demo application.** Any application with the right shape, ours or open source, used as the demo and as an accuracy-harness fixture: one shared service, two consumers (for example, a UI backend and a report/PDF API), and a business rule applied by only one of them |

**Done when:**
- On the B6 fixture, the KF query for the change returns both entry points (the UI's API and the PDF API) with the reason.
- The notice-period divergence is reported.
- After the refactor, both are listed as "validate".
- Each case is checked by an automated test.

### Status of the work items (2026-11-02)

**Verification level:** verified (simulated). The automated tests run on generated git repositories, never on a customer code base.

| # | Status | Where it is checked |
|---|---|---|
| B1 Entry points to test | **Done.** Plain-language labels come from `tia-services.yaml` `entry_points:`. Screens are named only through these labels: front-end code is not analysed | `tests/test_testscope.py` |
| B2 Rule divergence | **Done**, for item filters in Python (comprehension conditions, `filter`, loops that skip or collect items) and Java (stream `filter`, `removeIf`). SQL `WHERE` clauses, validation annotations and early-return `if`s are not compared | `tests/test_testscope.py` (including false-positive guards) |
| B3 Shared-components report | **Done** (API and CLI) | `tests/test_testscope.py` |
| B4 Tester-facing output | **Done**: report `test_scope`, PR comment "What to test", report page, Knowledge Fabric chunks and paths, redaction per reader | `tests/test_testscope.py` |
| B5 Method change classification | **Done** (Python and Java signatures; docstring-only changes recognised in Python) | `tests/test_testscope.py` |
| B6 Demo application | **Done**: `fixtures/build_lanes_fixture.py`, built on the existing payments test landscape. A shared `payments-service`, a statement PDF service that leaves out disputed payments, and a payments-screen backend that does not. Six change branches | `python tools/demo.py --landscape lanes --branch behaviour` (also `move_rule`, `signature`, `signature_unfixed`, `docstring`, `contract`) |

**Full test suite with these changes (2026-11-02):** 988 passed, 1 skipped, 3 failed, 2 errors. Then fixed and re-run:
- **2 report-page tests** counted the page's tables and needed the new "What to test" table added.
- **1 docs test** read a `tia-services.yaml` example in the user guide as an API route; the example was reworded.
- **2 observability tests** errored while setting up their git remotes on a heavily loaded machine; they did not fail an assertion. They pass when re-run.

All 79 tests in those three files pass on re-run, so no failure is known. A single clean full run after the fixes has not been done.

### UC-2 and UC-3 (built 2026-11-03)

**Verification level:** verified (simulated), on our own sample applications only.

The demo landscape (`fixtures/build_lanes_fixture.py`) now also has:
* a payment-creation API with real rules (amount > 0 and <= 10000; currency EUR/USD/GBP; reference of 1-18 characters from A-Z0-9; account id of 1-9 digits). It answers 404 for an unknown account and 409 for a duplicate reference;
* automated tests that cover part of this;
* written test cases (`customer-portal/testcases/portal-payments.csv`, `statement-service/testcases/statement.yaml`);
* an Angular payments page and a React statement page.

| Use case | What a tester gets | Where |
|---|---|---|
| UC-2 technical coverage | For each endpoint and page, its technical aspects with covered / partial / missing and the tests behind each. In the sample: **41% of 35 aspects covered**. Missing: an invalid amount or reference, a missing currency, the 409 duplicate, GBP payments, the status filter values. Written test cases that match nothing are listed | `GET /api/v1/coverage`, `POST /api/v1/coverage/test-case`, `tia coverage` |
| UC-3 API inventory | 5 endpoints. For the payment API: its 6 inputs with their rules, valid and invalid values, a 19-case test matrix, and errors 404/409 | `GET /api/v1/automation/api`, `tia automation` |
| UC-3 UI inventory | 2 pages, 14 elements. Each has a unique locator, except 2 "Export" buttons that share their text (flagged as fragile); mandatory flags; dropdown values (including a list kept in the component); the APIs each page calls | `GET /api/v1/automation/ui`, `tia automation --ui` |

**Tests:** `tests/test_tester_views.py` (25 tests), plus false-positive guards:
* a provider's tests do not count as exercising a consumer;
* a test that leaves out one field does not count as testing an invalid value of another;
* a written case about a page does not count for an API with the same path;
* markup in comments is not read as elements.

**Limits:**
* Matching is static and textual: "covered" means addressed, not passing.
* Automated UI tests (Playwright/Cypress) are not read yet.
* Request bodies sent by Java tests are not read, so only their field names count.
* A dropdown filled from an API shows as dynamic.

**Status (2026-11-03):** all three use cases are built and verified (simulated), on our own sample applications only.

**Next:**
1. Run all three on a real code base and its real test cases, and review the results with testers.
2. Read automated UI tests (Playwright/Cypress) and request bodies in Java tests.
3. Pass the coverage and automation views through the Knowledge Fabric retriever.

## Appendix A: full transcript

Machine transcription of the recording (`WhatsApp Audio 2026-11-02 at 17.17.20.mp4`, 17 min) by a local speech-to-text model, lightly corrected:
- "5G and 5G systems" → "swipe-in, swipe-out systems"
- "AI ID" → "AI IDE"
- "Amazon Kero" → "Amazon Kiro"
- "key card" → "UI", where the meaning is clear
- "KIKA" → "Keka", the HR application used as an example

Timestamps are minutes:seconds. Other wording may contain transcription errors.

> **[00:00]** Now if there is a change in Keka application, I am telling you, you know, in Keka let's say a particular module or somebody changed the format of PDF, the way it is generating the work anniversaries, or let's say they are introducing a feature where it will show it in the UI itself, all the work anniversaries and birthdays. Then I know for a fact that it is, you know, interacting with the CRM, I know for a fact that it is interacting with some other swipe-in, swipe-out systems, and it's PDF, right? So now because it's a work anniversary module, functionally I know I should make sure that work anniversary displayed on the UI is same as that is generated in the PDF. So all that Pulse is able to do now. — Yes. — Okay, because it's all the documentation.
>
> **[00:53]** But what Pulse is not able to do is, let's say because of the work anniversary changes you also made an architectural change, that both now the UI as well as the PDF generation API, both of them call a common endpoint to retrieve the list of employees having the, you know, current month as the work anniversary month, right? Let's say that's a change that you did. Earlier the PDF API was directly hitting the DB and doing it. Now you realize no, no, no, it is also required in UI, so I will bring in one more layer which will be directly in the microservices, which will also retrieve the specific records for me, and then that is internally called by those APIs, business API, okay? — Okay. — This information Pulse doesn't have because it doesn't read the code. Now if there is any change in the microservice, now I have to test both, something I won't know, because for a tester it's a black box, right?
>
> **[02:06]** Maybe there is also some logic written that, you know, anybody who is serving notice, you don't have to include their names in the work anniversary. Let's say hypothetically that's a business case, business rule. That rule you have implemented in API, right? — Yes. — For the PDF but not for the UI, correct? So I have to test manually; it's all retrospective validation that we have to do and find out the differences. But if we have code access, if now Pulse is able to look at the code base, we should be able to very clearly say: hey, you know what, while you have created a common method which both your UI and PDF calls, unfortunately this particular rule is not in your UI at all, because it's only directly written in the PDF side. So these are all additional insights that we can get by reading the code. You get it, right? — Yes, yes.
>
> **[03:27]** This is what we are trying to achieve now with Lane zero. Because it's underlying, we don't know how it is implemented. Now we are looking at the implementation aspects and we are able to improve the quality of the product by giving additional information, like: hey, you know what, there is a function in the PDF lane that removes all the people serving notice period; in the UI lane it is directly calling the microservice, and PDF will also be calling the microservice but UI is directly printing. Why there is a defect: because UI doesn't have the business logic built. Now the developer will make that change, take it from PDF and make it a generic method. Let's say right now at the moment that PDF filtering of the employees in notice is made as a generic method. Now both these get impacted, right? UI also gets impacted, PDF also gets impacted. So now Pulse has to generate that insight, say: you know what, now both these methods are calling this reusable, very common code, hence please validate both — validate your UI as well as your PDF generation. So that's what we are trying to achieve in Lane zero. — Okay, understood.
>
> **[05:00]** From a business standpoint, Arthur, you have to understand this. Otherwise all the demos saying, you know, "I'm able to…" — we are not worried about, you know, how UI works, what are the various sources that you can pick. While those are all technical requirements for your Lane zero, are you serving the business purpose is something we are not able to validate. So for that your demo also has to be aligned in the way that I told you now, right? It is able to take code from ADO, it is able to take from GitHub — yeah, that's good, that is a necessity, okay, that is the, you know, what do you say, the P0 expectation. But what is the business objective of you doing all this? Essentially you are opening up the entire system code base, dissecting it and giving out information to our testing community, saying: these are the different ways the code is implemented, and because there is a change in the requirement, because you are now creating test cases for a particular system, while you are creating everything from the requirement you should also consider these specifics from the code: there are 25 common methods, 25 common endpoints used across the system; any change into these methods has a significant impact on the system. These are all insights that we want to give out from the Lane zero ingestion, okay?
>
> **[06:56]** While all this has to be done, while there is a significant amount of intelligence activity, what should your Lane zero ingestion be supporting? That is the question that we have to answer in the demo. We should say, to take the same example — yes — now I'm going to talk to you about three use cases.
>
> **[07:16]** One: during test case design I want to tell testers what is the change impact from the code standpoint. So now let's assume there is a change in this particular method. Developer is making change in this particular method; this method is used in these three places; these are the different files used, and these files correspond to these particular business nodes. Hence, from the technical impact analysis perspective, whenever there is a change in this method, for whatever may be the requirement, the retriever will be able to pull these three nodes as the impacted area from the technical impact analysis. That should be the demonstration for a change.
>
> **[08:15]** Next: there is no change. Now the tester is blindly asking the question: can you tell me if my test case repository is sufficient? That's a functional coverage agent that Pratik is building now. Right, so that's the second use case for you. Now, I want to tell whether my test cases are sufficient to validate all my business rules, business requirements — that we are able to successfully do. Now, will I be able to do the same on the technical side? Here are the 25 technical aspects that this case should ideally cover. Now if I get a test case as a query — is my test case really covering all aspects or not — this is the way that ingested code can help in establishing the coverage. That should be your second use case demo. You got it, right? — Yes, yes.
>
> **[09:24]** And then the third use case demo should be: okay, now the team is trying to automate the test cases. For automation of test cases, what is needed? They want to see what are the API endpoints; under API endpoints, what are the mandatory, non-mandatory fields which are there; what are those fields which can take multiple values; what are the various restrictions, rules in that API request. There may be, you know, some field which will take zero to nine digits; there may be only Boolean values; some of them may be multi-select boxes; some of the attributes in that request can take only, you know, five valid values. There could be multiple rules, right? — Yes, yes. — So now, based on that, when you're automating it, you consider all these permutation combinations: here is the input API endpoint, here are the valid and invalid values, here are the rules that are there. Okay. Next, automation from UI: here are the set of UI elements that are captured; these are mandatory, these are non-mandatory; this is the unique way to identify the UI element. This is the information needed from Lane zero. These are some two, three use cases, right?
>
> **[11:08]** Now, what is not needed, I can tell you. A function changed. Now this function is called in three other places, right? Okay. If you stop that discussion there, it is absolutely no value to me. — Correct, correct. — Okay. Because a function getting changed and all other three, you know, places where this function is called is an insight I will directly get from my AI IDE. The developer himself will get it. Even though you are, because you are developing, you would also know, right? When I open my Amazon Kiro and I see, like, if I'm making a change in a method, it will clearly tell me, you know, what are those places it is already implemented, and all that it will tell me, especially AI. I'm talking about just IDE, AI also in that. Then I can even find out, you know, what is the impact, should I, you know, will there be a change — all that I can find out. That's why that much only context won't help me. — Okay, okay.
>
> **[12:24]** Right. For instance, what are the dependencies required for this particular file to work? I don't care. Let's say there is an include method for various libraries — why will I care about it? I don't need to know it. — Okay. — The exact logic of how a rule is implemented, I don't want to know initially, but later during deep dive maybe, but not right now. I only want to know whether that rule is implemented or not. That's all I'm worried about right now. Right. So those are all things which an AI IDE by default does, or an IDE by default does, and are of less value to me, because that anyway the developer will take care.
>
> **[13:31]** What is my job role? What is my JD, job description? It is: I want to make sure the product is of highest quality. For that, I want to know whether all my requirements are met; a change in some place — what is the significant impact of that change in other places; can I automate my tests faster, right; have I considered all permutation combinations in my automation. Let's say already I have test cases — I inherit some 100,000 test cases from somebody. Now, is it covering every aspect in the implementation in the code? These are all my problem statements that we need to make sure Pulse addresses. It's not the other way.
>
> **[14:30]** Yeah. So from this perspective you think, Arthur, and then you see what is needed — top down this time. Okay. You pick one use case. You pick the use case of, let's say, change impact only: there is a small change, now how it is impacting and how are you connecting it to the business requirement — that you pick. Or you pick from an automation standpoint: what are those APIs, what is the insight that you have captured about that API, what are those UI elements, how do I identify that UI element uniquely — that is called, in the automation world, a locator. So now with Pulse looking at the code, if it is able to derive the page, and then under that page what are the UI elements, under the UI element how do I uniquely identify each UI element, then we are good. Then we have saved some 20% of automation testers' effort. That's the second use case for you.
>
> **[15:53]** So now when you're reading the code, if you're structured in such a way that, you know, I can uniquely identify each page, each of those UI elements, and then for each element I can also find out how do I locate those elements and whether it's a mandatory or a non-mandatory field; if it is a dropdown, let's say, what are the possible values in the dropdown — if you are able to identify that from the code, then we have solved a big problem for the automation. Okay. — Okay. — So like that, from a use case, I've explained you two, three use cases. You pick one use case. You tell me which use case you want to pick, and for that use case then we will discuss on a day-to-day basis and see how to make sure that your Lane zero works. — Awesome. Okay, sure. — Yeah. Thank you so much. — You're welcome. Okay, sir. We'll connect tomorrow. — Sure.
