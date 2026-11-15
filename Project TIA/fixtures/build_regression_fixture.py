"""Builds the regression-selection demo (docs/MEETING-PAWAN.md, use case 1): one payments app, ten written functional test cases, a main
branch and feature branches.

payments-app   FastAPI service + a React "Pay" screen + testcases/regression-suite.csv (10 functional test cases, one of them naming
               nothing TIA can match). Configuration in config/app.env (MIN_BALANCE).

Feature branches (each from main):
  feature/holds      balance_of() now subtracts held amounts when the NEW environment variable HOLDS_ENABLED is "true" (the statement
                     already reads HOLDS_ENABLED; no configuration file sets it). Expected: 4 of 10 test cases selected
                     (TC-01, TC-02, TC-03 through POST /payments, TC-03 via the Pay screen, TC-07 through GET /accounts/{id}/balance).
  feature/min-balance  config/app.env raises MIN_BALANCE from 80 to 100 and changes FX_PROVIDER (a configuration-only change).
                     Expected: 3 of 10 selected (the cases that create a payment), reached through can_pay(), which reads MIN_BALANCE;
                     FX_PROVIDER is read by no analysed code, which is said, and neither value appears in the report.
  feature/fx-rounding  convert() rounds differently: reaches the statement and the exchange-rate endpoints. Expected: 2 of 10
                     (TC-08, TC-09); TC-11, added by the branch itself, is listed as new, not as regression.
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


APP = '''import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()

MIN_BALANCE = float(os.getenv("MIN_BALANCE", "80"))
RATES = {"EUR": 1.0, "USD": 1.08, "GBP": 0.86}
ACCOUNTS = {"ACC1001": {"balance": 500.0, "held": 120.0, "currency": "EUR"}}
PAYMENTS = {"P1": {"payment_id": "P1", "account_id": "ACC1001", "amount": 25.0, "currency": "EUR", "status": "SETTLED"}}


class PaymentRequest(BaseModel):
    account_id: str = Field(min_length=6, max_length=12)
    amount: float = Field(gt=0, le=10000)
    currency: str
    reference: str = Field(max_length=18)


class Payment(BaseModel):
    payment_id: str
    account_id: str
    amount: float
    currency: str
    status: str


class Balance(BaseModel):
    account_id: str
    balance: float
    currency: str


def balance_of(account_id: str) -> float:
    return ACCOUNTS[account_id]["balance"]


def can_pay(account_id: str, amount: float) -> bool:
    return balance_of(account_id) - amount >= MIN_BALANCE


def fee_for(amount: float) -> float:
    return round(amount * 0.01, 2)


def convert(amount: float, currency: str) -> float:
    return round(amount * RATES[currency], 2)


@app.post("/payments", response_model=Payment, status_code=201)
def create_payment(req: PaymentRequest):
    if req.account_id not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="unknown account")
    if not can_pay(req.account_id, req.amount + fee_for(req.amount)):
        raise HTTPException(status_code=409, detail="insufficient balance")
    p = {"payment_id": f"P{len(PAYMENTS) + 1}", "account_id": req.account_id, "amount": req.amount, "currency": req.currency,
         "status": "PENDING"}
    PAYMENTS[p["payment_id"]] = p
    return p


@app.get("/payments/{payment_id}", response_model=Payment)
def get_payment(payment_id: str):
    if payment_id not in PAYMENTS:
        raise HTTPException(status_code=404, detail="unknown payment")
    return PAYMENTS[payment_id]


@app.get("/payments/{payment_id}/status")
def get_status(payment_id: str):
    return {"status": get_payment(payment_id)["status"]}


@app.post("/payments/{payment_id}/refund")
def refund(payment_id: str):
    p = get_payment(payment_id)
    p["status"] = "REFUNDED"
    return {"refunded": p["amount"]}


@app.get("/accounts/{account_id}/balance", response_model=Balance)
def get_balance(account_id: str):
    return {"account_id": account_id, "balance": balance_of(account_id), "currency": ACCOUNTS[account_id]["currency"]}


@app.get("/accounts/{account_id}/statement")
def statement(account_id: str, currency: str = "EUR"):
    rows = [p for p in PAYMENTS.values() if p["account_id"] == account_id]
    held = ACCOUNTS[account_id]["held"] if os.getenv("HOLDS_ENABLED") == "true" else 0.0
    return {"payments": [dict(p, amount=convert(p["amount"], currency)) for p in rows], "held": held}


@app.get("/fx/rates")
def rates(currency: str = "EUR"):
    return {c: convert(1.0, c) for c in RATES}
'''

HOLDS = APP.replace('''def balance_of(account_id: str) -> float:
    return ACCOUNTS[account_id]["balance"]''', '''def balance_of(account_id: str) -> float:
    acc = ACCOUNTS[account_id]
    if os.getenv("HOLDS_ENABLED", "false") == "true":
        return acc["balance"] - acc["held"]
    return acc["balance"]''')

FX = APP.replace("return round(amount * RATES[currency], 2)", "return round(amount * RATES[currency], 4)")

PAY_SCREEN = '''import { useState } from "react";
import axios from "axios";

export default function Pay() {
  const [amount, setAmount] = useState("");
  const [reference, setReference] = useState("");
  const pay = () => axios.post("/payments", { account_id: "ACC1001", amount, currency: "EUR", reference });
  return (
    <form onSubmit={e => { e.preventDefault(); pay(); }}>
      <label htmlFor="amount">Amount</label>
      <input id="amount" name="amount" type="number" required min="0.01" max="10000" onChange={e => setAmount(e.target.value)} />
      <label htmlFor="reference">Reference</label>
      <input id="reference" name="reference" required maxLength={18} onChange={e => setReference(e.target.value)} />
      <button type="submit" data-testid="pay">Pay</button>
    </form>
  );
}
'''

ROUTES = '''import { BrowserRouter, Routes, Route } from "react-router-dom";
import Pay from "./Pay";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/pay" element={<Pay />} />
      </Routes>
    </BrowserRouter>
  );
}
'''

SUITE = '''ID,Title,Entry point,Steps,Expected Result,Priority
TC-01,Create a payment,POST /payments,"Pay 50 EUR from account ACC1001 with reference INV1",The payment is created with status PENDING,High
TC-02,Payment refused below the minimum balance,POST /payments,"Pay 450 EUR from account ACC1001 (balance 500)",The payment is refused: 409 insufficient balance,High
TC-03,Pay screen: amount above the limit,/pay,"Open the Pay screen, enter amount 20000 and press Pay",The amount field shows an error,Medium
TC-04,View a payment,GET /payments/{payment_id},Open payment P1,The payment details are shown,Medium
TC-05,Payment status,GET /payments/{payment_id}/status,Ask for the status of payment P1,The status is SETTLED,Medium
TC-06,Refund a payment,POST /payments/{payment_id}/refund,Refund payment P1,The payment is REFUNDED and the amount returned,High
TC-07,Account balance,GET /accounts/{account_id}/balance,Open the balance of account ACC1001,The balance and currency are shown,High
TC-08,Statement in USD,GET /accounts/{account_id}/statement,Open the statement of ACC1001 in USD,Amounts are converted to USD,Medium
TC-09,Exchange rates,GET /fx/rates,Open the exchange rates,EUR USD and GBP rates are listed,Low
TC-10,Statement export reaches the archive,,Export last month's statements,The archive receives the file,Low
'''

TESTS = '''from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_create_payment():
    r = client.post("/payments", json={"account_id": "ACC1001", "amount": 10, "currency": "EUR", "reference": "T1"})
    assert r.status_code == 201


def test_rates():
    assert client.get("/fx/rates").status_code == 200
'''


def build(dest: str) -> dict:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    p = root / "payments-app"
    p.mkdir(parents=True, exist_ok=True)
    _run(p, "init", "-q", "-b", "main")
    _write(p, {"app.py": APP, "tests/test_app.py": TESTS, "web/src/Pay.jsx": PAY_SCREEN, "web/src/App.jsx": ROUTES,
               "testcases/regression-suite.csv": SUITE, "config/app.env": "MIN_BALANCE=80\nFX_PROVIDER=ecb\n",
               "tia-services.yaml": "service: payments-app\nowner: team-payments\ncriticality: critical\n"
                                    "entry_points:\n  POST /payments: Make a payment\n  GET /accounts/{account_id}/balance: Account balance\n"})
    base = _commit(p, "initial")
    out = {"paths": {"payments-app": str(p)}, "shas": {"base": base}, "target": "payments-app"}

    def branch(name, edits: dict):
        _run(p, "checkout", "-q", "-b", name, base)
        _write(p, edits)
        out["shas"][name] = _commit(p, name)

    branch("feature/holds", {"app.py": HOLDS})
    branch("feature/min-balance", {"config/app.env": "MIN_BALANCE=100\nFX_PROVIDER=ecb-live-7f3a9c\n"})
    branch("feature/fx-rounding", {"app.py": FX, "testcases/regression-suite.csv": SUITE + (
        "TC-11,Rates show four decimals,GET /fx/rates,Open the exchange rates,Rates are shown with four decimals,Medium\n")})
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))
