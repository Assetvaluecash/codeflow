"""UC1-01 / UC1-02: tenant isolation (row-level security) and the tamper-evident audit chain."""
import threading
import uuid

import psycopg
import pytest

import conftest
from conftest import fresh_store, new_tenant
from tia import db as tdb
from tia.extract_python import extract_repo
from tia.repos import Repo


def repo(name="svc"):
    return Repo(name=name, url="x", provider="git", kind="remote", path="/p", default_branch="main")


# ---- isolation -----------------------------------------------------------------------------------------
def test_tenants_cannot_see_each_others_data():
    a, b = fresh_store(), fresh_store()
    a.put_repo(repo("payments")); a.put_analysis({"analysis_id": "A1", "status": "complete"})
    a.put_extract(extract_repo("payments", "c1", {"x.py": "x=1\n"}))
    assert [r.name for r in a.list_repos()] == ["payments"]
    assert b.list_repos() == [] and b.get_repo("payments") is None
    assert b.get_analysis("A1") is None and b.get_extract("payments", "c1", "py-1") is None
    b.put_repo(repo("payments"))                                  # same name in another tenant is independent
    b.delete_repo("payments")
    assert [r.name for r in a.list_repos()] == ["payments"]       # b's delete did not touch a


def test_session_without_tenant_sees_nothing_and_cannot_write():
    a = fresh_store(); a.put_repo(repo())
    with psycopg.connect(conftest.PG.app_dsn) as c:               # runtime role, no tenant set
        assert c.execute("select count(*) from repositories").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into repositories (tenant_id, name, data) values (%s, 'x', '{}')", (a.tenant_id,))


def test_rls_blocks_writing_rows_for_another_tenant():
    a, b = fresh_store(), fresh_store()
    with conftest.PG.db.tx(a.tenant_id) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into repositories (tenant_id, name, data) values (%s, 'evil', '{}')", (b.tenant_id,))


def test_missing_where_clause_cannot_leak():
    """The point of RLS: even `select *` / `delete` with no filter only touch the bound tenant."""
    a, b = fresh_store(), fresh_store()
    a.put_repo(repo("one")); b.put_repo(repo("two")); b.put_repo(repo("three"))
    with conftest.PG.db.tx(a.tenant_id) as c:
        assert [r[0] for r in c.execute("select name from repositories")] == ["one"]
        c.execute("delete from repositories")                        # unfiltered delete
    assert sorted(r.name for r in b.list_repos()) == ["three", "two"]


def test_runtime_role_is_not_privileged_and_cannot_read_tenant_registry():
    with psycopg.connect(conftest.PG.app_dsn) as c:
        sup, byp = c.execute("select rolsuper, rolbypassrls from pg_roles where rolname = current_user").fetchone()
        assert (sup, byp) == (False, False)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from tenants")


def test_rls_is_forced_on_every_tenant_table():
    with psycopg.connect(conftest.PG.admin) as c:
        rows = dict(((n), (e, f)) for n, e, f in c.execute(
            "select relname, relrowsecurity, relforcerowsecurity from pg_class where relname in "
            "('repositories','extractions','analyses','audit_log')"))
    assert rows and all(v == (True, True) for v in rows.values()), rows


def test_tenant_id_must_be_a_uuid():
    with pytest.raises(ValueError):
        with conftest.PG.db.tx("not-a-uuid"):
            pass


# ---- migrations / registry --------------------------------------------------------------------------------
def test_migrations_are_idempotent_and_tenant_ensure_is_stable():
    assert tdb.migrate(conftest.PG.admin, conftest.APP_PW) == []     # already applied
    name = f"t-{uuid.uuid4().hex[:8]}"
    assert conftest.PG.tenants.ensure(name) == conftest.PG.tenants.ensure(name)
    assert conftest.PG.tenants.get(name)["status"] == "active"
    assert conftest.PG.tenants.get("nope") is None
    assert any(t["name"] == name for t in conftest.PG.tenants.list())


# ---- audit chain -----------------------------------------------------------------------------------------
def test_audit_chain_verifies_and_is_per_tenant():
    a, b = fresh_store(), fresh_store()
    for i in range(5):
        a.audit("ev", n=i, who="x")
    b.audit("other")
    assert a.verify_audit() == {"ok": True, "entries": 5, "first_bad_seq": None}
    assert b.verify_audit()["entries"] == 1
    assert [e["seq"] for e in a.audit_events()] == [5, 4, 3, 2, 1]
    assert fresh_store().verify_audit() == {"ok": True, "entries": 0, "first_bad_seq": None}


def test_audit_tampering_is_detected():
    a = fresh_store()
    for i in range(4):
        a.audit("ev", n=i)
    with psycopg.connect(conftest.PG.admin, autocommit=True) as admin:       # an attacker with owner access
        admin.execute("update audit_log set payload = '{\"n\":999}' where tenant_id = %s and seq = 2", (a.tenant_id,))
    assert a.verify_audit() == {"ok": False, "entries": 4, "first_bad_seq": 2}


def test_audit_deletion_and_reordering_are_detected():
    a = fresh_store()
    for i in range(4):
        a.audit("ev", n=i)
    with psycopg.connect(conftest.PG.admin, autocommit=True) as admin:
        admin.execute("delete from audit_log where tenant_id = %s and seq = 3", (a.tenant_id,))
    res = a.verify_audit()
    assert res["ok"] is False and res["first_bad_seq"] == 4          # gap detected at the next entry


def test_audit_is_append_only_for_the_runtime_role():
    a = fresh_store(); a.audit("ev")
    with conftest.PG.db.tx(a.tenant_id) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update audit_log set event = 'x'")
    with conftest.PG.db.tx(a.tenant_id) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from audit_log")
    assert a.verify_audit()["ok"]


def test_concurrent_audit_writes_keep_one_valid_chain():
    a = fresh_store()
    errs = []

    def worker(k):
        try:
            for i in range(8):
                a.audit("ev", worker=k, i=i)
        except Exception as e:           # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    assert a.verify_audit() == {"ok": True, "entries": 48, "first_bad_seq": None}


def test_audit_payload_roundtrips_exactly():
    a = fresh_store()
    a.audit("ev", text="ünïcode \"quotes\"", nested={"b": 1, "a": [1, 2]}, when=None)
    e = a.audit_events(1)[0]
    assert e["text"] == "ünïcode \"quotes\"" and e["nested"] == {"a": [1, 2], "b": 1} and e["when"] is None
    assert a.verify_audit()["ok"]


def test_keyed_audit_chain_resists_a_database_owner_who_recomputes_hashes():
    from tia.store import Store, _chain_hash
    keyed = Store(conftest.PG.db, new_tenant(), audit_key=b"secret-outside-db")
    for i in range(3):
        keyed.audit("ev", n=i)
    assert keyed.verify_audit()["ok"]
    # attacker edits row 2 and rewrites every later hash with the unkeyed algorithm
    with psycopg.connect(conftest.PG.admin, autocommit=True) as admin:
        rows = admin.execute("select seq, ts, event, payload from audit_log where tenant_id = %s order by seq",
                             (keyed.tenant_id,)).fetchall()
        prev = "GENESIS"
        for seq, ts, ev, payload in rows:
            if seq == 2:
                payload = '{"n":999}'
            h = _chain_hash(prev, seq, ts, ev, payload)                       # no key available to the attacker
            admin.execute("update audit_log set payload=%s, prev_hash=%s, hash=%s where tenant_id=%s and seq=%s",
                          (payload, prev, h, keyed.tenant_id, seq))
            prev = h
    assert keyed.verify_audit()["ok"] is False
    # control: the same attack against an UNKEYED chain succeeds, which is why the key matters
    plain = fresh_store()
    for i in range(3):
        plain.audit("ev", n=i)
    with psycopg.connect(conftest.PG.admin, autocommit=True) as admin:
        rows = admin.execute("select seq, ts, event, payload from audit_log where tenant_id = %s order by seq",
                             (plain.tenant_id,)).fetchall()
        prev = "GENESIS"
        for seq, ts, ev, payload in rows:
            if seq == 2:
                payload = '{"n":999}'
            h = _chain_hash(prev, seq, ts, ev, payload)
            admin.execute("update audit_log set payload=%s, prev_hash=%s, hash=%s where tenant_id=%s and seq=%s",
                          (payload, prev, h, plain.tenant_id, seq))
            prev = h
    assert plain.verify_audit()["ok"] is True
