# Demo data: what to type in each section, and what you should see

This page is for the person running a TIA demo on their own machine. For every section of the web page it gives the exact inputs and the
result you should get. Every number here was measured on 2026-11-09, and `tests/test_demo_seed.py` checks them on every test run, so
they stay true while the code changes.

The demo landscape is the payments story of [CASE-STUDY.md](CASE-STUDY.md):
- **payments-service** holds the payments.
- **statement-service** makes the monthly PDF and leaves disputed payments out.
- **customer-portal** shows every payment on screen.

Both statement-service and customer-portal call payments-service.

## 0. Start a clean demo (once, about 1 minute)

**Terminal 1** starts TIA with the demo repositories:

```
python tools/run_dev.py --demo --landscape lanes --data demo-data/lanes
```

**Terminal 2** loads the demo evidence and writes the two upload zips:

```
python tools/demo_seed.py
```

Open http://127.0.0.1:8080.

The seed script adds what a CI pipeline, a deployment tool and an incident system would send:
- **Two releases.** 2026.10 contains payments-service and statement-service. In 2026.11, customer-portal joins.
- **A CI run of payments-service:** 3 tests passed, 1 failed.
- **Deployments to staging and production,** from 1 to 3 November 2026.
- **Production traffic** from the two known consumers, plus one TIA cannot see in code: **mobile-app**.
- **Incident INC-1001:** "Statement PDF totals are wrong", on statement-service in production, 3 November 12:00 UTC.
- **`demo-data/inputs/loyalty-service-v1.zip` and `-v2.zip`,** for the upload in section 1.

Rules:
- **Clean start:** stop the server, delete `demo-data/lanes`, and run both commands again. Running the seed twice adds the
  evidence twice, so counts in sections 5a and 7 grow.
- **Follow the order of this page.** Sections 6 and the upload (1b) change later results, so they come last.
- **No sign-in in this mode.** The local server runs without sign-in, so the "Sign in" box never appears and you act as an
  administrator.

## 1. Ingest repositories

The table already lists **customer-portal**, **payments-service** (7 branches) and **statement-service**. Nothing to type yet; the
upload comes at the end, in step 1b.

Tabs that need a real Git host and cannot be shown offline:
- **Git URL:** any public GitHub URL works without a token.
- **Whole ADO project / GitHub org:** needs a PAT.

## 2. Code graph

| Level of detail | Repositories | You should see |
|---|---|---|
| Services | nothing selected | 3 boxes and 2 arrows: customer-portal → payments-service ← statement-service |
| Endpoints & schemas | nothing selected | 15 nodes and 14 edges; the arrows end on `GET /accounts/{}/payments` |
| Functions, call sites, tests | nothing selected | 44 nodes and 40 edges; the full chain from `account_payments` and `statement_rows` down to `ordered` |
| Services | only statement-service | 1 box and no arrows. This is the "island": selecting one repository hides the others |

A grey dotted edge from `payments.component.ts` is expected. The Angular page calls a relative address, so TIA marks it unresolved
instead of guessing.

## 3. Analyse a change

For every run:
- **Repository** = `payments-service`
- **Base** = `main` (or leave it empty)
- **Pull request number** and **Token** stay empty.

Only **Head** changes.

| Head | What the developer did | Findings |
|---|---|---|
| `behaviour` | the list is now sorted newest first | **6**: 5 medium, 1 low |
| `contract` | removed the `disputed` field from the answer | **5 high** |
| `signature_unfixed` | added a required parameter and forgot the caller | **5 high, 1 low** |
| `signature` | the same change, with the caller fixed | **5 medium, 1 low** |
| `move_rule` | moved the "no disputed payments" rule into payments-service | **5 medium** |
| `docstring` | changed a comment only | **0**: "nothing to retest" |

Every run shows the status **complete with gaps**. That is honest, not an error: the unresolved Angular call, and consumers outside
the analysed repositories, are listed under **Unknowns**.

## 4. Change Impact Report

### After `behaviour`
- **What to test** lists 3 entry points:
  - **Payments screen** (customer-portal), test `test_portal_payments`;
  - **Monthly statement PDF** (statement-service), test `test_statement_pdf`;
  - `GET /accounts/{account_id}/payments` itself, tests `test_list_payments` and `test_unknown_account_is_404`.
- **Rule gap:** the PDF keeps only payments where `not p['disputed']`, but the Payments screen does not.
- **mobile-app** appears as a finding with evidence *runtime observed*, and under "Without test evidence". TIA knows about it only
  from production traffic, not from code.
- **Recommended tests:** `test_list_payments` and `test_unknown_account_is_404` show **passed**, from the CI run. The other two show
  `no_ci_results`.
- **Deployments** shows what runs in staging and production.

### After `contract`
- **Filters:**

  | Filter | Rows |
  |---|---|
  | Severity = `high` | 5 |
  | Reach = `direct` | 3 |
  | Service = `statement-service` | 2 |
  | Impact = `contract` | 3 |

- **Team coordination** lists **team-portal** and **team-statements**, both *pending*.
- **Notify the teams** reports *2 failed*: "no notification channel is configured". That is expected locally, because no email or chat
  is connected.
- **Acknowledge for team:** choose `team-portal`, type the comment `We will adapt the screen first`, and press **Acknowledge**.
  team-portal turns *acknowledged*.
- **Reviewer verdict:**
  - pick the first finding, choose `confirmed`, and record it;
  - pick the third finding, choose `false positive`, and record it.

  These two verdicts feed section 5b.
- **PR comment (markdown)** begins "TIA impact analysis - complete_with_gaps … 5 finding(s), 5 material - 5 high", followed by the
  What to test table marked **BREAKING**.

## 5. Tester views

Nothing to type in the first four tabs; press **Refresh**.

### Common components & rule gaps
- **Common components:** `list_payments`, `load_payments` and `ordered` are each used by 3 entry points.
- **Rule gaps:** 1 gap, the disputed-payments rule.

### Technical coverage
**41.4%** of 35 aspects covered: 13 covered, 3 partial and 19 missing.

| Entry point | Covered | Missing (examples) |
|---|---|---|
| `POST /accounts/{account_id}/payments` | 3 of 11 | amount limits, currency outside EUR/USD/GBP, reference format, answer 409 |
| `GET /accounts/{account_id}/payments` | 2 of 4 | the `status` filter values |
| Payments page `/accounts/:id/payments` | 3 of 8 | invalid Amount, empty Currency, empty reference |
| Statement page `/statements` | 2 of 5 | empty Account number, empty Month |
| `POST /portal/accounts/{account_id}/payments` | 0 of 4 | not tested at all |

Unmatched written case: **TC-204** "Old statements are archived". It names no entry point.

### Automation: APIs
5 endpoints, 14 inputs, **29 generated cases**. Open `POST /accounts/{account_id}/payments`:

| Input | Valid values | Invalid values |
|---|---|---|
| amount | `0.01`, `10000` | left out, `0`, `10000.01`, `abc` |
| currency | `EUR`, `USD`, `GBP` | left out, `NOT_A_CURRENCY` |
| reference | `A`, 18 × `A` | left out, `!@#`, empty, 19 × `A` |

The endpoint's error answers are **404** and **409**.

### Automation: pages & locators
2 pages, 14 elements, **2 without a unique locator**:
- the two **Export** buttons on the Payments page. They can only be found by position (`button:nth-of-type(3)` and `(5)`), which is
  fragile.
- Note: **Details** is unique only within its table row.

### Check one test case
Type each row, then press **Check coverage of this test case**.

| Title | Entry point | Steps | Expected result | You should see |
|---|---|---|---|---|
| Amount above the limit | `POST /accounts/{id}/payments` | Send a payment with amount 10001, currency EUR and reference INV778 | The service refuses it with 422 | covers 40.9%, including "an invalid 'amount' is rejected (<= 10000; > 0)" |
| Duplicate reference | `POST /accounts/{id}/payments` | Send the same payment twice with reference INV779 | The second one is refused with 409 duplicate reference | covers 18.2%, including "answers 409 (duplicate reference)" |
| Pay in GBP | `POST /accounts/{id}/payments` | Send a payment for account 123 with amount 25, currency GBP and reference INV777 | The payment is created (201) | covers 13.6%: the call and the currency values |
| Pay without amount on the screen | `/accounts/:id/payments` | Open the payments page; leave Amount empty; press Pay | The form refuses to submit | covers 25%, including "leaving 'Amount' empty is refused" |
| Order history | `GET /orders/{id}` | Open the order history | Orders are listed | no match: "does not name any analysed entry point or page" |

## 5a. Releases, environments and incidents

### Releases
1. Press **Refresh**: it lists 2026.10 and 2026.11.
2. **Compare from** `2026.10` to `2026.11`:
   - **Introduced (2):** customer-portal → `payments-service: GET /accounts/{}/payments` and `POST /accounts/{}/payments`;
   - **Removed:** none;
   - **Modified:** none.
3. To record a new release:
   - **New release name:** `2026.12-rc1`
   - **Revisions:** `{"payments-service": "behaviour", "statement-service": "main", "customer-portal": "main"}`
   - Press **Record release**. It answers "Release 2026.12-rc1 recorded (40 relationships)".

### What ran where
Type each date and time, then press **Show**.

| At (UTC) | You should see |
|---|---|
| `2026-10-31 12:00` | "No deployment recorded before that moment." |
| `2026-11-02 10:00` | customer-portal and statement-service in production; payments-service in **staging only** |
| `2026-11-03 12:00` | payments-service now in staging **and** production |

### Incidents
1. Press **Refresh incidents**: **INC-1001** appears.
2. Press **Investigate**. It shows 2 candidate changes in the 72-hour window. TIA never calls either one the cause:
   - statement-service: *time correlated*, deployed 52 h before the incident;
   - payments-service: *dependency derived*, "statement-service depends on payments-service".

## 5b. Insights

- **Show trends**, grouped by application, period month: `payments-service`, 2026-11, 6 changes, 1 breaking.
- **Group by team** shows the same row under `team-payments`.
- **Show rule precision:** `SEV-BREAKING-CONFIRMED`, judged 2, confirmed 1, false positive 1, precision 50% "(too few)". These come
  from the two verdicts in section 4.

## 7. Administration

| Part | Input | You should see |
|---|---|---|
| Usage and plan | **Show this month's usage** | 3 repositories, the analyses run so far, 17+ evidence records |
| Evidence held | Kind `deployment` → **List** | 4 records |
| Evidence held | Kind `runtime_edge` | 3 records |
| Evidence held | Kind `incident` | 1 record |
| Evidence held | Kind `release` | 2 records, 3 after step 5a |
| Environments | **Show what runs where** | payments-service in staging and production; the other two in production |
| Service-account tokens | Name `ci-pipeline`, Repositories `*`, Expires `90`, roles **viewer** and **engineer**, then **Create token** | a token starting `tia_`, shown once |

People and identity providers stay empty locally. They need sign-in and a real identity provider, so they are not part of this demo.

## 6. Configuration (after the rest: it changes later results)

### A. Import owners and criticality
In section 7, **Service catalog import**: Format `CSV`, then this content, then **Import as a new configuration version**.

```
service,owner,criticality
statement-service,team-statements,critical
customer-portal,team-portal,high
```

1. The import is saved as configuration version 1.
2. Section 6 now shows `"criticality": {"customer-portal": "high", "statement-service": "critical"}`.
3. Run section 3 with Head `contract` again. The findings change from 5 high to **2 critical + 3 high**: the two statement-service
   findings use rule `SEV-BREAKING-CRITICAL`.

### B. A setting that is refused, then one that is accepted
1. In the JSON box, change `"max_depth": 8` to `"max_depth": 0` and press **Validate**. The answer is "Not valid":
   *limits.max_depth must be between 1 and 50 (got 0)*.
2. Set it to `1`, type the comment `demo: direct consumers only`, and press **Save as new version**.
3. Run `behaviour` again. Only **1 finding (low)** remains: payments-service itself, because depth 1 stops before the consumers.
4. Restore the default: in the history, press **Restore as new version** on version 1, or set `max_depth` back to `8` and save.

## 1b. Upload a new service (finale: it adds a 4th service)

1. In section 1, open **Upload zip / folder**.
2. Choose `demo-data/inputs/loyalty-service-v1.zip` and press **Upload zip**. The answer reads "loyalty-service: 4 files stored as
   revision …".
3. In section 2, load Services: **4 boxes**. loyalty-service → payments-service was found automatically from the code.
4. Run `contract` again. loyalty-service is now among the affected services, along with customer-portal, statement-service and
   mobile-app.
5. Upload `loyalty-service-v2.zip` the same way. Version 2 doubles the loyalty points.
6. In the repository table, press **Analyse vs previous upload** on loyalty-service, then **Run analysis**:
   - 1 finding (low);
   - What to test: `GET /loyalty/{account_id}/points` with test `test_loyalty_points`.

## If your screen shows other numbers

| Cause | What happens | Fix |
|---|---|---|
| An old data folder | other repositories in section 1 | use `--data demo-data/lanes` and delete it for a clean start |
| No seed | `behaviour` gives 5 findings instead of 6 (no mobile-app), and sections 5a, 5b and 7 are empty | run `python tools/demo_seed.py` |
| Seed run twice | doubled evidence counts | clean start |
| Section 6 or 1b done earlier | changed severities or extra findings | clean start |
