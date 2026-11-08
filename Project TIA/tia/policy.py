"""Release policy (PRD FR-033 configurable rules, FR-034 evaluation, FR-036 advisory and enforcement modes, AC-013, AC-014, AC-030).

Configured per tenant (versioned like every setting):
    "policies": {"mode": "advisory" | "enforcing",
                 "rules": [{"id": "critical-no-breaking", "type": "no_breaking_change", "mandatory": true,
                            "applies_to": {"criticality": ["critical"]}, "params": {}}]}

Rule types (FR-033):
  no_breaking_change          no material finding on a path with a breaking change
  tests_passed                every relevant test (recommended, priority <= params.max_priority, default 2) passed in CI on the analysed revision
  contract_validated          every material contract or event consumer has a test that validated the contract in a passing run
  owner_approval              every team owning a materially affected component has approved
  no_unresolved_dependencies  no unresolved outbound calls, stale relationships or consumers that cannot be seen
  analysis_complete           the analysis is complete (complete_with_gaps needs review; partial or failed fails)
  production_evidence         the proposed revision is deployed in params.environment (default staging) and its tests passed

Each rule yields pass | fail | not_evaluated | requires_review, with the evidence it is based on. A missing data source never yields pass:
without CI results a test rule is not_evaluated (AC-014). The mode decides only what is reported to the repository protection process:
advisory never blocks (the default); enforcing reports a failed MANDATORY rule as a failure (AC-030). TIA never changes protection settings.
"""
from __future__ import annotations

from typing import Optional

RULE_TYPES = ("no_breaking_change", "tests_passed", "contract_validated", "owner_approval", "no_unresolved_dependencies",
              "analysis_complete", "production_evidence", "teams_acknowledged")
MODES = ("advisory", "enforcing")
OUTCOMES = ("pass", "fail", "not_evaluated", "requires_review")
UNRESOLVED_AREAS = ("unresolved_outbound_calls", "stale_relationships", "event_consumers_unknown", "inaccessible_repositories")


def validate(raw, problems: list) -> Optional[dict]:
    """The `policies` setting -> normalised {mode, rules}, appending every problem found."""
    if not isinstance(raw, dict):
        problems.append("policies must be an object with mode and rules")
        return None
    for k in raw:
        if k not in ("mode", "rules"):
            problems.append(f"unknown setting 'policies.{k}'")
    mode = raw.get("mode", "advisory")
    if mode not in MODES:
        problems.append(f"policies.mode must be advisory or enforcing (got {mode!r})")
    rules, ids = [], set()
    for i, r in enumerate(raw.get("rules") or []):
        where = f"policies.rules[{i}]"
        if not isinstance(r, dict):
            problems.append(f"{where} must be an object")
            continue
        for k in r:
            if k not in ("id", "type", "mandatory", "applies_to", "params", "description"):
                problems.append(f"unknown setting '{where}.{k}'")
        rid, typ = r.get("id"), r.get("type")
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 80:
            problems.append(f"{where}.id must be a short name")
        elif rid in ids:
            problems.append(f"{where}.id '{rid}' is used twice")
        if typ not in RULE_TYPES:
            problems.append(f"{where}.type must be one of {', '.join(RULE_TYPES)} (got {typ!r})")
        if not isinstance(r.get("mandatory", True), bool):
            problems.append(f"{where}.mandatory must be true or false")
        ap = r.get("applies_to") or {}
        if not isinstance(ap, dict) or any(k not in ("criticality", "services") for k in ap) or \
                any(not isinstance(v, list) or not all(isinstance(x, str) for x in v) for v in ap.values()):
            problems.append(f"{where}.applies_to may hold criticality and services, each a list of names")
        pr = r.get("params") or {}
        if not isinstance(pr, dict):
            problems.append(f"{where}.params must be an object")
        elif typ == "tests_passed" and "max_priority" in pr and pr["max_priority"] not in (1, 2, 3):
            problems.append(f"{where}.params.max_priority must be 1, 2 or 3")
        elif typ == "production_evidence" and "environment" in pr and not isinstance(pr["environment"], str):
            problems.append(f"{where}.params.environment must be an environment name")
        if isinstance(rid, str):
            ids.add(rid)
        rules.append({"id": rid, "type": typ, "mandatory": bool(r.get("mandatory", True)), "applies_to": ap if isinstance(ap, dict) else {},
                      "params": pr if isinstance(pr, dict) else {}, **({"description": str(r["description"])[:300]} if r.get("description") else {})})
    if len(rules) > 100:
        problems.append("policies.rules may hold at most 100 rules")
    return {"mode": mode, "rules": rules}


def _applies(rule: dict, report: dict, criticality: dict) -> bool:
    ap = rule.get("applies_to") or {}
    if not ap:
        return True
    target = report["change"].get("service")
    if ap.get("services") and target not in ap["services"]:
        return False
    if ap.get("criticality"):
        comps = {target} | {f["component"] for f in report.get("findings", []) if f.get("material")}
        return any(criticality.get(c) in ap["criticality"] for c in comps)
    return True


def _result(rule, outcome, reason, evidence=None):
    return {"id": rule["id"], "type": rule["type"], "mandatory": rule["mandatory"], "outcome": outcome, "reason": reason,
            "evidence": evidence or [], **({"description": rule["description"]} if rule.get("description") else {})}


def evaluate(report: dict, policies: Optional[dict], criticality: dict, owners: dict, approvals: list, coordination: Optional[dict] = None) -> dict:
    """-> {"mode", "outcome", "blocking", "results"}. approvals: [{"owner", "decision": approve|reject, "by", "at"}]; coordination: the
    coordination status (tia.coordination.status) for teams_acknowledged, None when it is not known (at analysis time)."""
    policies = policies or {"mode": "advisory", "rules": []}
    results = []
    for rule in policies["rules"]:
        if not _applies(rule, report, criticality):
            results.append(_result(rule, "pass", "The rule does not apply to this change."))
            continue
        arg = coordination if rule["type"] == "teams_acknowledged" else approvals
        results.append(RULES[rule["type"]](rule, report, criticality, owners, arg))
    order = ["fail", "not_evaluated", "requires_review", "pass"]
    worst = min((r["outcome"] for r in results), key=order.index) if results else None
    blocking = policies["mode"] == "enforcing" and any(r["outcome"] == "fail" and r["mandatory"] for r in results)
    return {"mode": policies["mode"], "outcome": worst, "blocking": blocking, "results": results}


# ---- the rules ------------------------------------------------------------------------------------------------------------------------
def r_no_breaking(rule, report, crit, owners, approvals):
    if report["status"] == "failed":
        return _result(rule, "not_evaluated", "The analysis failed, so whether the change is breaking is unknown.")
    bad = [f for f in report.get("findings", []) if f.get("material") and f.get("change_nature") == "breaking"]
    ap = (rule.get("applies_to") or {}).get("criticality")
    if ap:
        bad = [f for f in bad if crit.get(f["component"]) in ap or crit.get(report["change"].get("service")) in ap]
    if bad:
        return _result(rule, "fail", f"{len(bad)} material finding(s) on a path with a breaking change.",
                       [{"finding": f["id"], "component": f["component"], "reason": f["reason"]} for f in bad[:20]])
    if report["status"] == "partial":
        return _result(rule, "requires_review", "No breaking change was found, but the analysis is incomplete.")
    return _result(rule, "pass", "No material finding on a path with a breaking change.")


def r_tests_passed(rule, report, crit, owners, approvals):
    mp = (rule.get("params") or {}).get("max_priority", 2)
    tests = [t for t in (report.get("test_recommendations") or {}).get("tests", []) if t["priority"] <= mp]
    if not tests:
        return _result(rule, "requires_review", "No relevant test evidence found in the analysed sources.")
    res = {(t["repo"], t["file"], t["test"]): t.get("last_result") for t in tests}
    if all(r in (None, "no_ci_results") for r in res.values()):
        return _result(rule, "not_evaluated", "No CI results are available for the relevant tests, so the rule cannot be evaluated.",
                       [{"test": k[2], "repo": k[0]} for k in res][:20])
    failed = [k for k, r in res.items() if r in ("failed", "error")]
    if failed:
        return _result(rule, "fail", f"{len(failed)} relevant test(s) failed.", [{"test": k[2], "repo": k[0], "result": res[k]} for k in failed][:20])
    other = [k for k, r in res.items() if r in (None, "no_ci_results", "not_in_run", "skipped")]
    if other:
        return _result(rule, "requires_review", f"{len(other)} relevant test(s) have no passing result in CI.",
                       [{"test": k[2], "repo": k[0], "result": res[k] or "no_ci_results"} for k in other][:20])
    stale = [t for t in tests if t.get("states") and "passed" in t["states"] and not _same_revision(report, t)]
    if stale:
        return _result(rule, "requires_review", f"{len(stale)} relevant test(s) passed on another revision only.",
                       [{"test": t["test"], "repo": t["repo"]} for t in stale][:20])
    return _result(rule, "pass", f"All {len(tests)} relevant test(s) passed in CI.", [{"test": k[2], "repo": k[0]} for k in res][:20])


def _same_revision(report, rec) -> bool:
    for e in (report.get("test_scope") or {}).get("entry_points", []) + [{"tests": f.get("tests", [])} for f in report.get("findings", [])]:
        for t in e.get("tests", []):
            if t["name"] == rec["test"] and t["repo"] == rec["repo"]:
                return bool((t.get("run") or {}).get("of_analysed_revision"))
    return True


def r_contract_validated(rule, report, crit, owners, approvals):
    cons = [f for f in report.get("findings", []) if f.get("material") and f.get("impact_type") in ("contract", "event")]
    if not cons:
        return _result(rule, "pass", "No material contract or event consumer is affected.")
    if all(t.get("result") in (None, "no_ci_results") for f in cons for t in f.get("tests", [])):
        return _result(rule, "not_evaluated", "No CI results are available for the affected consumers' tests.",
                       [{"finding": f["id"], "component": f["component"]} for f in cons][:20])
    missing = [f for f in cons if not any(t.get("validated_contract") for t in f.get("tests", []))]
    if missing:
        return _result(rule, "fail", f"{len(missing)} affected consumer(s) have no test that validated the contract in a passing run.",
                       [{"finding": f["id"], "component": f["component"], "tests": [t["name"] for t in f.get("tests", [])]} for f in missing][:20])
    return _result(rule, "pass", f"Every affected consumer ({len(cons)}) has a test that validated the contract.")


def r_teams_acknowledged(rule, report, crit, owners, coordination):
    """FR-045: every team owning a materially affected component has acknowledged the change."""
    from .coordination import teams_to_notify
    teams = teams_to_notify(report, owners)
    if not teams:
        return _result(rule, "pass", "No other team owns a materially affected component.")
    if coordination is None:
        return _result(rule, "requires_review", f"{len(teams)} team(s) must acknowledge: {', '.join(teams)}.",
                       [{"team": t} for t in teams])
    waiting = [t for t in coordination["teams"] if t["status"] != "acknowledged"]
    if not waiting:
        return _result(rule, "pass", f"All {len(coordination['teams'])} affected team(s) acknowledged.",
                       [{"team": t["team"], "by": (t["acknowledged"] or {}).get("by")} for t in coordination["teams"]])
    failed = [t for t in waiting if t["status"] == "notification_failed"]
    if failed:
        return _result(rule, "not_evaluated", f"{len(failed)} team(s) could not be notified, so their acknowledgement cannot be expected.",
                       [{"team": t["team"], "detail": (t["notification"] or {}).get("detail")} for t in failed])
    return _result(rule, "requires_review", f"{len(waiting)} team(s) have not acknowledged yet: {', '.join(t['team'] for t in waiting)}.",
                   [{"team": t["team"], "status": t["status"]} for t in waiting])


def required_owners(report: dict, owners: dict) -> list:
    """Teams owning a materially affected component other than the change's own team."""
    mine = owners.get(report["change"].get("service"))
    return sorted({owners[f["component"]] for f in report.get("findings", []) if f.get("material") and owners.get(f["component"])
                   and owners[f["component"]] != mine})


def r_owner_approval(rule, report, crit, owners, approvals):
    need = required_owners(report, owners)
    if not need:
        unknown = sorted({f["component"] for f in report.get("findings", []) if f.get("material") and not owners.get(f["component"])})
        if unknown:
            return _result(rule, "requires_review", "Owners are not known for: " + ", ".join(unknown) + ".", [{"component": c} for c in unknown])
        return _result(rule, "pass", "No other team owns a materially affected component.")
    latest = {}
    for a in sorted(approvals, key=lambda a: (a.get("at") or "", a.get("evidence_id") or 0)):      # the latest decision wins
        latest[a["owner"]] = a
    rejected = [o for o in need if latest.get(o, {}).get("decision") == "reject"]
    if rejected:
        return _result(rule, "fail", "Rejected by " + ", ".join(rejected) + ".", [latest[o] for o in rejected])
    pending = [o for o in need if latest.get(o, {}).get("decision") != "approve"]
    if pending:
        return _result(rule, "requires_review", "Waiting for approval from " + ", ".join(pending) + ".", [{"owner": o} for o in pending])
    return _result(rule, "pass", "Approved by " + ", ".join(need) + ".", [latest[o] for o in need])


def r_unresolved(rule, report, crit, owners, approvals):
    hit = [u for u in report.get("unknowns", []) if u["area"] in UNRESOLVED_AREAS]
    if hit:
        strict = (rule.get("params") or {}).get("strict")
        return _result(rule, "fail" if strict else "requires_review", "Dependencies that could not be resolved: " + ", ".join(u["area"] for u in hit) + ".",
                       [{"area": u["area"], "reason": u["reason"]} for u in hit])
    return _result(rule, "pass", "No unresolved dependency was reported.")


def r_complete(rule, report, crit, owners, approvals):
    st = report["status"]
    if st == "complete":
        return _result(rule, "pass", "The analysis is complete.")
    if st == "complete_with_gaps":
        areas = [u["area"] for u in report.get("unknowns", []) if u.get("affects_confidence")]
        return _result(rule, "requires_review", "The analysis has unknowns that limit confidence: " + ", ".join(areas) + ".", [{"area": a} for a in areas])
    return _result(rule, "fail", f"The analysis is {st}.")


def r_production(rule, report, crit, owners, approvals):
    env = (rule.get("params") or {}).get("environment", "staging")
    dep = report.get("deployments")
    if not dep:
        return _result(rule, "not_evaluated", f"No deployment records are available for {env}.")
    mine = dep["by_repo"].get(report["change"]["repository"], {}).get(env)
    if not mine:
        return _result(rule, "not_evaluated", f"No deployment record of {report['change']['repository']} in {env}.")
    if not mine.get("is_proposed_revision"):
        return _result(rule, "fail", f"{env} runs {mine['revision'][:7]}, not the proposed revision.", [mine])
    tests = r_tests_passed({**rule, "params": {"max_priority": 2}}, report, crit, owners, approvals)
    if tests["outcome"] != "pass":
        return _result(rule, tests["outcome"], f"The proposed revision is deployed in {env}, but: {tests['reason']}", tests["evidence"])
    return _result(rule, "pass", f"The proposed revision is deployed in {env} and its relevant tests passed.", [mine])


RULES = {"no_breaking_change": r_no_breaking, "tests_passed": r_tests_passed, "contract_validated": r_contract_validated,
         "owner_approval": r_owner_approval, "no_unresolved_dependencies": r_unresolved, "analysis_complete": r_complete,
         "production_evidence": r_production, "teams_acknowledged": r_teams_acknowledged}


def check_conclusion(policy: Optional[dict]) -> str:
    """GitHub check run conclusion: advisory (or no policy) is always neutral; enforcing reports what the mandatory rules say."""
    if not policy or policy.get("mode") != "enforcing" or not policy.get("results"):
        return "neutral"
    mand = [r for r in policy["results"] if r["mandatory"]]
    if any(r["outcome"] == "fail" for r in mand):
        return "failure"
    if any(r["outcome"] in ("not_evaluated", "requires_review") for r in mand):
        return "action_required"
    return "success"


def ado_state(policy: Optional[dict]) -> str:
    return {"neutral": "notApplicable", "failure": "failed", "action_required": "pending", "success": "succeeded"}[check_conclusion(policy)]
