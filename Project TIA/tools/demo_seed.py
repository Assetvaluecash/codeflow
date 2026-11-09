"""Support script (not production code): loads the demo evidence of docs/DEMO-DATA.md into a running TIA and writes the upload zips.

    python tools/run_dev.py --demo --landscape lanes --data demo-data/lanes      # first, in another terminal
    python tools/demo_seed.py [--url http://127.0.0.1:8080] [--token TOKEN] [--zips demo-data/inputs]

What it loads (every record is an ordinary API call a CI pipeline, a deployment tool or an ITSM would make):
  * releases 2026.10 (payments-service and statement-service) and 2026.11 (customer-portal joins): section 5a "Releases";
  * CI test results of payments-service (JUnit: 3 passed, 1 failed): shown as "last result" on recommended tests;
  * deployments to staging and production (section 5a "What ran where", section 7 "Environments");
  * observed production traffic (OpenTelemetry): the two known consumers plus an unknown one, mobile-app;
  * incident INC-1001 on statement-service in production (section 5a "Incidents").
It also writes loyalty-service-v1.zip and loyalty-service-v2.zip (section 1 "Upload zip"). Running it twice is harmless: releases
are refused as duplicates and the other records are added again.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANES = ("payments-service", "statement-service", "customer-portal")

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.test_payments" name="test_list_payments" file="tests/test_payments.py" time="0.12"/>
  <testcase classname="tests.test_payments" name="test_unknown_account_is_404" file="tests/test_payments.py" time="0.05"/>
  <testcase classname="tests.test_payments" name="test_create_payment" file="tests/test_payments.py" time="0.09"/>
  <testcase classname="tests.test_payments" name="test_create_payment_requires_a_reference" file="tests/test_payments.py" time="0.04">
    <failure message="assert 400 == 422">the service answered 400 instead of 422</failure></testcase>
</testsuite></testsuites>"""

# ---- loyalty-service: a fourth consumer of payments-service, sent as an upload (no Git host) ----------------------------------
LOYALTY_CLIENT = '''import os

import requests

PAYMENTS_BASE_URL = os.getenv("PAYMENTS_BASE_URL", "http://payments-service")


def fetch_payments(account_id: str):
    return requests.get(f"{PAYMENTS_BASE_URL}/accounts/{account_id}/payments", timeout=5).json()
'''
LOYALTY_APP = '''from fastapi import FastAPI

from client import fetch_payments

app = FastAPI()


def points_for(payments):
    """One point per 10 of settled, undisputed payments."""
    return sum(int(p["amount"]) for p in payments if p["status"] == "SETTLED" and not p["disputed"]) // 10


@app.get("/loyalty/{account_id}/points")
def loyalty_points(account_id: str):
    return {"account": account_id, "points": points_for(fetch_payments(account_id))}
'''
LOYALTY_TEST = '''from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_loyalty_points():
    assert client.get("/loyalty/123/points").status_code == 200
'''
LOYALTY_YAML = ("service: loyalty-service\nowner: team-loyalty\nconfig_keys:\n  PAYMENTS_BASE_URL: payments-service\n"
                "entry_points:\n  GET /loyalty/{account_id}/points: Loyalty points\n")


def loyalty_zip(version: int) -> bytes:
    app = LOYALTY_APP if version == 1 else LOYALTY_APP.replace("// 10", "// 5").replace("per 10", "per 5")   # v2: double points
    files = {"app.py": app, "client.py": LOYALTY_CLIENT, "tests/test_loyalty.py": LOYALTY_TEST, "tia-services.yaml": LOYALTY_YAML}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr("loyalty-service/" + name, text)
    return buf.getvalue()


def otlp(calls: list) -> str:
    """OpenTelemetry traces: (caller, callee, route, environment) -> a client span and the server span under it."""
    res = []
    for i, (caller, callee, route, env) in enumerate(calls):
        attrs = lambda svc: [{"key": "service.name", "value": {"stringValue": svc}},                       # noqa: E731
                             {"key": "deployment.environment", "value": {"stringValue": env}}]
        span = {"traceId": f"t{i}", "startTimeUnixNano": "1793700000000000000", "endTimeUnixNano": "1793700001000000000"}
        res.append({"resource": {"attributes": attrs(caller)}, "scopeSpans": [{"spans": [{**span, "spanId": f"c{i}", "kind": 3}]}]})
        res.append({"resource": {"attributes": attrs(callee)}, "scopeSpans": [{"spans": [{**span, "spanId": f"s{i}", "parentSpanId": f"c{i}", "kind": 2,
                    "attributes": [{"key": "http.route", "value": {"stringValue": route}},
                                   {"key": "http.request.method", "value": {"stringValue": "GET"}}]}]}]})
    return json.dumps({"resourceSpans": res})


def check(r, what: str, ok=(200,)):
    if r.status_code not in ok:
        raise SystemExit(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def seed(c, out_dir: Path | None = None) -> dict:
    """c: an httpx.Client (or a FastAPI TestClient) pointed at TIA. Returns what was loaded."""
    repos = {r["name"]: r for r in check(c.get("/api/v1/repositories"), "list repositories")}
    missing = [n for n in LANES if n not in repos]
    if missing:
        raise SystemExit(f"not the lanes demo: {missing} not registered. Start: python tools/run_dev.py --demo --landscape lanes --data demo-data/lanes")
    done: dict = {}
    # releases first: their answer carries the commit each branch resolves to
    rels = {}
    for name, revs in (("2026.10", {"payments-service": "main", "statement-service": "main"}), ("2026.11", {n: "main" for n in LANES})):
        r = c.post("/api/v1/releases", content=json.dumps({"name": name, "revisions": revs, "description": "demo"}))
        if r.status_code == 409:
            rels[name] = next(x["revisions"] for x in check(c.get("/api/v1/releases"), "list releases")["releases"] if x["name"] == name)
        else:
            rels[name] = check(r, f"release {name}")["revisions"]
    done["releases"] = sorted(rels)
    main = rels["2026.11"]
    r = check(c.post(f"/api/v1/evidence/test-results?repo=payments-service&revision={main['payments-service']}&build_id=ci-1042&environment=ci",
                     content=JUNIT), "test results")
    done["test_results"] = r["counts"]
    deps = [{"repo": "payments-service", "environment": "staging", "revision": main["payments-service"], "deployed_at": "2026-11-02T09:00:00Z"},
            {"repo": "payments-service", "environment": "production", "revision": main["payments-service"], "deployed_at": "2026-11-03T08:00:00Z"},
            {"repo": "statement-service", "environment": "production", "revision": main["statement-service"], "deployed_at": "2026-11-01T08:00:00Z"},
            {"repo": "customer-portal", "environment": "production", "revision": main["customer-portal"], "deployed_at": "2026-11-01T08:30:00Z"}]
    done["deployments"] = check(c.post("/api/v1/evidence/deployments?source=pipeline", content=json.dumps(deps)), "deployments")["deployments"]
    calls = [("statement-service", "payments-service", "/accounts/{account_id}/payments", "production"),
             ("customer-portal", "payments-service", "/accounts/{account_id}/payments", "production"),
             ("mobile-app", "payments-service", "/accounts/{account_id}/payments", "production")]
    done["runtime_edges"] = check(c.post("/api/v1/evidence/runtime?format=otlp&source=otel-collector", content=otlp(calls)), "runtime")["edges"]
    inc = {"id": "INC-1001", "title": "Statement PDF totals are wrong", "service": "statement-service", "environment": "production",
           "started_at": "2026-11-03T12:00:00Z", "ended_at": "2026-11-03T14:00:00Z", "severity": "high"}
    done["incident"] = check(c.post("/api/v1/incidents?source=itsm", content=json.dumps(inc)), "incident")["incident"]
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for v in (1, 2):
            (out_dir / f"loyalty-service-v{v}.zip").write_bytes(loyalty_zip(v))
        done["zips"] = str(out_dir)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--token", help="a service-account token, when the server requires sign-in")
    ap.add_argument("--zips", default=str(ROOT / "demo-data" / "inputs"))
    a = ap.parse_args()
    import httpx
    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    with httpx.Client(base_url=a.url, headers=headers, timeout=300) as c:
        done = seed(c, Path(a.zips))
    print(json.dumps(done, indent=2))
    print(f"\nUpload zips written to {done['zips']}")


if __name__ == "__main__":
    sys.exit(main())
