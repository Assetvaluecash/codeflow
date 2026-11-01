"""UC1-21 (part 1): where secrets come from, key wrapping, and encryption in transit. Vault is a local fake that enforces tokens and
Transit context binding (no Vault in this environment: simulated verification, with real TLS handshakes wherever TLS is involved)."""
import base64
import os
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn
from fastapi.testclient import TestClient

import conftest
from conftest import make_app
from tia.credentials import (LocalKeyWrapper, SecretError, VaultClient, VaultTransitWrapper, env_secret, is_loopback, keywrapper_from_env,
                             read_secret_file, require_secure_url, resolve)
from tia.repos import IngestError, RepoService
from tia.transport import TransportError, check_db_tls, check_outbound, tls_options
from tia.webhooks import ScmConfig
from tlsutil import FakeVault, make_pki


@pytest.fixture
def vault():
    v = FakeVault()
    yield v
    v.close()


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


def client(v, **kw):
    return VaultClient(v.addr, token=kw.pop("token", v.token), **kw)


# ---- references ---------------------------------------------------------------------------------------------------------------
def test_literals_files_and_env_references(tmp_path, monkeypatch):
    assert resolve(None) is None and resolve("plain-value") == "plain-value"
    f = tmp_path / "token"
    f.write_bytes(b"s3cr3t-from-file\r\n\n")
    assert resolve(f"file:{f}") == "s3cr3t-from-file" and read_secret_file(str(f)) == "s3cr3t-from-file"      # trailing newlines only
    (tmp_path / "padded").write_text("  keep inner and leading space  \n")
    assert read_secret_file(str(tmp_path / "padded")) == "  keep inner and leading space  "
    (tmp_path / "empty").write_text("\n")
    for bad, msg in ((str(tmp_path / "empty"), "is empty"), (str(tmp_path / "missing"), "cannot be read")):
        with pytest.raises(SecretError, match=msg):
            read_secret_file(bad)
    monkeypatch.setenv("OTHER_VAR", "from-env")
    assert resolve("env:OTHER_VAR") == "from-env"
    with pytest.raises(SecretError, match="NOPE_VAR"):
        resolve("env:NOPE_VAR")
    with pytest.raises(SecretError, match="looks like vault:<mount>/<path>#<key>"):
        resolve("vault:secret/only-path")


def test_name_file_convention_and_ambiguity(tmp_path, monkeypatch):
    f = tmp_path / "db_pw"
    f.write_text("pw-123\n")
    monkeypatch.delenv("TIA_X", raising=False)
    monkeypatch.setenv("TIA_X_FILE", str(f))
    assert env_secret("TIA_X") == "pw-123"
    monkeypatch.setenv("TIA_X", "also-set")
    with pytest.raises(SecretError, match="both TIA_X and TIA_X_FILE are set"):
        env_secret("TIA_X")                                                           # an ambiguity is an error, never a guess
    monkeypatch.delenv("TIA_X_FILE")
    assert env_secret("TIA_X") == "also-set" and env_secret("TIA_UNSET", "dflt") == "dflt" and env_secret("TIA_UNSET") is None
    monkeypatch.setenv("TIA_X", f"file:{f}")
    assert env_secret("TIA_X") == "pw-123"


def test_loopback_and_secure_url_rules():
    assert all(is_loopback(h) for h in ("localhost", "127.0.0.1", "127.9.9.9", "::1", "[::1]", "LOCALHOST"))
    assert not any(is_loopback(h) for h in ("example.com", "10.0.0.1", "127.example.com", "", None))
    assert require_secure_url("https://vault.corp:8200", "x") and require_secure_url("http://127.0.0.1:8200", "x")
    for bad in ("http://vault.corp:8200", "ftp://vault.corp", "vault.corp"):
        with pytest.raises(SecretError, match="must use https"):
            require_secure_url(bad, "VAULT_ADDR")
    assert require_secure_url("http://vault.corp:8200", "x", allow_insecure=True)


# ---- Vault --------------------------------------------------------------------------------------------------------------------
def test_vault_kv_references_and_errors(vault, monkeypatch):
    vault.kv["secret/tia/github"] = {"token": "ghp-from-vault", "other": "x"}
    monkeypatch.setenv("VAULT_ADDR", vault.addr)
    monkeypatch.setenv("VAULT_TOKEN", vault.token)
    monkeypatch.setenv("VAULT_NAMESPACE", "team-a")
    assert resolve("vault:secret/tia/github#token") == "ghp-from-vault"
    assert vault.requests[-1]["namespace"] == "team-a" and vault.requests[-1]["token"] == vault.token
    assert vault.requests[-1]["path"] == "/v1/secret/data/tia/github"
    with pytest.raises(SecretError, match="has no key 'nope'"):
        resolve("vault:secret/tia/github#nope")
    with pytest.raises(SecretError, match=r"failed \(HTTP 404\)"):
        resolve("vault:secret/tia/missing#token")
    c = client(vault)
    c.kv_put("secret", "tia/new", {"a": "1"})
    assert c.kv_get("secret", "tia/new") == {"a": "1"} and vault.kv["secret/tia/new"] == {"a": "1"}
    monkeypatch.setenv("TIA_GH", "vault:secret/tia/github#token")
    assert env_secret("TIA_GH") == "ghp-from-vault"                                   # a variable can be a Vault reference


def test_vault_errors_never_echo_the_response_or_the_token(vault):
    wrong = client(vault, token="s.wrong-token-12345")
    with pytest.raises(SecretError) as e:
        wrong.kv_get("secret", "a", "k")
    assert "HTTP 403" in str(e.value) and "s.wrong-token-12345" not in str(e.value) and "permission denied" not in str(e.value)
    vault.fail_status = 500
    with pytest.raises(SecretError) as e:
        client(vault).kv_get("secret", "a", "k")
    assert "HTTP 500" in str(e.value) and "secret-detail-that-must-not-be-echoed" not in str(e.value)
    vault.close()
    with pytest.raises(SecretError, match="Vault is unreachable"):
        client(vault, timeout=1).kv_get("secret", "a", "k")


def test_vault_token_file_is_reread_so_a_rotated_token_works(vault, tmp_path):
    tf = tmp_path / "vault-token"
    tf.write_text("s.old-token\n")
    vault.kv["secret/a"] = {"k": "v"}
    c = VaultClient(vault.addr, token_file=str(tf))
    with pytest.raises(SecretError, match="HTTP 403"):
        c.kv_get("secret", "a", "k")                                                  # the file holds a stale token
    tf.write_text(vault.token + "\n")                                                  # the agent rotated it; no restart needed
    assert c.kv_get("secret", "a", "k") == "v"


def test_vault_needs_a_token_and_https(monkeypatch):
    with pytest.raises(SecretError, match="VAULT_TOKEN or VAULT_TOKEN_FILE"):
        VaultClient("https://vault.corp:8200")
    with pytest.raises(SecretError, match="must use https"):
        VaultClient("http://vault.corp:8200", token="t")
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with pytest.raises(SecretError, match="VAULT_ADDR is not set"):
        VaultClient.from_env()


def test_vault_over_real_tls_trusts_only_the_configured_ca(pki):
    v = FakeVault(pki=pki)
    try:
        v.kv["secret/a"] = {"k": "tls-secret"}
        assert v.addr.startswith("https://")
        assert VaultClient(v.addr, token=v.token, ca_file=pki["ca"]).kv_get("secret", "a", "k") == "tls-secret"     # real handshake
        with pytest.raises(SecretError, match="unreachable"):
            VaultClient(v.addr, token=v.token, ca_file=pki["other_ca"]).kv_get("secret", "a", "k")                  # untrusted issuer
        with pytest.raises(SecretError, match="unreachable"):
            VaultClient(v.addr, token=v.token).kv_get("secret", "a", "k")                                           # system store: unknown CA
        assert all(r["token"] == v.token for r in v.requests)
    finally:
        v.close()


# ---- key wrapping -------------------------------------------------------------------------------------------------------------
def test_local_key_wrapper_binds_context_and_detects_tampering():
    k1 = os.urandom(32)
    w = LocalKeyWrapper({"k1": k1}, "k1")
    dek = os.urandom(32)
    wrapped = w.wrap(dek, b"ctx-a")
    assert dek not in wrapped and w.unwrap(wrapped, b"ctx-a") == dek and w.id == "local:k1"
    assert w.wrap(dek, b"ctx-a") != wrapped                                            # random nonce each time
    with pytest.raises(SecretError, match="integrity"):
        w.unwrap(wrapped, b"ctx-b")                                                     # a key wrapped for one tenant cannot open for another
    bad = bytearray(wrapped)
    bad[-1] ^= 1
    with pytest.raises(SecretError, match="integrity"):
        w.unwrap(bytes(bad), b"ctx-a")
    with pytest.raises(SecretError, match="not produced by a local master key"):
        w.unwrap(b"V1nonsense", b"ctx-a")
    with pytest.raises(SecretError, match="32 bytes"):
        LocalKeyWrapper({"k1": b"short"}, "k1")
    with pytest.raises(SecretError, match="not among"):
        LocalKeyWrapper({"k1": k1}, "k2")


def test_previous_master_keys_open_old_wrappings_until_they_are_rewrapped():
    k1, k2 = os.urandom(32), os.urandom(32)
    old = LocalKeyWrapper({"k1": k1}, "k1")
    wrapped = old.wrap(b"d" * 32, b"c")
    both = LocalKeyWrapper({"k1": k1, "k2": k2}, "k2")
    assert both.unwrap(wrapped, b"c") == b"d" * 32 and both.id == "local:k2"
    only_new = LocalKeyWrapper({"k2": k2}, "k2")
    with pytest.raises(SecretError, match="master key 'k1' is not loaded"):
        only_new.unwrap(wrapped, b"c")
    assert only_new.unwrap(both.wrap(b"d" * 32, b"c"), b"c") == b"d" * 32


def test_keywrapper_from_env(monkeypatch, tmp_path):
    for v in ("TIA_MASTER_KEY", "TIA_MASTER_KEY_PREVIOUS", "TIA_MASTER_KEY_ID", "TIA_KEK_PROVIDER", "TIA_MASTER_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    assert keywrapper_from_env() is None                                               # no key configured: the store is simply absent
    a, b = LocalKeyWrapper.generate(), LocalKeyWrapper.generate()
    monkeypatch.setenv("TIA_MASTER_KEY", a)
    w = keywrapper_from_env()
    assert w.id == "local:k1" and len(w.keys["k1"]) == 32
    monkeypatch.setenv("TIA_MASTER_KEY", f"2026a:{a},2026b:{b}")
    monkeypatch.setenv("TIA_MASTER_KEY_ID", "2026b")
    assert keywrapper_from_env().id == "local:2026b"
    monkeypatch.setenv("TIA_MASTER_KEY", b)
    monkeypatch.setenv("TIA_MASTER_KEY_ID", "k1")
    monkeypatch.setenv("TIA_MASTER_KEY_PREVIOUS", f"old:{a}")
    w = keywrapper_from_env()
    assert set(w.keys) == {"k1", "old"} and w.current == "k1"
    f = tmp_path / "mk"
    f.write_text(a + "\n")
    monkeypatch.delenv("TIA_MASTER_KEY")
    monkeypatch.setenv("TIA_MASTER_KEY_FILE", str(f))
    monkeypatch.delenv("TIA_MASTER_KEY_PREVIOUS")
    assert keywrapper_from_env().keys["k1"] == base64.b64decode(a)                      # from a mounted secret file
    monkeypatch.delenv("TIA_MASTER_KEY_FILE")
    monkeypatch.setenv("TIA_MASTER_KEY", "not base64!!")
    with pytest.raises(SecretError, match="must be base64"):
        keywrapper_from_env()
    monkeypatch.delenv("TIA_MASTER_KEY")
    monkeypatch.setenv("TIA_KEK_PROVIDER", "local")
    with pytest.raises(SecretError, match="needs TIA_MASTER_KEY"):
        keywrapper_from_env()


def test_vault_transit_wrapper_keeps_the_key_in_vault_and_binds_context(vault):
    w = VaultTransitWrapper(client(vault), "tia-kek")
    dek = os.urandom(32)
    wrapped = w.wrap(dek, b"tenant-a:1")
    assert wrapped.startswith(b"V1vault:v1:") and dek not in wrapped and w.id == "vault-transit:tia-kek"
    assert w.unwrap(wrapped, b"tenant-a:1") == dek
    with pytest.raises(SecretError, match=r"failed \(HTTP 400\)"):
        w.unwrap(wrapped, b"tenant-b:1")                                                 # context bound by Vault itself
    with pytest.raises(SecretError, match="not produced by Vault Transit"):
        w.unwrap(b"L1xx", b"c")
    assert [r["path"] for r in vault.requests if "transit" in r["path"]][:1] == ["/v1/transit/encrypt/tia-kek"]
    assert "plaintext" in vault.requests[0]["body"] and "context" in vault.requests[0]["body"]


def test_transit_provider_from_env(vault, monkeypatch):
    monkeypatch.setenv("TIA_KEK_PROVIDER", "vault-transit")
    monkeypatch.setenv("TIA_KEK_TRANSIT_KEY", "my-kek")
    monkeypatch.setenv("VAULT_ADDR", vault.addr)
    monkeypatch.setenv("VAULT_TOKEN", vault.token)
    w = keywrapper_from_env()
    assert isinstance(w, VaultTransitWrapper) and w.id == "vault-transit:my-kek"


# ---- encryption in transit ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("dsn,ok", [
    ("postgresql://u:p@localhost/db", True), ("postgresql://u:p@127.0.0.1:5432/db", True), ("postgresql://u:p@/db?host=/var/run/postgresql", True),
    ("host=/tmp user=u dbname=d", True), ("postgresql://u:p@db.corp/db", False), ("postgresql://u:p@db.corp/db?sslmode=prefer", False),
    ("postgresql://u:p@db.corp/db?sslmode=disable", False), ("postgresql://u:p@db.corp/db?sslmode=allow", False),
    ("postgresql://u:p@db.corp/db?sslmode=require", True), ("postgresql://u:p@db.corp/db?sslmode=verify-ca", True),
    ("postgresql://u:p@db.corp/db?sslmode=verify-full&sslrootcert=/ca.pem", True),
    ("postgresql://u:p@a.corp,b.corp/db?sslmode=require", True), ("postgresql://u:p@a.corp,127.0.0.1/db", False)])
def test_database_connections_must_demand_tls_unless_local(dsn, ok, monkeypatch):
    monkeypatch.delenv("TIA_ALLOW_INSECURE_DB", raising=False)
    if ok:
        assert check_db_tls(dsn) == dsn
    else:
        with pytest.raises(TransportError, match="does not require TLS"):
            check_db_tls(dsn)
        assert check_db_tls(dsn, allow=True) == dsn                                       # the explicit opt-out
        monkeypatch.setenv("TIA_ALLOW_INSECURE_DB", "true")
        assert check_db_tls(dsn) == dsn


def test_outbound_credentials_need_https_and_scm_config_enforces_it(monkeypatch):
    monkeypatch.delenv("TIA_ALLOW_INSECURE_OUTBOUND", raising=False)
    assert check_outbound("https://api.github.com", "x") and check_outbound("http://127.0.0.1:9", "x")
    with pytest.raises(TransportError, match="TIA_GITHUB_API_URL must use https"):
        check_outbound("http://ghe.corp/api/v3", "TIA_GITHUB_API_URL")
    monkeypatch.setenv("TIA_GITHUB_API_URL", "http://ghe.corp/api/v3")
    with pytest.raises(TransportError):
        ScmConfig.from_env()
    monkeypatch.setenv("TIA_ALLOW_INSECURE_OUTBOUND", "true")
    assert ScmConfig.from_env().github_api_url == "http://ghe.corp/api/v3"
    monkeypatch.setenv("TIA_GITHUB_API_URL", "https://ghe.corp/api/v3/")
    assert ScmConfig.from_env().github_api_url == "https://ghe.corp/api/v3"


def test_scm_secrets_come_from_references(monkeypatch, tmp_path, vault):
    f = tmp_path / "wh"
    f.write_text("hook-secret\n")
    vault.kv["secret/tia"] = {"gh": "ghp-vault-token"}
    monkeypatch.setenv("VAULT_ADDR", vault.addr)
    monkeypatch.setenv("VAULT_TOKEN", vault.token)
    monkeypatch.setenv("TIA_GITHUB_WEBHOOK_SECRET", f"file:{f}")
    monkeypatch.setenv("TIA_GITHUB_TOKEN", "vault:secret/tia#gh")
    monkeypatch.setenv("TIA_GITHUB_API_URL", "https://api.github.com")
    scm = ScmConfig.from_env()
    assert scm.github_webhook_secret == b"hook-secret" and scm.github_token == "ghp-vault-token"


def test_a_token_is_never_sent_over_plain_http(tmp_path):
    svc = RepoService(conftest.fresh_store(), str(tmp_path / "ws"), allow_local=True)
    for call in (lambda: svc.register(url="http://git.corp.example/o/r.git", token="tok"),
                 lambda: svc.discover("github", "org", None, "tok", "http://ghe.corp.example/api/v3"),
                 lambda: svc.discover("ado", "org", "proj", "tok", "http://ado.corp.example")):
        with pytest.raises(IngestError, match="refusing to send a token over plain http"):
            call()
    assert svc.list() == []                                                            # refused before any clone was attempted


def test_tls_options(tmp_path, monkeypatch, pki):
    for v in ("TIA_TLS_CERTFILE", "TIA_TLS_KEYFILE", "TIA_TLS_KEYFILE_PASSWORD", "TIA_TLS_CA_CERTS"):
        monkeypatch.delenv(v, raising=False)
    assert tls_options() == {}                                                         # TLS terminated upstream
    monkeypatch.setenv("TIA_TLS_CERTFILE", pki["cert"])
    with pytest.raises(TransportError, match="must be set together"):
        tls_options()
    monkeypatch.setenv("TIA_TLS_KEYFILE", str(tmp_path / "missing.key"))
    with pytest.raises(TransportError, match="does not exist"):
        tls_options()
    monkeypatch.setenv("TIA_TLS_KEYFILE", pki["key"])
    pw = tmp_path / "pw"
    pw.write_text("kp\n")
    monkeypatch.setenv("TIA_TLS_KEYFILE_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("TIA_TLS_CA_CERTS", pki["ca"])
    assert tls_options() == {"ssl_certfile": pki["cert"], "ssl_keyfile": pki["key"], "ssl_keyfile_password": "kp", "ssl_ca_certs": pki["ca"]}


def test_hsts_is_sent_only_over_https_and_only_when_enabled(tmp_path):
    secure = TestClient(make_app(tmp_path / "a", allow_local=True, hsts=True), base_url="https://testserver")
    assert secure.get("/api/v1/health").headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    plain = TestClient(make_app(tmp_path / "b", allow_local=True, hsts=True))
    assert "strict-transport-security" not in plain.get("/api/v1/health").headers                 # never over http
    assert "strict-transport-security" in plain.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers   # TLS proxy
    off = TestClient(make_app(tmp_path / "c", allow_local=True), base_url="https://testserver")
    assert "strict-transport-security" not in off.get("/api/v1/health").headers


def test_the_server_really_speaks_tls_and_refuses_plaintext(tmp_path, pki):
    app = make_app(tmp_path / "tls", allow_local=True, hsts=True)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, ssl_certfile=pki["cert"], ssl_keyfile=pki["key"], log_level="error")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        import ssl
        ctx = ssl.create_default_context(cafile=pki["ca"])
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/api/v1/health", context=ctx, timeout=10) as r:        # a real handshake
            assert r.status == 200 and r.headers["Strict-Transport-Security"].startswith("max-age=")
        with pytest.raises(urllib.error.URLError):                                                                   # untrusted CA is refused
            urllib.request.urlopen(f"https://127.0.0.1:{port}/api/v1/health", context=ssl.create_default_context(cafile=pki["other_ca"]), timeout=10)
        with pytest.raises(Exception):                                                                               # plaintext on the TLS port
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=5).read()
    finally:
        server.should_exit = True
        t.join(10)
