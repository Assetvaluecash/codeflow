"""Builds the tester landscape of docs/CASE-STUDY.md (UC-1 test scope, UC-2 technical coverage, UC-3 automation inventory).

payments-service    provider: GET /accounts/{account_id}/payments returns List[Payment] (payment_id, status, amount, disputed);
                    POST /accounts/{account_id}/payments takes a PaymentRequest with rules (amount > 0 and <= 10000, currency one of
                    EUR/USD/GBP, reference 1-18 of A-Z0-9, account id 1-9 digits); answers 404 for an unknown account, 409 for a
                    duplicate reference. Its automated tests cover some of that and deliberately miss the rest.
statement-service   the PDF statement: GET /statements/{account_id}/pdf - leaves disputed payments OUT (the rule);
                    a React page (web/src/StatementRequest.jsx, route /statements); written test cases in testcases/statement.yaml
customer-portal     the screen's backend: GET /portal/accounts/{account_id}/payments - shows every payment (no rule: the gap);
                    POST /portal/accounts/{account_id}/payments forwards a payment; an Angular page (web/src/app/payments, route
                    /accounts/:id/payments); written test cases in testcases/portal-payments.csv

PR branches on payments-service (all from base):
  behaviour      the listing logic changes (newest first): both consumers must be retested, and compared
  move_rule      the disputed-payment rule moves into the shared endpoint (a generic function): both consumers change behaviour
  signature      a helper gains a required parameter; its only caller is updated in the same change
  signature_unfixed  a helper gains a required parameter; its caller is NOT updated (breaking)
  docstring      documentation only: nothing to retest
  contract       the `disputed` field is removed from the response: breaking for both consumers
"""
from __future__ import annotations

from pathlib import Path

from fixtures.build_fixture import _commit, _run, _write

PAYMENTS = '''from enum import Enum
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, Field

app = FastAPI()
ACCOUNTS = {"123", "456"}
SEEN = set()


class Payment(BaseModel):
    payment_id: str
    status: str
    amount: float
    disputed: bool


class Currency(str, Enum):
    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"


class PaymentStatus(str, Enum):
    SETTLED = "SETTLED"
    PENDING = "PENDING"


class PaymentRequest(BaseModel):
    amount: float = Field(..., gt=0, le=10000)
    currency: Currency
    reference: str = Field(..., min_length=1, max_length=18, pattern=r"^[A-Z0-9]+$")
    express: bool = False
    tags: List[str] = []


def load_payments(account_id: str):
    return [{"payment_id": "p1", "status": "SETTLED", "amount": 10.0, "disputed": False},
            {"payment_id": "p2", "status": "SETTLED", "amount": 99.0, "disputed": True}]


def ordered(rows):
    return sorted(rows, key=lambda r: r["payment_id"])


@app.get("/accounts/{account_id}/payments", response_model=List[Payment])
def list_payments(account_id: str, status: Optional[PaymentStatus] = None):
    """Payments of one account."""
    if account_id not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="unknown account")
    return ordered(load_payments(account_id))


def known_account(account_id: str):
    if account_id not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="unknown account")


@app.post("/accounts/{account_id}/payments", status_code=201)
def create_payment(req: PaymentRequest, account_id: str = Path(..., pattern="^[0-9]{1,9}$")):
    known_account(account_id)
    if req.reference in SEEN:
        raise HTTPException(status_code=409, detail="duplicate reference")
    SEEN.add(req.reference)
    return {"payment_id": "p3"}
'''

PAYMENTS_TEST = '''from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_list_payments():
    assert client.get("/accounts/123/payments").status_code == 200


def test_unknown_account_is_404():
    assert client.get("/accounts/999/payments").status_code == 404


def test_create_payment():
    r = client.post("/accounts/123/payments", json={"amount": 10.5, "currency": "EUR", "reference": "INV001"})
    assert r.status_code == 201


def test_create_payment_requires_a_reference():
    r = client.post("/accounts/123/payments", json={"amount": 10.5, "currency": "EUR"})
    assert r.status_code == 422
'''

CLIENT = '''import os

import requests

PAYMENTS_BASE_URL = os.getenv("PAYMENTS_BASE_URL", "http://payments-service")


def fetch_payments(account_id: str):
    return requests.get(f"{PAYMENTS_BASE_URL}/accounts/{account_id}/payments", timeout=5).json()
'''

PORTAL_CLIENT = CLIENT + '''

def send_payment(account_id: str, payment: dict):
    return requests.post(f"{PAYMENTS_BASE_URL}/accounts/{account_id}/payments", json=payment, timeout=5).json()
'''

STATEMENT_APP = '''from fastapi import FastAPI

from client import fetch_payments

app = FastAPI()


def statement_rows(account_id: str):
    payments = fetch_payments(account_id)
    return [p for p in payments if not p["disputed"]]


def render_pdf(rows):
    return b"%PDF-" + str(len(rows)).encode()


@app.get("/statements/{account_id}/pdf")
def statement_pdf(account_id: str):
    return render_pdf(statement_rows(account_id))
'''

STATEMENT_TEST = '''from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_statement_pdf():
    assert client.get("/statements/a1/pdf").status_code == 200
'''

PORTAL_APP = '''from fastapi import FastAPI
from pydantic import BaseModel

from client import fetch_payments, send_payment

app = FastAPI()


class NewPayment(BaseModel):
    amount: float
    currency: str
    reference: str


@app.get("/portal/accounts/{account_id}/payments")
def account_payments(account_id: str):
    payments = fetch_payments(account_id)
    return [{"id": p["payment_id"], "amount": p["amount"], "status": p["status"]} for p in payments]


@app.post("/portal/accounts/{account_id}/payments")
def pay(account_id: str, body: NewPayment):
    return send_payment(account_id, body.dict())
'''

PORTAL_TEST = '''from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_portal_payments():
    assert client.get("/portal/accounts/a1/payments").status_code == 200
'''

PORTAL_PAGE = '''<h1>Payments</h1>
<form [formGroup]="form" (ngSubmit)="submit()">
  <label for="amount">Amount</label>
  <input id="amount" type="number" formControlName="amount" min="0.01" max="10000" required>
  <label for="currency">Currency</label>
  <select id="currency" formControlName="currency" required>
    <option *ngFor="let c of currencies" [value]="c">{{ c }}</option>
  </select>
  <input formControlName="reference" maxlength="18" placeholder="Reference">
  <label><input type="checkbox" formControlName="express"> Express</label>
  <button type="submit" data-testid="pay-submit">Pay</button>
</form>
<label for="status-filter">Status</label>
<select id="status-filter" (change)="filter($event)">
  <option value="">All</option>
  <option value="SETTLED">Settled</option>
  <option value="PENDING">Pending</option>
</select>
<button (click)="refresh()">Refresh</button>
<button (click)="export()">Export</button>
<table data-testid="payments-table">
  <tr *ngFor="let p of payments">
    <td>{{ p.id }}</td>
    <td><button (click)="details(p)">Details</button></td>
  </tr>
</table>
<button (click)="export()">Export</button>
'''

PORTAL_COMPONENT = '''import { Component, OnInit } from '@angular/core';
import { FormBuilder, Validators } from '@angular/forms';
import { HttpClient } from '@angular/common/http';

@Component({
  selector: 'app-payments',
  templateUrl: './payments.component.html'
})
export class PaymentsComponent implements OnInit {
  accountId = '123';
  currencies = ['EUR', 'USD', 'GBP'];
  payments: any[] = [];
  form = this.fb.group({
    amount: [null, [Validators.required, Validators.min(0.01), Validators.max(10000)]],
    currency: ['EUR', Validators.required],
    reference: ['', [Validators.required, Validators.maxLength(18), Validators.pattern('^[A-Z0-9]+$')]],
    express: [false]
  });

  constructor(private fb: FormBuilder, private http: HttpClient) {}

  ngOnInit() { this.load(); }

  load() {
    this.http.get<any[]>(`/portal/accounts/${this.accountId}/payments`).subscribe(p => this.payments = p);
  }

  submit() {
    this.http.post(`/portal/accounts/${this.accountId}/payments`, this.form.value).subscribe(() => this.load());
  }
}
'''

PORTAL_ROUTES = '''import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';
import { PaymentsComponent } from './payments/payments.component';

const routes: Routes = [
  { path: 'accounts/:id/payments', component: PaymentsComponent },
  { path: '', redirectTo: 'accounts/123/payments', pathMatch: 'full' }
];

@NgModule({ imports: [RouterModule.forRoot(routes)], exports: [RouterModule] })
export class AppRoutingModule {}
'''

PORTAL_CASES = '''ID,Title,Entry point,Steps,Expected Result,Priority
TC-101,View payments,GET /portal/accounts/{account_id}/payments,Open the payments of account 123,All payments of the account are listed,High
TC-102,Pay with missing amount,/accounts/:id/payments,"Open the payments page; leave Amount empty; press Pay",The form refuses to submit,High
TC-103,Pay in USD,/accounts/:id/payments,"Choose currency USD, enter amount 10 and reference INV9, press Pay",The payment is created,Medium
TC-104,Reference too long,/accounts/:id/payments,Enter a reference longer than 18 characters,An error is shown,Low
TC-105,Payment for an unknown account,POST /accounts/{account_id}/payments,Send a payment for account 999,The service answers not found,Medium
'''

STATEMENT_PAGE = '''import { useState } from "react";

const MONTHS = ["2026-09", "2026-10", "2026-11"];

export default function StatementRequest() {
  const [account, setAccount] = useState("");
  const [month, setMonth] = useState("");
  const download = () => fetch(`/statements/${account}/pdf?month=${month}`);
  return (
    <form onSubmit={e => { e.preventDefault(); download(); }}>
      <label htmlFor="account">Account number</label>
      <input id="account" name="account" required pattern="[0-9]{1,9}" maxLength={9} onChange={e => setAccount(e.target.value)} />
      <label htmlFor="month">Month</label>
      <select id="month" name="month" required value={month} onChange={e => setMonth(e.target.value)}>
        <option value="">Choose a month</option>
        {MONTHS.map(m => <option key={m} value={m}>{m}</option>)}
      </select>
      <button type="submit" data-testid="download-statement">Download PDF</button>
    </form>
  );
}
'''

STATEMENT_ROUTES = '''import { BrowserRouter, Routes, Route } from "react-router-dom";
import StatementRequest from "./StatementRequest";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/statements" element={<StatementRequest />} />
      </Routes>
    </BrowserRouter>
  );
}
'''

STATEMENT_CASES = '''- id: TC-201
  title: Statement PDF leaves out disputed payments
  entry_point: GET /statements/{account_id}/pdf
  steps: Create a disputed payment for account 123, then download the statement
  expected: The disputed payment is not in the PDF
- id: TC-202
  title: Download a statement
  entry_point: /statements
  steps: Open the statement page, enter account number 123, choose month 2026-10 and press Download PDF
  expected: The PDF downloads
- id: TC-203
  title: Account number with letters
  entry_point: /statements
  steps: Enter ABC as the account number
  expected: The field shows an invalid format error
- id: TC-204
  title: Old statements are archived
  steps: Ask for a statement from 2019
  expected: The archive service answers
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
        "app.py": PAYMENTS, "tests/test_payments.py": PAYMENTS_TEST,
        "tia-services.yaml": "service: payments-service\nowner: team-payments\ncriticality: critical\n"})
    out["shas"]["base"] = base
    repo("statement-service", {
        "client.py": CLIENT, "app.py": STATEMENT_APP, "tests/test_statement.py": STATEMENT_TEST,
        "web/src/StatementRequest.jsx": STATEMENT_PAGE, "web/src/App.jsx": STATEMENT_ROUTES, "testcases/statement.yaml": STATEMENT_CASES,
        "tia-services.yaml": "service: statement-service\nowner: team-statements\n"
                             "config_keys:\n  PAYMENTS_BASE_URL: payments-service\n"
                             "entry_points:\n  GET /statements/{account_id}/pdf: Monthly statement PDF\n"})
    repo("customer-portal", {
        "client.py": PORTAL_CLIENT, "app.py": PORTAL_APP, "tests/test_portal.py": PORTAL_TEST,
        "web/src/app/payments/payments.component.html": PORTAL_PAGE, "web/src/app/payments/payments.component.ts": PORTAL_COMPONENT,
        "web/src/app/app-routing.module.ts": PORTAL_ROUTES, "testcases/portal-payments.csv": PORTAL_CASES,
        "tia-services.yaml": "service: customer-portal\nowner: team-portal\n"
                             "config_keys:\n  PAYMENTS_BASE_URL: payments-service\n"
                             "entry_points:\n  GET /portal/accounts/{account_id}/payments: Payments screen\n"})

    def branch(name, text):
        _run(p, "checkout", "-q", "-b", name, base)
        _write(p, {"app.py": text})
        out["shas"][name] = _commit(p, name)

    branch("behaviour", PAYMENTS.replace('key=lambda r: r["payment_id"])', 'key=lambda r: r["payment_id"], reverse=True)'))
    branch("move_rule", PAYMENTS.replace(
        "def ordered(rows):",
        'def exclude_disputed(rows):\n    return [r for r in rows if not r["disputed"]]\n\n\ndef ordered(rows):').replace(
        "return ordered(load_payments(account_id))", "return ordered(exclude_disputed(load_payments(account_id)))"))
    branch("signature", PAYMENTS.replace("def ordered(rows):", "def ordered(rows, newest_first):").replace(
        'key=lambda r: r["payment_id"])', 'key=lambda r: r["payment_id"], reverse=newest_first)').replace(
        "return ordered(load_payments(account_id))", "return ordered(load_payments(account_id), False)"))
    branch("signature_unfixed", PAYMENTS.replace("def ordered(rows):", "def ordered(rows, newest_first):").replace(
        'key=lambda r: r["payment_id"])', 'key=lambda r: r["payment_id"], reverse=newest_first)'))
    branch("docstring", PAYMENTS.replace('"""Payments of one account."""', '"""All payments of one account, oldest first."""'))
    branch("contract", PAYMENTS.replace("    disputed: bool\n", ""))
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))
