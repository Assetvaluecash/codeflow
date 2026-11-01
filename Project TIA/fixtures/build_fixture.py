"""Builds the 6-repository demo landscape (real git repos) used by the tests and `tia demo`.

payments-service   provider; PR branches change the PaymentStatus contract / implementation / tests
reconciliation-service   R2 consumer (config key -> service map), critical, with transitive endpoint + test
ledger-sync        unresolved base URL -> R5 inferred (INF-PATH-UNIQUE)
mobile-bff         generated client -> R1
audit-reporting    declared contract only -> R3
notifications      unresolvable URL -> unresolved outbound call
"""
from __future__ import annotations

import subprocess
from pathlib import Path

GIT = ["git", "-c", "user.name=tia", "-c", "user.email=tia@example.com", "-c", "commit.gpgsign=false"]


def _run(cwd, *args):
    r = subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def _write(root: Path, files: dict):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")


def _commit(root: Path, msg: str) -> str:
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", msg, "--allow-empty")
    return _run(root, "rev-parse", "HEAD")


PAYMENTS_APP = '''from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class PaymentStatus(BaseModel):
    payment_id: str
    status: str
    amount: float


class PaymentRequest(BaseModel):
    account: str
    amount: float


def lookup(payment_id: str):
    return {"payment_id": payment_id, "status": "SETTLED", "amount": 10.0}


@app.get("/payments/{payment_id}/status", response_model=PaymentStatus)
def get_status(payment_id: str):
    return lookup(payment_id)


@app.post("/payments")
def create_payment(req: PaymentRequest):
    return {"ok": True}
'''

PAYMENTS_TEST = '''from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_status_endpoint():
    r = client.get("/payments/123/status")
    assert r.status_code == 200
'''


def build(dest: str) -> dict:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    out = {"paths": {}, "shas": {}}

    def repo(name, files):
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        out["paths"][name] = str(p)
        return p, _commit(p, "initial")

    p, base = repo("payments-service", {
        "app.py": PAYMENTS_APP,
        "tests/test_payments.py": PAYMENTS_TEST,
        "README.md": "# payments\n",
        "tia-services.yaml": "service: payments-service\nowner: team-payments\ncriticality: critical\n"
                             "packages:\n  payments_client: payments-service\n"})
    out["shas"]["base"] = base

    repo("reconciliation-service", {
        "client.py": '''import os
import requests

PAYMENTS_BASE_URL = os.getenv("PAYMENTS_BASE_URL", "http://payments-service")


def fetch_status(payment_id: str):
    r = requests.get(f"{PAYMENTS_BASE_URL}/payments/{payment_id}/status", timeout=3)
    return r.json()
''',
        "reconcile.py": '''from client import fetch_status


def reconcile(payment_id):
    s = fetch_status(payment_id)
    return s["status"]
''',
        "app.py": '''from fastapi import FastAPI
from reconcile import reconcile

app = FastAPI()


@app.get("/reconciliations/{rid}")
def get_reconciliation(rid: str):
    return {"status": reconcile(rid)}
''',
        "tests/test_reconcile.py": '''from client import fetch_status


def test_fetch_status():
    assert fetch_status("1") is not None
''',
        "tia-services.yaml": "service: reconciliation-service\nowner: team-recon\ncriticality: critical\n"
                             "config_keys:\n  PAYMENTS_BASE_URL: payments-service\n"})

    repo("ledger-sync", {
        "sync.py": '''import os
import requests


def pull_status(pid):
    return requests.get(os.environ["LEDGER_UPSTREAM"] + "/payments/" + pid + "/status").json()
''',
        "tia-services.yaml": "service: ledger-sync\nowner: team-ledger\n"})

    repo("mobile-bff", {
        "bff.py": '''from payments_client import PaymentsClient

client = PaymentsClient()


def show_payment(pid):
    return client.get_status(pid)
''',
        "tia-services.yaml": "service: mobile-bff\nowner: team-mobile\n"})

    repo("audit-reporting", {
        "report.py": "def build_report():\n    return []\n",
        "contracts/payments.json": '{"consumer": "audit-reporting", "provider": "payments-service", '
                                   '"interactions": [{"method": "GET", "path": "/payments/{id}/status"}]}',
        "tia-services.yaml": "service: audit-reporting\nowner: team-audit\n"})

    repo("notifications", {
        "notify.py": '''import requests


def build_url(pid):
    return "https://example.invalid/" + str(pid)


def send(pid):
    url = build_url(pid)
    return requests.post(url)
''',
        "tia-services.yaml": "service: notifications\nowner: team-notify\n"})

    # ---- PR branches on payments-service (all branch from base) -------------------------------------------
    def branch(name, edits: dict):
        _run(p, "checkout", "-q", "-b", name, base)
        _write(p, edits)
        out["shas"][name] = _commit(p, name)

    branch("breaking", {"app.py": PAYMENTS_APP.replace("    status: str\n", "    state: str\n")})
    branch("impl", {"app.py": PAYMENTS_APP.replace('"SETTLED"', '"COMPLETED"')})
    branch("tests_only", {"tests/test_payments.py": PAYMENTS_TEST + "\n\ndef test_again():\n    assert True\n"})
    branch("docs_only", {"README.md": "# payments\nupdated docs\n"})
    branch("syntax_error", {"broken.py": "def oops(:\n    pass\n"})
    branch("removed_endpoint", {"app.py": PAYMENTS_APP.replace(
        '@app.get("/payments/{payment_id}/status", response_model=PaymentStatus)\n'
        'def get_status(payment_id: str):\n    return lookup(payment_id)\n\n\n', "")})
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))
