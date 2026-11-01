"""UC1-21 (part 2): the per-tenant encrypted secret store on real PostgreSQL, the scrubber, and the end-to-end guarantee that secrets in
analysed source never reach a table, a report, a comment, the report page, the graph or the audit log."""
import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import make_app
from fixtures.build_fixture import _commit, _run, _write
from test_webhooks import SECRET as WEBHOOK_SECRET, env, gh, origins, set_pr, sign  # noqa: F401  (fixtures)
from tia.credentials import LocalKeyWrapper, SecretError, VaultTransitWrapper, VaultClient
from tia.analysis_service import make_handlers
from tia.db import Database
from tia.jobs import JobQueue
from tia.scrub import mask_literals, scrub_extract, scrub_json, scrub_text
from tia.secretstore import SecretStore, main as secretstore_cli
from tia.tenants import Tenants, tenant_tables
from tia.webhooks import ScmConfig, with_stored_token
from tia.worker import Worker
from tlsutil import FakeVault


def wrapper(kid="k1"):
    return LocalKeyWrapper({kid: os.urandom(32)}, kid)


def new_store(w=None, audit=None):
    t = conftest.new_tenant()
    return SecretStore(conftest.PG.db, t, w or wrapper(), audit), t


def admin_rows(sql, *args):
    with psycopg.connect(conftest.PG.admin) as c:
        return c.execute(sql, args).fetchall()


# =====================================================================================================================
# the secret store
# =====================================================================================================================
def test_put_get_list_delete_and_validation():
    s, _ = new_store()
    assert s.get("github/token") is None and s.names() == [] and s.delete("github/token") is False
    s.put("github/token", "ghp-value-1")
    s.put("db.password", b"\x00binary\xff")
    assert s.get_text("github/token") == "ghp-value-1" and s.get("db.password") == b"\x00binary\xff"
    assert s.names() == ["db.password", "github/token"]                              # names only: listing never returns values
    s.put("github/token", "ghp-value-2")                                              # overwrite
    assert s.get_text("github/token") == "ghp-value-2" and len(s.names()) == 2
    assert s.delete("github/token") is True and s.get("github/token") is None and s.names() == ["db.password"]
    for bad in ("", "Upper", " x", "x y", "-x", "x" * 129, "é"):
        with pytest.raises(SecretError, match="a secret name is"):
            s.put(bad, "v")
    with pytest.raises(SecretError, match="cannot be empty"):
        s.put("empty", "")


def test_secrets_are_encrypted_at_rest_and_nowhere_in_clear():
    s, t = new_store()
    value = "very-recognisable-secret-value-12345"
    s.put("api/key", value)
    s.put("api/key2", value)
    [(ct1, kv)], [(ct2,)] = admin_rows("select ciphertext, key_version from tenant_secrets where tenant_id = %s and name = 'api/key'", t), \
        admin_rows("select ciphertext from tenant_secrets where tenant_id = %s and name = 'api/key2'", t)
    assert value.encode() not in bytes(ct1) and bytes(ct1) != bytes(ct2) and kv == 1               # same value, different ciphertext
    with psycopg.connect(conftest.PG.admin) as c:                                                    # not in any column of any tenant table
        c.execute("select set_config('tia.tenant_id', %s, false)", (t,))
        for table in tenant_tables(c):
            for (row,) in c.execute(f'select x::text from "{table}" x where tenant_id = %s', (t,)):
                assert value not in row, table
    [(wrapped, kek)] = admin_rows("select wrapped_dek, kek_id from tenant_keys where tenant_id = %s", t)
    assert kek == "local:k1" and bytes(wrapped).startswith(b"L1") and len(bytes(wrapped)) > 32       # the data key is stored only wrapped


def test_tenants_are_isolated_and_a_ciphertext_cannot_be_moved():
    w = wrapper()
    a, ta = new_store(w)
    b, tb = new_store(w)
    a.put("shared-name", "A-secret")
    b.put("shared-name", "B-secret")
    assert a.get_text("shared-name") == "A-secret" and b.get_text("shared-name") == "B-secret"
    assert b.names() == ["shared-name"] and SecretStore(conftest.PG.db, tb, w).get_text("shared-name") == "B-secret"
    # an operator (or attacker with SQL access) copies A's ciphertext over B's row: it must not decrypt as B's secret
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (tb,))
        ct_a = c.execute("select ciphertext from tenant_secrets where tenant_id = %s and name = 'shared-name'", (ta,)).fetchone()[0]
        c.execute("update tenant_secrets set ciphertext = %s where tenant_id = %s and name = 'shared-name'", (ct_a, tb))
    with pytest.raises(SecretError, match="failed its integrity check"):
        b.get("shared-name")
    assert a.get_text("shared-name") == "A-secret"                                                   # A is unaffected


def test_a_row_cannot_be_renamed_or_re_versioned_or_flipped():
    s, t = new_store()
    s.put("one", "value-one")
    s.put("two", "value-two")
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (t,))
        c.execute("update tenant_secrets set name = 'swapped' where tenant_id = %s and name = 'one'", (t,))
    with pytest.raises(SecretError, match="integrity"):
        s.get("swapped")                                                                           # moved under another name
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (t,))
        ct = bytearray(c.execute("select ciphertext from tenant_secrets where tenant_id = %s and name = 'two'", (t,)).fetchone()[0])
        ct[-1] ^= 0xFF
        c.execute("update tenant_secrets set ciphertext = %s where tenant_id = %s and name = 'two'", (bytes(ct), t))
    with pytest.raises(SecretError, match="integrity"):
        s.get("two")                                                                               # a flipped bit is detected, not decrypted


def test_the_store_never_depends_on_row_level_security_alone():
    """Found through the admin CLI: a superuser connection bypasses RLS, and queries that relied on it alone read other tenants' keys.
    Every query names its tenant, so two tenants with different master keys work side by side even with RLS out of the picture."""
    admin_db = Database(conftest.PG.admin, max_size=2)
    try:
        ta, tb = conftest.new_tenant(), conftest.new_tenant()
        a, b = SecretStore(admin_db, ta, wrapper()), SecretStore(admin_db, tb, wrapper())          # different master keys
        a.put("n", "A-value")
        b.put("n", "B-value")
        assert a.get_text("n") == "A-value" and b.get_text("n") == "B-value" and a.names() == ["n"] and b.names() == ["n"]
        assert a.rotate()["key_version"] == 2 and b.rotate()["key_version"] == 2
        assert a.get_text("n") == "A-value" and b.get_text("n") == "B-value"
        assert a.delete("n") is True and b.get_text("n") == "B-value" and a.names() == []         # deleting is scoped too
        assert a.rewrap_keys() == 0
        assert admin_rows("select count(*) from tenant_keys where tenant_id = %s", tb) == [(1,)]
    finally:
        admin_db.close()


def test_first_use_race_creates_one_data_key_and_every_value_is_readable():
    w = wrapper()
    s, t = new_store(w)
    errors = []

    def work(i):
        try:
            st = SecretStore(conftest.PG.db, t, w)
            st.put(f"k{i}", f"v{i}")
            assert st.get_text(f"k{i}") == f"v{i}"
        except Exception as e:                                                                       # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(10)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert not errors, errors
    assert admin_rows("select count(*), max(version) from tenant_keys where tenant_id = %s", t) == [(1, 1)]
    assert all(s.get_text(f"k{i}") == f"v{i}" for i in range(10))


def test_rotation_reencrypts_everything_atomically_and_retires_the_old_key():
    s, t = new_store()
    for i in range(3):
        s.put(f"s{i}", f"value-{i}")
    [(old_ct,)] = admin_rows("select ciphertext from tenant_secrets where tenant_id = %s and name = 's0'", t)
    assert s.rotate() == {"key_version": 2, "secrets_reencrypted": 3}
    assert [s.get_text(f"s{i}") for i in range(3)] == [f"value-{i}" for i in range(3)]
    assert admin_rows("select version from tenant_keys where tenant_id = %s", t) == [(2,)]          # the old data key is gone
    assert admin_rows("select distinct key_version from tenant_secrets where tenant_id = %s", t) == [(2,)]
    [(new_ct,)] = admin_rows("select ciphertext from tenant_secrets where tenant_id = %s and name = 's0'", t)
    assert bytes(new_ct) != bytes(old_ct)
    # a stale copy of an old ciphertext (a restored backup row) cannot be opened any more
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (t,))
        c.execute("update tenant_secrets set ciphertext = %s, key_version = 1 where tenant_id = %s and name = 's0'", (old_ct, t))
    s2 = SecretStore(conftest.PG.db, t, s.wrapper)
    with pytest.raises(SecretError, match="data key version 1 of this tenant does not exist"):
        s2.get("s0")
    with pytest.raises(SecretError, match="rotation aborted, nothing changed"):
        s.rotate()                                                                                  # never rotate over a row it cannot read
    assert admin_rows("select version from tenant_keys where tenant_id = %s", t) == [(2,)]


def test_rotation_with_a_corrupt_secret_aborts_and_changes_nothing():
    s, t = new_store()
    s.put("good", "g")
    s.put("bad", "b")
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (t,))
        c.execute("update tenant_secrets set ciphertext = '\\x00'::bytea || ciphertext where tenant_id = %s and name = 'bad'", (t,))
    before = admin_rows("select name, key_version, ciphertext from tenant_secrets where tenant_id = %s order by name", t)
    with pytest.raises(SecretError, match="rotation aborted, nothing changed"):
        s.rotate()
    assert admin_rows("select name, key_version, ciphertext from tenant_secrets where tenant_id = %s order by name", t) == before
    assert admin_rows("select version from tenant_keys where tenant_id = %s", t) == [(1,)]


def test_master_key_rotation_rewraps_data_keys_without_touching_secrets():
    k1, k2 = os.urandom(32), os.urandom(32)
    t = conftest.new_tenant()
    old = SecretStore(conftest.PG.db, t, LocalKeyWrapper({"k1": k1}, "k1"))
    old.put("s", "kept")
    [(ct_before,)] = admin_rows("select ciphertext from tenant_secrets where tenant_id = %s", t)
    new = SecretStore(conftest.PG.db, t, LocalKeyWrapper({"k1": k1, "k2": k2}, "k2"))
    assert new.get_text("s") == "kept"                                                             # old wrapping still opens
    assert new.rewrap_keys() == 1 and new.rewrap_keys() == 0                                       # idempotent
    assert admin_rows("select kek_id from tenant_keys where tenant_id = %s", t) == [("local:k2",)]
    assert admin_rows("select ciphertext from tenant_secrets where tenant_id = %s", t) == [(ct_before,)]   # secrets untouched
    only_new = SecretStore(conftest.PG.db, t, LocalKeyWrapper({"k2": k2}, "k2"))                   # the retired master key can be dropped
    assert only_new.get_text("s") == "kept"
    unwrapped = SecretStore(conftest.PG.db, t, LocalKeyWrapper({"k1": k1}, "k1"))
    with pytest.raises(SecretError, match="integrity|not loaded"):
        unwrapped.get("s")                                                                         # k1 alone no longer opens it


def test_audit_receives_names_and_actions_never_values():
    events = []
    s, _ = new_store(audit=lambda e, **f: events.append((e, f)))
    s.put("github/token", "VALUE-MUST-NOT-APPEAR")
    s.rotate()
    s.delete("github/token")
    assert [e for e, _ in events] == ["secret_stored", "secret_keys_rotated", "secret_deleted"]
    assert "VALUE-MUST-NOT-APPEAR" not in json.dumps(events) and events[0][1] == {"name": "github/token"}


def test_the_store_works_with_vault_transit_as_the_key_encryption_key():
    v = FakeVault()
    try:
        w = VaultTransitWrapper(VaultClient(v.addr, token=v.token), "tia-kek")
        s, t = new_store(w)
        s.put("github/token", "via-transit")
        assert s.get_text("github/token") == "via-transit"
        [(wrapped, kek)] = admin_rows("select wrapped_dek, kek_id from tenant_keys where tenant_id = %s", t)
        assert bytes(wrapped).startswith(b"V1vault:v1:") and kek == "vault-transit:tia-kek"        # the KEK never reached TIA
        assert s.rotate()["secrets_reencrypted"] == 1 and s.get_text("github/token") == "via-transit"
        assert any(r["path"].startswith("/v1/transit/encrypt/") for r in v.requests)
    finally:
        v.close()


def test_offboarding_destroys_the_keys_and_so_every_copy_of_the_secrets():
    s, t = new_store()
    s.put("a", "secret-a")
    [(ct,)] = admin_rows("select ciphertext from tenant_secrets where tenant_id = %s", t)
    name = admin_rows("select name from tenants where id = %s", t)[0][0]
    cert = Tenants(conftest.PG.admin).offboard(name, name)
    assert cert["tables"]["tenant_keys"]["rows_deleted"] == 1 and cert["tables"]["tenant_secrets"]["rows_deleted"] == 1
    assert admin_rows("select count(*) from tenant_keys where tenant_id = %s", t) == [(0,)]
    # even with the ciphertext kept in a backup there is no wrapped data key left to open it with
    assert bytes(ct) and SecretStore(conftest.PG.db, t, s.wrapper).get("a") is None


def test_runtime_roles_can_use_the_store_but_not_other_tenants_keys():
    s, t = new_store()
    s.put("x", "y")
    other, to = new_store()
    other.put("x", "other-y")
    with conftest.PG.db.tx(t) as conn:
        assert conn.execute("select count(*) from tenant_secrets").fetchone()[0] == 1               # RLS: only this tenant's rows
        assert conn.execute("select count(*) from tenant_keys").fetchone()[0] == 1


# ---- command line -----------------------------------------------------------------------------------------------------------------
def test_secretstore_cli_reads_values_from_stdin_and_never_prints_them(monkeypatch, capsys):
    t = Tenants(conftest.PG.admin).create("sc-" + uuid.uuid4().hex[:8])
    base = ["--admin-dsn", conftest.PG.admin]
    assert secretstore_cli(["generate-master-key"]) == 0
    key = capsys.readouterr().out.strip()
    assert len(key) == 44
    monkeypatch.setenv("TIA_MASTER_KEY", key)

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO(stdin))
        code = secretstore_cli([*base, *argv])
        cap = capsys.readouterr()
        return code, (json.loads(cap.out) if cap.out.strip() else None), cap.err
    code, out, err = run("put", t["name"], "github/token", stdin="cli-secret-value\n")
    assert code == 0 and out == {"stored": "github/token"}, err
    code, out, err = run("list", t["name"])
    assert out == {"names": ["github/token"]} and "cli-secret-value" not in json.dumps(out) + err
    assert run("rotate", t["name"])[1] == {"key_version": 2, "secrets_reencrypted": 1}
    assert run("rewrap", t["name"])[1] == {"rewrapped": 0}
    st = SecretStore(conftest.PG.db, t["id"], LocalKeyWrapper({"k1": __import__("base64").b64decode(key)}, "k1"))
    assert st.get_text("github/token") == "cli-secret-value"
    assert run("delete", t["name"], "github/token")[1] == {"deleted": True}
    monkeypatch.delenv("TIA_MASTER_KEY")
    code, _, err = run("list", t["name"])
    assert code == 2 and "no key-encryption key is configured" in err
    Tenants(conftest.PG.admin).offboard(t["name"], t["name"])
    monkeypatch.setenv("TIA_MASTER_KEY", key)
    code, _, err = run("put", t["name"], "x", stdin="v")
    assert code == 1 and "offboarded" in err


# ---- the stored token is used by the worker and the webhook ---------------------------------------------------------------------
def test_a_stored_token_is_used_when_none_is_configured(env):
    w = wrapper()
    store = SecretStore(conftest.PG.db, env.tenant, w)
    store.put("github/token", "stored-token-777")
    no_env = replace(env.scm, github_token=None)
    assert with_stored_token(no_env, store).github_token == "stored-token-777" and with_stored_token(env.scm, store).github_token == env.scm.github_token
    assert with_stored_token(no_env, None).github_token is None
    app = make_app(env.ws, tenant=env.tenant, allow_local=True, scm=no_env, secrets=store, gate_ttl=0)
    c = TestClient(app)
    set_pr(env.origins, 61, env.fx["shas"]["breaking"])
    body = json.dumps(env.event(61, env.fx["shas"]["breaking"])).encode()
    hdr = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": uuid.uuid4().hex, "X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    r = c.post("/api/v1/webhooks/github", content=body, headers=hdr)
    assert r.status_code == 202, r.text                                                             # accepted: a token exists in the store
    h = make_handlers(conftest.PG.worker_db, allow_local=True, workspace=str(env.ws), scm=no_env, keywrapper=w)
    wk = Worker(JobQueue(conftest.PG.worker_db), h, base_delay_s=0)
    while wk.run_once():
        pass
    job = c.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded" and job["result"]["comment"] == "created"
    assert env.gh.tokens == {"Bearer stored-token-777"}                                              # GitHub saw the decrypted token
    assert "stored-token-777" not in json.dumps(c.get("/api/v1/audit").json()) + json.dumps(job)
    c2 = TestClient(make_app(env.ws, tenant=env.tenant, allow_local=True, scm=no_env, gate_ttl=0))   # no store, no env token: refused
    r = c2.post("/api/v1/webhooks/github", content=body, headers={**hdr, "X-GitHub-Delivery": uuid.uuid4().hex})
    assert r.status_code == 503 and "TIA_GITHUB_TOKEN" in r.json()["detail"]


# =====================================================================================================================
# the scrubber
# =====================================================================================================================
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
KEY_BLOCK = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAabc123\nmore/lines+here==\n-----END RSA PRIVATE KEY-----"   # tia-ci: allow-secret (fake, for the scrubber test)


@pytest.mark.parametrize("text,kind", [
    (f"token {GH} end", "github_token"), ("github_pat_" + "a" * 60, "github_token"), ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    (KEY_BLOCK, "private_key"), ("xoxb-1234567890-abcdefghij", "slack_token"),  # tia-ci: allow-secret (fake, for the scrubber test)
    ("sk_live_" + "a1B2c3D4" * 3, "stripe_key"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk", "jwt")])
def test_high_confidence_secrets_are_withheld_anywhere(text, kind):
    out, n = scrub_text(text)                                                                        # even without the generic tier
    assert n == 1 and f"[REDACTED:{kind}]" in out
    assert "A1b2C3d4" not in out and "AKIAIOSF" not in out and "MIIEow" not in out and "dBjftJeZ" not in out


def test_a_password_in_url_userinfo_is_withheld_but_the_rest_of_the_url_stays():
    out, n = scrub_text("clone https://deploy:s3cretP4ss@git.corp/o/r.git please")
    assert n == 1 and out == "clone https://deploy:[REDACTED:url_password]@git.corp/o/r.git please"
    assert scrub_text("https://git.corp/o/r.git")[1] == 0 and scrub_text("postgres://u:${DB_PASSWORD}@db/x")[1] == 0     # placeholders pass


@pytest.mark.parametrize("text", ["password=hunter2hunter2", 'API_KEY: "abcd-1234-efgh"', "CREATE USER x PASSWORD 'Sup3rS3cret'",
                                  "secret = 'topsecretvalue'", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "identified by 'pw12345'"])
def test_generic_assignments_are_withheld_in_free_text_only(text):
    assert scrub_text(text, generic=True)[1] == 1 and "[REDACTED" in scrub_text(text, generic=True)[0]
    assert scrub_text(text, generic=False)[1] == 0                                                   # ids, paths, names are never touched


@pytest.mark.parametrize("text", ["auth=settings.AUTH", "password=get_password()", "token=request.token", "password=${DB_PASSWORD}",
                                  "password=<your-password>", "secret=changeme", "token=null", "password=********", "api_key=%s",
                                  "the token was rejected", "passwords are hashed", "src/main/java/com/acme/TokenService.java",
                                  "3f2b8a9c-1d4e-4f6a-9b7c-0e1d2c3b4a59", "a" * 40 + "b" * 24])
def test_things_that_are_not_secrets_are_left_alone(text):
    assert scrub_text(text, generic=True) == (text, 0)


def test_scrub_json_keeps_structure_and_only_scrubs_where_it_should():
    obj = {"id": "svc:token=abcdef123456", "reason": "password=hunter2hunter2 in config", "path": f"/hook/{GH}/x", "n": 3, "ok": True,
           "limitations": ["secret = 'topsecretvalue'", "fine"], "items": [{"text": "token: 'abcdef123456'", "file": "a.py", "line": 4}],
           "nested": {"detail": "no secrets here", "key": "AKIAIOSFODNN7EXAMPLE"}}
    out, n = scrub_json(obj)
    assert n == 5
    assert out["id"] == "svc:token=abcdef123456"                                                    # not free text: untouched
    assert out["reason"] == "password=[REDACTED:secret] in config" and out["path"] == "/hook/[REDACTED:github_token]/x"
    assert out["limitations"] == ["secret = '[REDACTED:secret]'", "fine"] and out["items"][0]["text"] == "token: '[REDACTED:secret]'"
    assert out["nested"] == {"detail": "no secrets here", "key": "[REDACTED:aws_access_key]"} and out["n"] == 3 and out["ok"] is True
    assert obj["reason"].startswith("password=hunter2")                                              # the input is not mutated
    out2, n2 = scrub_json(obj, generic_everywhere=True)
    assert out2["id"] == "svc:token=[REDACTED:secret]" and n2 == 6


def test_literals_in_snippets_are_masked_not_kept():
    assert mask_literals("CALL grant_all('app', 'Sup3rS3cretDbPw')") == "CALL grant_all('app', '...')"
    assert mask_literals('BASE + f("abcdefghi"') == 'BASE + f("..."'                                  # cut off by truncation
    assert mask_literals("p = 'it''s a long one' + z") == "p = '...' + z" and mask_literals("x = 'ok'") == "x = 'ok'"
    assert mask_literals("a = \"esc \\\" quote inside\"") == 'a = "..."' and mask_literals("no quotes") == "no quotes"


# =====================================================================================================================
# end to end: secrets planted in source never reach any record or output
# =====================================================================================================================
AWS = "AKIAIOSFODNN7EXAMPLE"
DB_PW = "Sup3rS3cretDbPw"
DB_PW2 = "An0therS3cretPw"
HUNTER = "hunter2hunter2"
PLANTED = {"github token": GH, "aws key": AWS, "db password": DB_PW, "second password": DB_PW2, "hunter": HUNTER}

LEAKY_APP = f'''import requests
from fastapi import FastAPI

app = FastAPI()
BASE = "https://elsewhere.internal"


@app.get("/items/{{i}}")
def item(i: int):
    a = requests.get(f"https://svc.internal/hooks/{GH}/items/{{i}}").json()
    b = requests.get(BASE + lookup("password={HUNTER}"))
    c = requests.get(f"https://admin:{DB_PW}@api.internal/v1/{{i}}")
    return a, b, c
'''


@pytest.fixture
def leaky(tmp_path):
    p = tmp_path / "leaky"
    p.mkdir()
    _run(p, "init", "-q", "-b", "main")
    _write(p, {"app.py": LEAKY_APP, "src/main/resources/db/migration/V1__init.sql":
               f"CREATE TABLE t (id int PRIMARY KEY);\nCALL grant_all('app', '{DB_PW}');\n"})
    base = _commit(p, "initial")
    _run(p, "checkout", "-q", "-b", "change", "main")
    _write(p, {"src/main/resources/db/migration/V2__more.sql": f"ALTER TABLE t DROP COLUMN c;\nCALL rotate_secret('{DB_PW2}');\n",
               "app.py": LEAKY_APP.replace("def item", "def item_v2")})
    _commit(p, "change")
    _run(p, "checkout", "-q", "main")
    return str(p), base


def leak_hunt(tenant_id, client, aid):
    """Everything TIA stored or emitted about the analysis, as one searchable text."""
    parts = []
    with psycopg.connect(conftest.PG.admin) as c:
        c.execute("select set_config('tia.tenant_id', %s, false)", (tenant_id,))
        for table in tenant_tables(c):
            parts += [f"{table}: {r[0]}" for r in c.execute(f'select x::text from "{table}" x where tenant_id = %s', (tenant_id,))]
    for path in (f"/api/v1/impact-analyses/{aid}", f"/api/v1/impact-analyses/{aid}/comment", f"/analyses/{aid}",
                 f"/api/v1/impact-analyses/{aid}/evidence", f"/api/v1/impact-analyses/{aid}/graph", f"/api/v1/impact-analyses/{aid}/unknowns",
                 "/api/v1/graph?level=function", "/api/v1/graph?level=endpoint", "/api/v1/audit?limit=1000", "/api/v1/graph/versions",
                 "/api/v1/graph/relationships"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        parts.append(f"{path}: {r.text}")
    return "\n".join(parts)


def run_leaky(tmp_path, leaky):
    path, _ = leaky
    tenant = conftest.new_tenant()
    c = TestClient(make_app(tmp_path / "ws", tenant=tenant, allow_local=True, gate_ttl=0))
    assert c.post("/api/v1/repositories", json={"path": path, "name": "leaky"}).status_code == 200
    r = c.post("/api/v1/impact-analyses", json={"target": "leaky", "head": "change", "base": "main"})
    assert r.status_code == 200, r.text
    return tenant, c, r.json()["report"]


def test_secrets_in_analysed_source_never_reach_any_record_or_output(tmp_path, leaky):
    tenant, c, rep = run_leaky(tmp_path, leaky)
    hunt = leak_hunt(tenant, c, rep["analysis_id"])
    low = hunt.lower()                                      # path normalisation lower-cases: a leak need not keep its case
    for what, secret in PLANTED.items():
        assert secret.lower() not in low, f"the {what} leaked"
    assert "[REDACTED:github_token]" in hunt                 # what was withheld is marked, not silently dropped
    u = next(u for u in rep["unknowns"] if u["area"] == "secrets_withheld")
    assert u["count"] >= 1 and not u["affects_confidence"] and "rotate" in u["reason"] and u["items"][0]["repo"] == "leaky"
    assert any(x["area"] == "unparsed_migration_statements" for x in rep["unknowns"])                    # the analysis itself still worked
    assert "secret-like value(s)" in c.get(f"/analyses/{rep['analysis_id']}").text                       # the page says what was withheld
    assert c.get("/api/v1/audit/verify").json()["ok"] is True


def test_the_leak_hunt_would_find_a_leak_if_scrubbing_were_off(tmp_path, leaky, monkeypatch):
    """Control: the same search, with the scrubbers disabled, must find the planted secrets - otherwise the test above proves nothing."""
    monkeypatch.setattr("tia.extract_python.scrub_extract", lambda part: 0)
    monkeypatch.setattr("tia.impact.scrub_json", lambda r, *a, **k: (r, 0))
    monkeypatch.setattr("tia.store.scrub_json", lambda f, *a, **k: (f, 0))
    tenant, c, rep = run_leaky(tmp_path, leaky)
    hunt = leak_hunt(tenant, c, rep["analysis_id"])
    found = [what for what, secret in PLANTED.items() if secret.lower() in hunt.lower()]
    assert {"github token", "db password", "second password"} <= set(found), found                      # the hunt can see leaks
    assert not any(x["area"] == "secrets_withheld" for x in rep["unknowns"])


def test_audit_and_job_failures_never_carry_a_secret(env):
    from tia.store import Store
    store = Store(conftest.PG.db, env.tenant)
    store.audit("probe", reason=f"clone failed for https://u:{DB_PW}@host/x with token {GH} and password={HUNTER}", repo="r")
    rows = [e for e in store.audit_events(5) if e["event"] == "probe"]
    blob = json.dumps(rows)
    assert DB_PW not in blob and GH not in blob and HUNTER not in blob and "[REDACTED" in blob and rows[0]["repo"] == "r"
    assert store.verify_audit()["ok"] is True                                                           # the chain is over the scrubbed record
    q = JobQueue(conftest.PG.db)
    kind = "k-" + uuid.uuid4().hex
    jid = q.enqueue(env.tenant, kind, {}, key="e")["job_id"]
    job = JobQueue(conftest.PG.worker_db).claim("w", kinds=[kind])
    JobQueue(conftest.PG.worker_db).fail(job, f"boom: password={HUNTER} {GH}", permanent=True)
    err = q.get(env.tenant, jid)["error"]
    assert HUNTER not in err and GH not in err and "boom" in err


def test_scrub_extract_leaves_resolution_facts_alone(tmp_path):
    from tia.extract_python import extract_repo
    ex = extract_repo("r", "c", {"app.py": "import requests\ndef f(i):\n    return requests.get(f'https://svc.internal/v1/items/{i}')\n"})
    assert ex.secrets_withheld == 0 and ex.callsites[0].norm_path == "/v1/items/{}" and ex.callsites[0].base_value == "svc.internal"
    leaky_ex = extract_repo("r", "c", {"app.py": LEAKY_APP})
    assert leaky_ex.secrets_withheld >= 1
    assert all(GH not in (c.norm_path or "") and HUNTER not in (c.reason or "") for c in leaky_ex.callsites)
    assert {c.base_value for c in leaky_ex.callsites if c.base_value} >= {"svc.internal", "api.internal"}      # hosts still resolve; userinfo dropped
