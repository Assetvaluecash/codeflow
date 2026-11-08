"""Evidence enrichment of an analysis (PRD FR-022 to FR-029): CI results, coverage, runtime observations and deployments, read from TIA's
evidence store (never queried live) and pinned in the analysis's evidence snapshot.

* Runtime (FR-026/027, AC-007/008): an observed call corroborates a source-confirmed relationship, or adds a runtime-only one (strategy R6,
  evidence `runtime_observed`) labelled with environment, time window and versions. Absence of an observation never removes anything.
* Tests (FR-022/023, AC-027): each relevant test carries distinct states - exists, statically associated, passed/failed in a given run
  (and whether that run was of the analysed revision), executed the component (per-test coverage), validated the contract.
* Recommendations (FR-024) with the reason each test is relevant, and missing-test evidence (FR-025) in the PRD's wording.
* Deployments (FR-028/029, AC-029): per environment the revision deployed, distinguished from the analysed and the proposed revision.
"""
from __future__ import annotations

from typing import Optional

from .evidence import in_ranges, match_file, test_keys, tia_test_keys
from .graph import _path_matches
from .models import CORROBORATED, RUNTIME_OBSERVED, SOURCE_CONFIRMED, Edge

ENVIRONMENTS = ("development", "testing", "staging", "production")
SKIP_NOTE = ("Tests not listed are not shown to be safe to skip: the list is based on the evidence available, and a test with no graph path to "
             "the change may still be affected.")
NO_TEST = "No relevant test evidence found in the analysed sources"


# ---- runtime overlay (applied to the analysis's own copy of the graph) -------------------------------------------------------------------
def apply_runtime(g, extracts: dict, records: list) -> dict:
    """Adds runtime observations to the graph. -> {edge or node key: [observation]} for labelling findings."""
    eps_by_service: dict = {}
    for n, a in g.nodes.items():
        if a["type"] == "endpoint":
            eps_by_service.setdefault(a["service"], []).append(n)
    repo_of = {ex.service: r for r, ex in extracts.items()}
    labels: dict = {}
    for rec in records:
        e = rec["data"]
        obs = {"environment": e.get("environment") or rec.get("environment"), "from": e.get("from_ts"), "to": e.get("to_ts"),
               "count": e.get("count"), "caller_versions": e.get("from_versions") or [], "callee_versions": e.get("to_versions") or [],
               "source": rec["source"], "evidence_id": rec["id"]}
        target = next((n for n in eps_by_service.get(e["to_service"], []) if g.nodes[n]["method"] == e["method"]
                       and _path_matches(g.nodes[n]["path"], e["path"])), None)
        if target is None:
            continue                                     # an observed call to something TIA has not extracted: not invented as an endpoint
        caller_repo = repo_of.get(e["from_service"])
        existing = [x for _, x in g.impacted_by(target) if x.rel == "CONSUMES_API" and g.nodes.get(x.src, {}).get("service") == e["from_service"]]
        if existing:
            for x in existing:
                if x.status == SOURCE_CONFIRMED:
                    x.status = CORROBORATED
                labels.setdefault(("edge", x.src, target), []).append(obs)
            continue
        rid = f"runtime:{e['from_service']}->{target}"
        if rid not in g.nodes:
            g.add_node(rid, type="runtime_ref", repo=caller_repo, service=e["from_service"], file=None, line=None,
                       name=f"{e['from_service']} (observed at runtime)")
            g.add_edge(Edge(rid, "CONSUMES_API", target, RUNTIME_OBSERVED, "R6", None,
                            {"repo": caller_repo, "source": rec["source"], "evidence_id": rec["id"]}))
        labels.setdefault(("edge", rid, target), []).append(obs)
    return labels


def runtime_of(f: dict, labels: dict) -> Optional[dict]:
    """The runtime observations on a finding's path, summarised: environments, overall window, call count, versions."""
    obs = []
    nodes = [p["node"] for p in f.get("path", [])]
    for a, b in zip(nodes, nodes[1:]):
        obs += labels.get(("edge", b, a), []) + labels.get(("edge", a, b), [])
    if not obs:
        return None
    froms, tos = [o["from"] for o in obs if o["from"]], [o["to"] for o in obs if o["to"]]
    return {"environments": sorted({o["environment"] for o in obs if o["environment"]}), "window": [min(froms) if froms else None, max(tos) if tos else None],
            "calls_observed": sum(o["count"] or 0 for o in obs), "caller_versions": sorted({v for o in obs for v in o["caller_versions"]}),
            "callee_versions": sorted({v for o in obs for v in o["callee_versions"]}), "evidence_ids": sorted({o["evidence_id"] for o in obs})}


# ---- tests: CI results and coverage ----------------------------------------------------------------------------------------------------------
class TestEvidence:
    def __init__(self, extracts: dict, runs: list, coverage: list, revision_of: dict):
        self.extracts, self.revision_of = extracts, revision_of
        self.run_of, self.cov_of = {}, {}
        for repo in extracts:
            self.run_of[repo] = self._pick([r for r in runs if r["repo"] == repo], revision_of.get(repo, []))
            self.cov_of[repo] = self._pick([r for r in coverage if r["repo"] == repo], revision_of.get(repo, []))
        self.results = {}
        for repo, run in self.run_of.items():
            if run:
                idx = {}
                for t in run["data"]["tests"]:
                    for k in test_keys(t):
                        idx.setdefault(k, t)
                self.results[repo] = idx
        self.fn_span = {}
        for repo, ex in extracts.items():
            for f in ex.functions:
                self.fn_span[f.id] = (repo, f.file, f.line, f.end_line or f.line)
        self.handler_of = {e.id: e.handler_id for ex in extracts.values() for e in ex.endpoints}

    @staticmethod
    def _pick(records: list, revisions: list) -> Optional[dict]:
        """The run (or coverage report) of the analysed revision when there is one, else the most recent."""
        for rev in revisions:
            for r in records:
                if r["revision"] and rev and (rev.startswith(r["revision"]) or r["revision"].startswith(rev)):
                    return r
        return records[0] if records else None

    def has_runs(self, repo: str) -> bool:
        return self.run_of.get(repo) is not None

    def span(self, g, node: str):
        a = g.nodes.get(node) or {}
        if a.get("type") == "endpoint":
            node = self.handler_of.get(node, node)
        if node in self.fn_span:
            return self.fn_span[node]
        if a.get("file") and a.get("line"):
            return a["repo"], a["file"], a["line"], a["line"]
        return None

    def states(self, g, t: dict, component: Optional[str], contract: bool) -> dict:
        """The evidence states of one test with respect to one component (FR-023): never collapsed into one another."""
        repo = t["repo"]
        name, file = t["name"], t["file"]
        out = {"exists": True, "associated": t.get("association")}
        run = self.run_of.get(repo)
        res = None
        if run:
            idx = self.results.get(repo, {})
            res = next((idx[k] for k in tia_test_keys(file, name) if k in idx), None)
            revs = self.revision_of.get(repo, [])
            same = bool(run["revision"] and any(r and (r.startswith(run["revision"]) or run["revision"].startswith(r)) for r in revs))
            out["run"] = {"build": run["data"].get("build_id"), "revision": run["revision"], "of_analysed_revision": same,
                          "environment": run["environment"], "at": run["observed_to"], "report": run["data"].get("report_ref"),
                          "evidence_id": run["id"]}
            out["result"] = res["result"] if res else "not_in_run"
        else:
            out["result"] = "no_ci_results"
        cov = self.cov_of.get(repo)
        sp = self.span(g, component) if component else None
        if cov and sp and sp[0] == repo and cov["data"].get("per_test"):
            per = cov["data"]["per_test"]
            key = next((k for k in per if k.split("::")[0].endswith(file) and k.rstrip("]").split("::")[-1].split("[")[0] == name), None)
            if key is None:
                out["executed_component"] = False if per else None
            else:
                f = match_file(per[key], sp[1])
                out["executed_component"] = bool(f and in_ranges(per[key][f], sp[2], sp[3]))
            out["coverage_evidence_id"] = cov["id"]
        else:
            out["executed_component"] = None                   # no per-test coverage: unknown, never "not executed"
        passed_here = out["result"] == "passed" and out.get("run", {}).get("of_analysed_revision")
        out["validated_contract"] = bool(contract and passed_here and t.get("association") == "test-http-call")
        out["states"] = (["exists", "associated"] + ([out["result"]] if out["result"] in ("passed", "failed", "error", "skipped") else [])
                         + (["executed"] if out["executed_component"] else []) + (["validated_contract"] if out["validated_contract"] else []))
        return out

    def component_covered(self, g, node: str) -> Optional[dict]:
        sp = self.span(g, node)
        if not sp:
            return None
        cov = self.cov_of.get(sp[0])
        if not cov:
            return None
        f = match_file(cov["data"]["files"], sp[1])
        return {"covered": bool(f and in_ranges(cov["data"]["files"][f]["covered"], sp[2], sp[3])), "format": cov["data"]["format"],
                "revision": cov["revision"], "evidence_id": cov["id"]}

    def tests_executing(self, repo: str, file: str, lo: int, hi: int) -> list:
        cov = self.cov_of.get(repo)
        per = (cov or {}).get("data", {}).get("per_test") or {}
        out = []
        for test, files in per.items():
            f = match_file(files, file)
            if f and in_ranges(files[f], lo, hi):
                out.append(test)
        return sorted(out)


def enrich_tests(g, report: dict, te: TestEvidence, base_fn: dict, target: str):
    """Adds the evidence states to every test in findings and test scope, coverage to changed functions, regression recommendations and
    missing-test evidence."""
    for f in report["findings"]:
        contract = f.get("impact_type") == "contract"
        for t in f.get("tests", []):
            t.update(te.states(g, t, f["node"], contract))
        cc = te.component_covered(g, f["node"])
        if cc:
            f["coverage"] = cc
    ts = report.get("test_scope") or {}
    for e in ts.get("entry_points", []) + ts.get("code_without_entry_point", []):
        for t in e.get("tests", []):
            t.update(te.states(g, t, e["node"], e["type"] == "endpoint"))
    recs: dict = {}

    def rec(repo, file, name, priority, why, extra=None):
        k = (repo, file, name)
        r = recs.setdefault(k, {"test": name, "file": file, "repo": repo, "priority": priority, "why": []})
        r["priority"] = min(r["priority"], priority)
        if why not in r["why"]:
            r["why"].append(why)
        if extra:
            r.update(extra)
    for c in report["change"]["changed_components"]:
        if c["type"] != "function" or c["name"] not in base_fn:
            continue
        fn = base_fn[c["name"]]
        hit = te.tests_executing(target, fn.file, fn.line, fn.end_line or fn.line)
        if te.cov_of.get(target):
            c["coverage"] = {"covered": bool(te.component_covered(g, c["name"]) and te.component_covered(g, c["name"])["covered"]),
                             "tests_executing": hit[:50], "evidence_id": te.cov_of[target]["id"]}
        for h in hit:
            file, _, rest = h.partition("::")
            rec(target, file, rest.split("::")[-1].split("[")[0], 1, f"executed the changed function {fn.qualname} in the coverage run")
    for f in report["findings"]:
        for t in f.get("tests", []):
            pri = 3 if t.get("evidence_status") == "inferred" else 2
            rec(t["repo"], t["file"], t["name"], pri, f"associated with {f['component']} ({t.get('association')}) on the impact path",
                {"last_result": t.get("result"), "states": t.get("states")})
    for e in ts.get("entry_points", []) + ts.get("code_without_entry_point", []):
        for t in e.get("tests", []):
            rec(t["repo"], t["file"], t["name"], 2, f"tests {e.get('label') or e['entry_point']} ({t.get('association')})",
                {"last_result": t.get("result"), "states": t.get("states")})
    report["test_recommendations"] = {"tests": sorted(recs.values(), key=lambda r: (r["priority"], r["repo"], r["file"], r["test"])),
                                      "note": SKIP_NOTE,
                                      "priority_meaning": {"1": "executed the changed code (coverage)", "2": "statically associated with an affected component",
                                                           "3": "associated by naming convention only (inferred)"}}
    gaps = [{"entry_point": e.get("label") or e["entry_point"], "service": e["service"], "repo": e["repo"], "text": NO_TEST}
            for e in ts.get("entry_points", []) if not e.get("tests")]
    gaps += [{"finding": f["id"], "component": f["component"], "repo": f["repository"], "text": NO_TEST}
             for f in report["findings"] if f.get("material") and not f.get("tests")]
    report["test_gaps"] = gaps
    missing = sorted({t["repo"] for f in report["findings"] for t in f.get("tests", []) if not te.has_runs(t["repo"])}
                     | {t["repo"] for e in ts.get("entry_points", []) for t in e.get("tests", []) if not te.has_runs(t["repo"])})
    return missing


# ---- deployments --------------------------------------------------------------------------------------------------------------------------------
def deployment_view(extracts: dict, records: list, analysed: dict, target: str, base: str, head: str) -> dict:
    """Per repository and environment: the revision deployed most recently, and how it compares with what was analysed."""
    svc_repo = {ex.service: r for r, ex in extracts.items()}
    latest: dict = {}
    for rec in sorted(records, key=lambda r: (r["observed_to"] or "", r["id"])):
        d = rec["data"]
        repo = d.get("repo") or svc_repo.get(d.get("service") or "")
        if repo not in extracts:
            continue
        latest.setdefault(repo, {})[d["environment"]] = {"revision": d["revision"], "deployed_at": d["deployed_at"], "artifact": d.get("artifact"),
                                                         "evidence_id": rec["id"]}
    by_repo = {}
    for repo in sorted(extracts):
        envs = {}
        for env, d in sorted(latest.get(repo, {}).items()):
            rev = d["revision"]
            same = lambda full: bool(full) and (full.startswith(rev) or rev.startswith(full))
            envs[env] = {**d, "is_analysed_revision": same(analysed.get(repo)),
                         **({"is_proposed_revision": same(head), "is_base_revision": same(base)} if repo == target else {})}
        by_repo[repo] = envs
    missing = [{"repo": r, "environments_without_record": [e for e in ENVIRONMENTS if e not in by_repo[r]]}
               for r in sorted(by_repo) if any(e not in by_repo[r] for e in ENVIRONMENTS)]
    return {"environments": list(ENVIRONMENTS), "by_repo": by_repo, "incomplete": missing, "records": len(records)}


def deployment_limitations(f: dict, view: dict) -> list:
    envs = view["by_repo"].get(f["repository"]) or {}
    out = []
    for env, d in envs.items():
        if not d["is_analysed_revision"]:
            out.append(f"{env} runs {f['repository']} at {d['revision'][:7]}, not the analysed revision: the deployed consumer may differ")
    return out
