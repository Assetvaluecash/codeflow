"""SQL and stored-procedure lineage (PRD FR-041, AC-037): procedures, views and triggers in PostgreSQL, Oracle, SQL Server and DB2 syntax;
SQL embedded in Python, Java, C# and other code; calling services reported with SQL evidence; dynamic SQL reported as an unknown."""
from types import SimpleNamespace as NS

import pytest

from fixtures.build_sql_fixture import build as build_sql
from tia.extraction import extract_commit
from tia.impact import AnalysisRequest, analyze
from tia.sqllineage import embedded, parse_definitions, statement_refs


@pytest.mark.parametrize("sql,reads,writes,calls", [
    ("SELECT a.id FROM accounts a JOIN customers c ON c.id = a.cid WHERE x IN (SELECT y FROM limits)", ["accounts", "customers", "limits"], [], []),
    ("INSERT INTO ledger.entries (a, b) SELECT a, b FROM staging_entries", ["staging_entries"], ["entries"], []),
    ("UPDATE [dbo].[Accounts] SET balance = 0 WHERE id = @id", [], ["accounts"], []),
    ("DELETE FROM audit_trail WHERE ts < now()", [], ["audit_trail"], []),
    ("MERGE INTO balances b USING daily_totals d ON (b.id = d.id) WHEN MATCHED THEN UPDATE SET b.v = d.v", ["daily_totals"], ["balances"], []),
    ("WITH recent AS (SELECT * FROM payments) SELECT * FROM recent, fx_rates", ["fx_rates", "payments"], [], []),
    ("SELECT * FROM a, b WHERE a.id = b.id", ["a", "b"], [], []),
    ("{call post_payment(?, ?)}", [], [], ["post_payment"]),
    ("{? = call billing.total(?)}", [], [], ["total"]),
    ("EXEC dbo.usp_close_day @d", [], [], ["usp_close_day"]),
    ("CALL archive_day(1)", [], [], ["archive_day"]),
    ("TRUNCATE TABLE staging", [], ["staging"], []),
    ("SELECT 1 FROM dual", [], [], []),
    ("SELECT * FROM generate_series(1, 3)", [], [], []),
    ("INSERT INTO #tmp SELECT * FROM orders", ["orders"], [], []),
    ("SELECT x FROM sysibm.sysdummy1", [], [], []),
])
def test_statement_references(sql, reads, writes, calls):
    r = statement_refs(sql)
    assert (r["reads"], r["writes"], r["calls"], r["dynamic"]) == (reads, writes, calls, None)


@pytest.mark.parametrize("sql", ["EXECUTE IMMEDIATE v_sql", "EXEC sp_executesql @stmt", "EXEC (@sql)", "EXECUTE format('SELECT * FROM %I', t)",
                                 "SELECT * FROM __tia_dynamic__ WHERE id = 1"])
def test_dynamic_sql_is_never_read_as_no_access(sql):
    assert statement_refs(sql)["dynamic"]


DEFS = """
CREATE OR REPLACE FUNCTION post_payment(p_id bigint, p_amount numeric) RETURNS void AS $$
BEGIN
  INSERT INTO ledger_entries(payment_id, amount) VALUES (p_id, p_amount);
  UPDATE accounts SET balance = balance - p_amount WHERE id = p_id;
  PERFORM notify_ops(p_id);
END;
$$ LANGUAGE plpgsql;

CREATE PROCEDURE dbo.usp_close_day @d date AS
BEGIN
  INSERT INTO #tmp SELECT * FROM ledger_entries;
  DELETE FROM pending_payments WHERE day = @d;
  EXEC sp_executesql @sql;
END
GO

CREATE OR REPLACE PACKAGE BODY billing AS
  PROCEDURE charge(p_id NUMBER) IS
  BEGIN
    UPDATE invoices SET status = 'PAID' WHERE id = p_id;
  END charge;
  FUNCTION total(p_id NUMBER) RETURN NUMBER IS
    v NUMBER;
  BEGIN
    SELECT SUM(amount) INTO v FROM invoice_lines WHERE invoice_id = p_id;
    RETURN v;
  END total;
END billing;
/
CREATE PROCEDURE ARCHIVE_DAY (IN P_DAY DATE) LANGUAGE SQL
BEGIN
  INSERT INTO ARCHIVE.PAYMENTS SELECT * FROM PAYMENTS WHERE DAY = P_DAY;
END
@
CREATE TRIGGER trg_accounts_audit AFTER UPDATE ON accounts FOR EACH ROW EXECUTE FUNCTION audit_accounts();
CREATE VIEW v_balances AS SELECT a.id, a.balance FROM accounts a;
"""


def test_procedure_definitions_in_four_dialects():
    procs, unk = parse_definitions("db/all.sql", DEFS)
    got = {p["name"]: (p["kind"], p["reads"], p["writes"], p["calls"]) for p in procs}
    assert got["post_payment"] == ("function", [], ["accounts", "ledger_entries"], ["notify_ops"])                 # PostgreSQL
    assert got["usp_close_day"] == ("procedure", ["ledger_entries"], ["pending_payments"], [])                    # SQL Server
    assert got["billing.charge"] == ("procedure", [], ["invoices"], [])                                          # Oracle package body
    assert got["billing.total"] == ("function", ["invoice_lines"], [], [])
    assert got["archive_day"] == ("procedure", ["payments"], ["payments"], [])                                    # DB2 (schema dropped)
    assert got["trg_accounts_audit"][0] == "trigger" and next(p for p in procs if p["name"] == "trg_accounts_audit")["on_table"] == "accounts"
    assert got["v_balances"] == ("view", ["accounts"], [], [])
    assert [u["reason"] for u in unk] == ["procedure usp_close_day: dynamic SQL (EXECUTE IMMEDIATE / sp_executesql / EXEC(@sql)) cannot be read"]


def test_embedded_sql_in_several_languages_is_attributed_to_its_function():
    py = 'def pay(cur, pid):\n    cur.execute("UPDATE accounts SET balance = 0 "\n                "WHERE id = %s", (pid,))\n    cur.callproc("post_payment", [pid])\n\n' \
         'def report(cur, table):\n    cur.execute("SELECT * FROM " + table)\n'
    acc, unk, _ = embedded("a.py", py, [NS(id="r:a.py::pay", line=1, end_line=4), NS(id="r:a.py::report", line=6, end_line=7)])
    assert [(a["function_id"].split("::")[1], a["op"], a["target"]) for a in acc] == [("pay", "call", "post_payment"), ("pay", "write", "accounts")]
    assert len(unk) == 1 and "report" in unk[0]["reason"] and unk[0]["kind"] == "dynamic_sql"
    java = 'class R {\n  void close() {\n    jdbc.update("INSERT INTO payments_archive SELECT * FROM payments WHERE day = ?", d);\n' \
           '    new SimpleJdbcCall(jdbc).withProcedureName("usp_close_day").execute(d);\n  }\n}\n'
    acc, _, _ = embedded("R.java", java, [NS(id="r:R.java::R.close", line=2, end_line=5)])
    assert {(a["op"], a["target"]) for a in acc} == {("call", "usp_close_day"), ("read", "payments"), ("write", "payments_archive")}
    cs = 'void Close() {\n    using var cmd = new SqlCommand("dbo.usp_close_day", conn);\n    cmd.CommandType = CommandType.StoredProcedure;\n' \
         '    var q = $"SELECT * FROM {tableName}";\n}\n'
    acc, unk, _ = embedded("C.cs", cs, [NS(id="r:C.cs::X.Close", line=1, end_line=5)])
    assert [(a["op"], a["target"]) for a in acc] == [("call", "usp_close_day")] and len(unk) == 1
    ts = 'async function list(db) {\n  return db.query(`SELECT id, name FROM customers WHERE region = $1`, [r]);\n}\n'
    acc, _, _ = embedded("svc.ts", ts, [NS(id="r:svc.ts::list", line=1, end_line=3)])
    assert [(a["op"], a["target"]) for a in acc] == [("read", "customers")]


# ---- AC-037 on real repositories ---------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sqlfx(tmp_path_factory):
    return build_sql(str(tmp_path_factory.mktemp("sql")))


def run(fx, head):
    return analyze(AnalysisRequest(repos=fx["paths"], target="core-db", base="main", head=head), None)


def test_extraction_of_the_landscape(sqlfx):
    ex = {n: extract_commit(n, p, "main", None)[0] for n, p in sqlfx["paths"].items()}
    assert {p["name"] for p in ex["core-db"].procedures} == {"post_payment", "v_account_balance"}
    assert [(a["op"], a["target"]) for a in ex["billing-java"].sql_access] == [("call", "post_payment")]
    assert not ex["billing-java"].data_unknowns                                   # its JDBC statements were all read
    assert [u["kind"] for u in ex["reports-service"].data_unknowns] == ["dynamic_sql"]


def test_a_procedure_writing_a_changed_column_reports_its_calling_services_with_sql_evidence(sqlfx):
    rep = run(sqlfx, "drop_balance")
    by = {(f["component"], f["node_type"], f["node"].rsplit("::", 1)[-1]): f for f in rep["findings"]}
    settle = by[("billing-java", "function", "BillingService.settle")]
    assert settle["impact_type"] == "data" and settle["resolution_strategy"] == "sql" and settle["evidence_status"] == "source_confirmed"
    assert "calls function post_payment" in settle["reason"] and "post_payment writes column accounts.balance" in settle["reason"]
    assert settle["evidence"]["sql"] == "{call post_payment(?, ?)}" and settle["severity"] == "high"
    pay = by[("payments-service", "function", "create_payment")]
    assert 'callproc("post_payment"' in pay["reason"]
    assert ("payments-service", "function", "balance_of") in by                   # embedded SQL reading the column
    assert ("payments-service", "endpoint", "proc:core:post_payment") not in by and any(
        f["node_type"] == "endpoint" and f["component"] == "payments-service" for f in rep["findings"])   # POST /payments, transitively
    assert {f["node"] for f in rep["findings"] if f["node_type"] == "procedure"} == {"proc:core:post_payment", "proc:core:v_account_balance"}
    assert "reports-service" not in {f["component"] for f in rep["findings"]}    # it reads accounts.status only
    assert "data_change_not_mapped" not in {u["area"] for u in rep["unknowns"]}  # SQL lineage maps the column


def test_column_precision_and_dynamic_sql(sqlfx):
    rep = run(sqlfx, "drop_status")
    assert [(f["component"], f["node"].rsplit("::", 1)[-1]) for f in rep["findings"]] == [("reports-service", "status_report")]
    u = next(u for u in rep["unknowns"] if u["area"] == "data_access_not_analysed")
    assert any(i["kind"] == "dynamic_sql" and "by_table" in i["reason"] for i in u["items"])


def test_procedure_changes_reach_callers(sqlfx):
    sig = run(sqlfx, "proc_signature")
    comp = next(c for c in sig["change"]["changed_components"] if c["type"] == "db_procedure")
    assert comp["change"] == "procedure_signature_changed" and comp["breaking"]
    callers = {f["node"].rsplit("::", 1)[-1] for f in sig["findings"] if f["node_type"] == "function"}
    assert callers == {"BillingService.settle", "create_payment"} and {f["severity"] for f in sig["findings"]} == {"high"}
    body = run(sqlfx, "proc_body")
    comp = next(c for c in body["change"]["changed_components"] if c["type"] == "db_procedure")
    assert comp["change"] == "procedure_body_changed" and not comp["breaking"] and "now writes audit_log" in comp["detail"]
    assert {f["severity"] for f in body["findings"]} == {"medium"}


def test_an_unknown_procedure_is_an_unknown_not_a_clean_result(sqlfx, tmp_path):
    import shutil
    from fixtures.build_fixture import _commit, _run
    p = tmp_path / "payments-service"
    shutil.copytree(sqlfx["paths"]["payments-service"], p)
    (p / "app.py").write_text((p / "app.py").read_text().replace('callproc("post_payment"', 'callproc("post_refund"'), encoding="utf-8")
    _commit(p, "calls a procedure defined elsewhere")
    paths = {**sqlfx["paths"], "payments-service": str(p)}
    rep = analyze(AnalysisRequest(repos=paths, target="core-db", base="main", head="drop_balance"), None)
    u = next(u for u in rep["unknowns"] if u["area"] == "procedures_not_found")
    assert u["items"][0]["procedure"] == "post_refund" and u["affects_confidence"]
    _run(p, "log", "-1")
