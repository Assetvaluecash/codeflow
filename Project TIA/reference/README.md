# Reference landscapes (accuracy ground truth)

Each directory holds a `reference.json`: the **curated truth** for a landscape, written by reading the code, never by running TIA.
`python tools/accuracy.py reference/<name> [--source <checkout>] [--enforce]` measures TIA against it (PRD section 15.3).

| Reference | Language | Independent? | Needs |
|---|---|---|---|
| `spring-petclinic-microservices` | Java (Spring Boot, WebClient, RestClient, service discovery) | **yes**: third-party code, curated by reading it | a checkout at the pinned commit (see `source` in the file) |
| `piggymetrics` | Java (Spring Cloud: Feign, Zuul, Config Server) | **yes** | a checkout at the pinned commit |
| `synthetic-landscape` | Python (FastAPI, requests, generated client, declared contract) | **no**: written by the product's author together with its expected results | nothing (built from `fixtures/`) |

* The third-party sources are only **copied and parsed**; nothing in them is built, installed or executed. Clone them into a short path on
  Windows (`git -c core.longpaths=true clone ...`).
* A reference lists `dependencies` (each with the `file:line` that proves it), the provider `endpoints` that exist, `unresolvable` calls
  that must never be linked to a provider, and `changes` (a patch on a branch plus the components that must be reported as impacted).
* `config` states settings the landscape needs (e.g. the servlet context paths that live in external configuration); the harness also
  reports how much of the result depends on them.
* When the harness disagrees with the truth, **re-read the code before changing either**: the first draft of the petclinic truth
  mislabelled a call (GET instead of POST) and the harness exposed it.
* No independent **Python** multi-service reference has been found yet; Python accuracy is only covered by the (non-independent)
  synthetic landscape. Adding one is open work.
* Results are small samples (a handful of dependencies and changes each): they show the product works on real code and catch regressions,
  they are not a statistical guarantee. Every customer landscape must still be measured at onboarding (PRD section 18).
