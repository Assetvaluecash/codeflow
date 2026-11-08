"""SQL / stored-procedure lineage landscape (PRD FR-041, AC-037). Every repository declares database 'core'.

core-db           Flyway migrations (accounts, ledger_entries) and a PL/pgSQL function post_payment() that writes ledger_entries and
                  updates accounts.balance; a view v_account_balance over accounts.
payments-service  Python (FastAPI): POST /payments calls cursor.callproc("post_payment"); balance_of() runs embedded SQL reading
                  accounts.balance.
billing-java      Java (Spring): BillingService.settle() calls "{call post_payment(?, ?)}" through JdbcTemplate.
reports-service   Python: status_report() reads accounts.status; by_table() builds its table name at run time (dynamic SQL).

Branches on core-db: drop_balance (accounts.balance dropped), drop_status (accounts.status dropped), proc_signature (post_payment gains
a parameter), proc_body (post_payment also writes audit_log).
"""
from __future__ import annotations

import json
from pathlib import Path

from fixtures.build_fixture import _commit, _run, _write

V1 = """CREATE TABLE accounts (id BIGINT PRIMARY KEY, balance NUMERIC(18,2) NOT NULL, status VARCHAR(20) NOT NULL);
CREATE TABLE ledger_entries (id BIGSERIAL PRIMARY KEY, account_id BIGINT NOT NULL, amount NUMERIC(18,2) NOT NULL);
CREATE TABLE audit_log (id BIGSERIAL PRIMARY KEY, note TEXT);
"""

PROC = """CREATE OR REPLACE FUNCTION post_payment(p_account BIGINT, p_amount NUMERIC) RETURNS void AS $$
BEGIN
  INSERT INTO ledger_entries (account_id, amount) VALUES (p_account, p_amount);
  UPDATE accounts SET balance = balance - p_amount WHERE id = p_account;
END;
$$ LANGUAGE plpgsql;
"""

VIEW = """CREATE VIEW v_account_balance AS SELECT a.id, a.balance FROM accounts a;
"""

PAYMENTS = '''from fastapi import FastAPI

app = FastAPI()


def get_cursor():
    return None


@app.post("/payments")
def create_payment(account_id: int, amount: float):
    cur = get_cursor()
    cur.callproc("post_payment", [account_id, amount])
    return {"ok": True}


def balance_of(cur, account_id):
    cur.execute("SELECT balance FROM accounts "
                "WHERE id = %s", (account_id,))
    return cur.fetchone()
'''

BILLING = '''package com.acme.billing;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class BillingService {
    private final JdbcTemplate jdbc;

    public BillingService(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    public void settle(long accountId, double amount) {
        jdbc.update("{call post_payment(?, ?)}", accountId, amount);
    }
}
'''

REPORTS = '''def status_report(cur):
    cur.execute("SELECT id, status FROM accounts WHERE status <> 'CLOSED'")
    return cur.fetchall()


def by_table(cur, table):
    cur.execute(f"SELECT * FROM {table}")
    return cur.fetchall()
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

    yaml_of = lambda svc: f"service: {svc}\ndatabase: core\n"      # noqa: E731
    p, base = repo("core-db", {"db/migration/V1__init.sql": V1, "db/procedures/post_payment.sql": PROC, "db/views/v_account_balance.sql": VIEW,
                               "tia-services.yaml": yaml_of("core-db")})
    out["shas"]["base"] = base
    repo("payments-service", {"app.py": PAYMENTS, "tia-services.yaml": yaml_of("payments-service")})
    repo("billing-java", {"src/main/java/com/acme/billing/BillingService.java": BILLING, "tia-services.yaml": yaml_of("billing-java")})
    repo("reports-service", {"reports.py": REPORTS, "tia-services.yaml": yaml_of("reports-service")})

    def branch(name, files):
        _run(p, "checkout", "-q", "-b", name, base)
        _write(p, files)
        out["shas"][name] = _commit(p, name)

    branch("drop_balance", {"db/migration/V2__drop_balance.sql": "ALTER TABLE accounts DROP COLUMN balance;\n"})
    branch("drop_status", {"db/migration/V2__drop_status.sql": "ALTER TABLE accounts DROP COLUMN status;\n"})
    branch("proc_signature", {"db/procedures/post_payment.sql": PROC.replace("p_amount NUMERIC)", "p_amount NUMERIC, p_currency CHAR(3))")})
    branch("proc_body", {"db/procedures/post_payment.sql": PROC.replace("END;\n$$", "  INSERT INTO audit_log (note) VALUES ('payment');\nEND;\n$$")})
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))
