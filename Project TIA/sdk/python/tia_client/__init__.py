"""Python client for the TIA HTTP API (PRD PL-008: public API and SDK).

    from tia_client import TiaClient
    tia = TiaClient("https://tia.bank.internal", token=os.environ["TIA_TOKEN"])
    report = tia.analyze(target="payments-service", head="feature/x", wait=True)
    for t in tia.test_scope(report["analysis_id"])["entry_points"]:
        print(t["entry_point"], t["change_effect"])

Standard library only. Every method maps to one documented route (docs/API.md, docs/openapi.json); `call()` reaches any other route.
Errors raise TiaError with the HTTP status and TIA's own message.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

__version__ = "1.0.0"


class TiaError(Exception):
    def __init__(self, status: int, message: str, body=None):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message, self.body = status, message, body


def _urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TiaClient:
    """transport(method, url, headers, body, timeout) -> (status, bytes) can be replaced (tests, proxies, retries)."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 60.0, transport: Optional[Callable] = None,
                 user_agent: str = f"tia-client-python/{__version__}"):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ("https", "http") or not u.netloc and not transport:
            raise ValueError("base_url must be http(s)://host[:port]")
        if u.scheme == "http" and token and u.hostname not in ("localhost", "127.0.0.1", "::1", "testserver"):
            raise ValueError("refusing to send a token over plain http to a remote host: use https")
        self.base, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.transport = transport or _urllib_transport
        self.user_agent = user_agent

    # ---- generic -----------------------------------------------------------------------------------------------------------------
    def call(self, method: str, path: str, params: Optional[dict] = None, json_body=None, data: Optional[str] = None,
             content_type: Optional[str] = None):
        q = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.base + path + ("?" + urllib.parse.urlencode(q, doseq=True) if q else "")
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if json_body is not None:
            body, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif data is not None:
            body, headers["Content-Type"] = data.encode("utf-8"), content_type or "text/plain"
        status, raw = self.transport(method, url, headers, body, self.timeout)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        if status >= 400:
            msg = payload.get("detail") if isinstance(payload, dict) else str(payload)
            raise TiaError(status, msg if isinstance(msg, str) else json.dumps(msg), payload)
        return payload

    # ---- repositories and analyses ------------------------------------------------------------------------------------------------
    def repositories(self) -> list:
        return self.call("GET", "/api/v1/repositories")

    def register_repository(self, url: Optional[str] = None, path: Optional[str] = None, name: Optional[str] = None) -> dict:
        return self.call("POST", "/api/v1/repositories", json_body={k: v for k, v in {"url": url, "path": path, "name": name}.items() if v})

    def analyze(self, target: str, head: Optional[str] = None, pr: Optional[int] = None, base: Optional[str] = None, **extra) -> dict:
        """Run an analysis and return its report (the server analyses synchronously)."""
        body = {"target": target, **{k: v for k, v in {"head": head, "pr": pr, "base": base}.items() if v is not None}, **extra}
        return self.call("POST", "/api/v1/impact-analyses", json_body=body)["report"]

    def queue_analysis(self, target: str, head: Optional[str] = None, pr: Optional[int] = None, base: Optional[str] = None, **extra) -> dict:
        """Queue an analysis for the workers; returns the job (job_id, status, deduplicated)."""
        body = {"target": target, **{k: v for k, v in {"head": head, "pr": pr, "base": base}.items() if v is not None}, **extra}
        return self.call("POST", "/api/v1/impact-analyses", params={"async": "true"}, json_body=body)

    def job(self, job_id: str) -> dict:
        return self.call("GET", f"/api/v1/jobs/{job_id}")

    def wait_for_job(self, job_id: str, poll_s: float = 2.0, max_wait_s: float = 900.0) -> dict:
        """Poll a queued analysis until it ends; returns its report when it succeeded."""
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            j = self.job(job_id)
            if j.get("status") == "succeeded":
                aid = (j.get("result") or {}).get("analysis_id")
                return self.report(aid) if aid else j
            if j.get("status") in ("failed", "cancelled", "superseded"):
                raise TiaError(500, f"analysis job {j.get('status')}: {j.get('error') or ''}", j)
            time.sleep(poll_s)
        raise TiaError(504, f"analysis job {job_id} did not finish within {max_wait_s:.0f}s")

    def report(self, analysis_id: str) -> dict:
        return self.call("GET", f"/api/v1/impact-analyses/{analysis_id}")

    def test_scope(self, analysis_id: str) -> dict:
        return self.report(analysis_id).get("test_scope") or {}

    def policy(self, analysis_id: str) -> dict:
        return self.call("GET", f"/api/v1/impact-analyses/{analysis_id}/policy")

    def approve(self, analysis_id: str, owner: str, decision: str = "approve", comment: Optional[str] = None) -> dict:
        """An owning team's approval (decision 'approve') or rejection ('reject') of a change that affects it."""
        return self.call("POST", f"/api/v1/impact-analyses/{analysis_id}/approvals",
                         json_body={"owner": owner, "decision": decision, **({"comment": comment} if comment else {})})

    # ---- evidence -------------------------------------------------------------------------------------------------------------------
    def push_test_results(self, repo: str, revision: str, junit_xml: str, build_id: Optional[str] = None, environment: Optional[str] = None) -> dict:
        return self.call("POST", "/api/v1/evidence/test-results", params={"repo": repo, "revision": revision, "build_id": build_id,
                                                                          "environment": environment}, data=junit_xml, content_type="application/xml")

    def push_coverage(self, repo: str, revision: str, report: str, format: Optional[str] = None) -> dict:
        return self.call("POST", "/api/v1/evidence/coverage", params={"repo": repo, "revision": revision, "format": format}, data=report)

    def push_runtime(self, otlp_json: str, format: str = "otlp") -> dict:
        return self.call("POST", "/api/v1/evidence/runtime", params={"format": format}, data=otlp_json, content_type="application/json")

    def push_deployments(self, deployments: list) -> dict:
        return self.call("POST", "/api/v1/evidence/deployments", data=json.dumps(deployments), content_type="application/json")

    # ---- releases, environments, incidents ------------------------------------------------------------------------------------------
    def record_release(self, name: str, revisions: dict, description: str = "") -> dict:
        return self.call("POST", "/api/v1/releases", data=json.dumps({"name": name, "revisions": revisions, "description": description}),
                         content_type="application/json")

    def compare_releases(self, base: str, head: str, cross_service_only: bool = True) -> dict:
        return self.call("GET", "/api/v1/releases/compare", params={"base": base, "head": head, "cross_service_only": str(cross_service_only).lower()})

    def consumers_at_release(self, release: str, component: str) -> dict:
        return self.call("GET", f"/api/v1/releases/{urllib.parse.quote(release, safe='')}/consumers", params={"component": component})

    def deployed_at(self, at: str, repo: Optional[str] = None) -> dict:
        return self.call("GET", "/api/v1/environments/at", params={"at": at, "repo": repo})

    def record_incident(self, incident: dict) -> dict:
        return self.call("POST", "/api/v1/incidents", data=json.dumps(incident), content_type="application/json")

    def link_incident(self, incident_id: str, repo: str, revision: str, note: str = "") -> dict:
        return self.call("POST", f"/api/v1/incidents/{urllib.parse.quote(incident_id, safe='')}/links",
                         data=json.dumps({"repo": repo, "revision": revision, "note": note}), content_type="application/json")

    def investigate_incident(self, incident_id: str, window_hours: Optional[float] = None) -> dict:
        return self.call("GET", f"/api/v1/incidents/{urllib.parse.quote(incident_id, safe='')}/investigation", params={"window_hours": window_hours})

    # ---- testing: tester pack of a change, landscape views, direct graph questions, the agent (Knowledge Fabric) answer ---------------
    def tester_pack(self, analysis_id: str) -> dict:
        """For the entry points a change reaches: coverage gate, technical coverage, UI locator pack, API test matrix."""
        return self.call("GET", f"/api/v1/impact-analyses/{urllib.parse.quote(analysis_id, safe='')}/tester-pack")

    def coverage(self, repositories: Optional[list] = None) -> dict:
        return self.call("GET", "/api/v1/coverage", params={"repos": ",".join(repositories) if repositories else None})

    def api_inventory(self, repositories: Optional[list] = None) -> dict:
        return self.call("GET", "/api/v1/automation/api", params={"repos": ",".join(repositories) if repositories else None})

    def ui_inventory(self, repositories: Optional[list] = None) -> dict:
        return self.call("GET", "/api/v1/automation/ui", params={"repos": ",".join(repositories) if repositories else None})

    def check_test_case(self, title: str = "", entry_point: str = "", steps: str = "", expected: str = "") -> dict:
        """One test case as a query: what it covers and what it misses."""
        return self.call("POST", "/api/v1/coverage/test-case", json_body={"title": title, "entry_point": entry_point, "steps": steps, "expected": expected})

    def shared_components(self, limit: int = 100) -> dict:
        return self.call("GET", "/api/v1/graph/shared-components", params={"limit": limit})

    def graph_query(self, question: str, component: str, repository: Optional[str] = None, limit: int = 200) -> dict:
        """question: consumers | dependencies | tests | covers. The answer's status is ok, ambiguous (see 'candidates') or not_found."""
        return self.call("GET", "/api/v1/graph/query", params={"question": question, "component": component, "repository": repository, "limit": limit})

    def consumers_of(self, component: str, repository: Optional[str] = None) -> dict:
        return self.graph_query("consumers", component, repository)

    def dependencies_of(self, component: str, repository: Optional[str] = None) -> dict:
        return self.graph_query("dependencies", component, repository)

    def tests_covering(self, component: str, repository: Optional[str] = None) -> dict:
        return self.graph_query("tests", component, repository)

    def covered_by_test(self, test: str, repository: Optional[str] = None) -> dict:
        return self.graph_query("covers", test, repository)

    def agent_answer(self, analysis_id: str, include_tester_pack: bool = True, top_k: int = 10) -> dict:
        """What an AI testing agent receives for one analysis (Knowledge Fabric QueryResponse: chunks and paths)."""
        return self.call("POST", "/query/tia", json_body={"analysis_id": analysis_id, "include_tester_pack": include_tester_pack,
                                                          "top_k_semantic": top_k, "top_k_graph": top_k})

    # ---- audit ---------------------------------------------------------------------------------------------------------------------
    def verify_audit(self) -> dict:
        return self.call("GET", "/api/v1/audit/verify")

    def audit_report(self, start: str, end: str) -> dict:
        return self.call("GET", "/api/v1/audit/report", params={"start": start, "end": end})
