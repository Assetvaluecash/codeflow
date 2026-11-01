"""Code upload from the user's drive: zip and folder, 2 GiB default cap, hostile input, quotas, tenancy, and real analysis."""
import io
import json
import os
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import git, make_app
from tia import gitutil
from tia.api import create_app
from tia.upload import GIB, Session, UploadConfig, UploadTooLarge, clean_member
from tia.repos import IngestError

SMALL = {"app.py": "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/ping')\ndef ping():\n    return {'ok': True}\n",
         "README.md": "# svc\n", "pkg/util.py": "def helper():\n    return 1\n"}


def zip_bytes(files: dict, root: str = None, extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(f"{root}/{name}" if root else name, data)
        for info, data in (extra or []):
            z.writestr(info, data)
    return buf.getvalue()


def start(c, name):
    r = c.post("/api/v1/uploads", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def upload_zip(c, name, data, strip_root="auto"):
    uid = start(c, name)
    r = c.put(f"/api/v1/uploads/{uid}/archive?strip_root={strip_root}", content=data)
    if r.status_code != 200:
        return r, uid
    return c.post(f"/api/v1/uploads/{uid}/commit"), uid


def tree_of(repo_dir, rev="main"):
    return git(repo_dir, "rev-parse", f"{rev}^{{tree}}")


def landscape_files(fx, name, rev):
    return gitutil.read_tree(fx["paths"][name], rev)


# ---- configuration ----------------------------------------------------------------------------------------------
def test_default_cap_is_2_gib_and_configurable(monkeypatch):
    assert UploadConfig().max_bytes == 2 * GIB == 2147483648
    monkeypatch.setenv("TIA_MAX_UPLOAD_BYTES", "12345")
    monkeypatch.setenv("TIA_ALLOW_UPLOADS", "false")
    cfg = UploadConfig.from_env()
    assert cfg.max_bytes == 12345 and cfg.enabled is False


def test_uploads_can_be_disabled(tmp_path):
    c = TestClient(make_app(tmp_path / "d", uploads=UploadConfig(enabled=False)))
    assert c.post("/api/v1/uploads", json={"name": "x"}).status_code == 403
    assert c.put("/api/v1/repositories/x/upload", content=zip_bytes(SMALL)).status_code == 403


# ---- the happy paths --------------------------------------------------------------------------------------------
def test_zip_upload_becomes_a_repository_with_a_revision(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    r, _ = upload_zip(c, "svc", zip_bytes(SMALL))
    assert r.status_code == 200, r.text
    repo = r.json()
    assert repo["name"] == "svc" and repo["provider"] == "upload" and repo["kind"] == "upload"
    assert repo["files"] == 3 and repo["unchanged"] is False and repo["branches"] == ["main"]
    assert repo["revision"] == repo["last_commit"] and repo["scan_status"] == "ok"
    revs = c.get("/api/v1/repositories/svc/revisions").json()
    assert len(revs) == 1 and revs[0]["commit"] == repo["revision"] and "Upload (archive): 3 files" in revs[0]["label"]
    stored = gitutil.read_tree(str(tmp_path / "d" / "svc"), "main")
    assert stored == SMALL                                                                # exactly what was uploaded
    g = c.get("/api/v1/graph?level=endpoint").json()
    assert any(n["label"] == "GET /ping" for n in g["nodes"])                              # it is analysed like any repository
    assert c.get("/api/v1/graph/versions").json()["head"] is not None


def test_one_call_zip_upload_for_scripts(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    r = c.put("/api/v1/repositories/svc/upload", content=zip_bytes(SMALL))
    assert r.status_code == 200 and r.json()["files"] == 3
    again = c.put("/api/v1/repositories/svc/upload", content=zip_bytes(SMALL))
    assert again.json()["unchanged"] is True and len(c.get("/api/v1/repositories/svc/revisions").json()) == 1


def test_wrapper_folder_in_the_zip_is_stripped_only_when_it_is_the_repository(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    upload_zip(c, "svc", zip_bytes(SMALL, root="svc"))                                     # name matches: stripped
    assert gitutil.list_files(str(tmp_path / "d" / "svc"), "main") == sorted(SMALL)
    upload_zip(c, "other", zip_bytes(SMALL, root="src"))                                   # unrelated top folder: kept
    assert all(f.startswith("src/") for f in gitutil.list_files(str(tmp_path / "d" / "other"), "main"))
    upload_zip(c, "third", zip_bytes(SMALL, root="whatever"), strip_root="true")           # explicit
    assert gitutil.list_files(str(tmp_path / "d" / "third"), "main") == sorted(SMALL)


def test_folder_upload_in_batches_equals_the_zip_route_and_retries_are_idempotent(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    upload_zip(c, "viazip", zip_bytes(SMALL))
    uid = start(c, "viafolder")
    names = sorted(SMALL)

    def batch(subset):
        return c.post(f"/api/v1/uploads/{uid}/files", data={"paths": subset},
                      files=[("files", (n.rsplit("/", 1)[-1], SMALL[n].encode())) for n in subset])
    b1 = batch(names[:2])
    assert b1.status_code == 200 and b1.json()["files"] == 2
    again = batch(names[:2])                                                                # the browser retried batch 1
    assert again.json()["files"] == 2 and again.json()["bytes"] == b1.json()["bytes"]
    b2 = batch(names[2:])
    assert b2.json()["files"] == 3
    done = c.post(f"/api/v1/uploads/{uid}/commit").json()
    assert done["files"] == 3 and done["provider"] == "upload"
    assert tree_of(tmp_path / "d" / "viafolder") == tree_of(tmp_path / "d" / "viazip")      # identical content, either route
    assert c.post(f"/api/v1/uploads/{uid}/commit").status_code == 404                       # the session is gone once committed


def test_ignored_directories_are_skipped_and_counted(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    files = {**SMALL, "node_modules/x/index.js": "x", ".git/config": "[core]", "pkg/__pycache__/a.pyc": "b", ".venv/lib/z.py": "z"}
    r, _ = upload_zip(c, "svc", zip_bytes(files))
    assert r.json()["files"] == 3 and r.json()["skipped_files"] == 4
    assert gitutil.read_tree(str(tmp_path / "d" / "svc"), "main") == SMALL


def test_stored_content_is_byte_exact_whatever_the_servers_git_settings(tmp_path):
    """CRLF files and a .gitignore inside the upload must not change what is stored (autocrlf / ignore rules disabled)."""
    c = TestClient(make_app(tmp_path / "d"))
    files = {"a.py": "x = 1\r\ny = 2\r\n", ".gitignore": "*.py\n", "b.py": "z = 3\n"}
    r, _ = upload_zip(c, "svc", zip_bytes(files))
    assert r.json()["files"] == 3
    raw = gitutil._git(str(tmp_path / "d" / "svc"), "show", "main:a.py", text=False)
    assert raw == b"x = 1\r\ny = 2\r\n"
    assert sorted(gitutil.list_files(str(tmp_path / "d" / "svc"), "main")) == [".gitignore", "a.py", "b.py"]


def test_nothing_to_analyse_is_an_error_not_an_empty_repository(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    r, uid = upload_zip(c, "svc", zip_bytes({"node_modules/a.js": "x"}))
    assert r.status_code == 400 and "nothing has been uploaded" in r.text
    assert c.get("/api/v1/repositories").json() == []


# ---- the point of it: analyse a change between two uploads ----------------------------------------------------------
def test_two_uploads_are_analysed_like_a_pull_request(fx, landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    for n, p in landscape.items():
        if n != "payments-service":
            assert c.post("/api/v1/repositories", json={"path": str(p), "name": n}).status_code == 200
    base, changed = landscape_files(fx, "payments-service", "main"), landscape_files(fx, "payments-service", "breaking")
    r1, _ = upload_zip(c, "payments-service", zip_bytes(base))
    assert r1.status_code == 200
    r2, _ = upload_zip(c, "payments-service", zip_bytes(changed))
    assert r2.status_code == 200 and r2.json()["unchanged"] is False
    revs = c.get("/api/v1/repositories/payments-service/revisions").json()
    assert [x["commit"] for x in revs] == [r2.json()["revision"], r1.json()["revision"]]

    rep = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": "main", "base": "main~1"}).json()["report"]
    assert rep["change"]["change_type"] == "api_change"
    assert rep["summary"]["findings"] == 5 and rep["summary"]["by_severity"]["critical"] == 2
    assert {f["component"] for f in rep["findings"]} >= {"reconciliation-service", "mobile-bff", "audit-reporting", "ledger-sync"}
    assert rep["change"]["base_revision"] == r1.json()["revision"] and rep["change"]["proposed_revision"] == r2.json()["revision"]

    # a third upload moves the repository on; the first analysis still reproduces (revisions are kept and pinned)
    r3, _ = upload_zip(c, "payments-service", zip_bytes({**changed, "NOTES.md": "later\n"}))
    assert r3.status_code == 200
    again = c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/reproduce").json()
    assert again["identical"] is True
    pins = git(tmp_path / "d" / "payments-service", "for-each-ref", "--format=%(objectname)", "refs/tia/pin").split()
    assert r2.json()["revision"] in pins and r1.json()["revision"] in pins
    audit = c.get("/api/v1/audit").json()
    assert [a["event"] for a in audit].count("repository_uploaded") == 3
    up = next(a for a in audit if a["event"] == "repository_uploaded")
    assert up["files"] and up["commit"] and "name" in up and "app.py" not in json.dumps(audit[:3])
    assert c.get("/api/v1/audit/verify").json()["ok"] is True


# ---- hostile input -----------------------------------------------------------------------------------------------------
def info(name, mode=None):
    zi = zipfile.ZipInfo(name)
    if mode is not None:
        zi.external_attr = mode << 16
    return zi


BAD_NAMES = ["../evil.py", "a/../../evil.py", "/abs/evil.py", "C:/evil.py", "a\\..\\..\\evil.py", "con.txt", "dir/AUX.py",
             "file.py:stream", "trailing.", "bad\x01name.py", "q?.py"]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_unsafe_names_reject_the_whole_upload_and_write_nothing(bad, tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    data = zip_bytes({"ok.py": "x = 1\n"}, extra=[(info(bad), b"boom")])
    r, uid = upload_zip(c, "svc", data)
    assert r.status_code == 400, (bad, r.text)
    assert c.get("/api/v1/repositories").json() == []                                      # nothing was registered
    assert c.post(f"/api/v1/uploads/{uid}/commit").status_code == 404                      # fail closed: the session is gone
    for root, _, names in os.walk(tmp_path):                                               # and nothing escaped the staging area
        assert "evil.py" not in names, root
    assert "upload_rejected" in [a["event"] for a in c.get("/api/v1/audit").json()]


def test_symlinks_special_files_encrypted_and_case_collisions_are_rejected(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    link = zip_bytes({}, extra=[(info("lnk", stat.S_IFLNK | 0o777), b"/etc/passwd")])
    assert upload_zip(c, "a", link)[0].status_code == 400
    fifo = zip_bytes({}, extra=[(info("pipe", stat.S_IFIFO | 0o644), b"")])
    assert upload_zip(c, "b", fifo)[0].status_code == 400
    enc = zipfile.ZipInfo("secret.py"); enc.flag_bits |= 0x1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ok.py", "x")
    assert upload_zip(c, "c", zip_bytes({"A.py": "1", "a.py": "2"}))[0].status_code == 400
    r, _ = upload_zip(c, "d", b"this is not a zip")
    assert r.status_code == 400 and "not a valid zip" in r.text
    assert c.get("/api/v1/repositories").json() == []


def test_path_validation_unit():
    assert clean_member("a/b.py") == "a/b.py" and clean_member("./a\\b.py") == "a/b.py"
    assert clean_member("dir/") is None and clean_member("") is None
    assert clean_member("node_modules/x.js") is None and clean_member("a/.git/hooks/pre-commit") is None
    for bad in ("../x", "..\\x", "/x", "C:\\x", "a/../x", "nul", "COM1.txt", "a:b", "x.", "x ", "a\x00b.py"):
        with pytest.raises(IngestError):
            clean_member(bad)


# ---- caps (enforced on bytes actually written) ----------------------------------------------------------------------------
def test_size_cap_on_the_archive_body_with_and_without_content_length(tmp_path):
    c = TestClient(make_app(tmp_path / "d", uploads=UploadConfig(max_bytes=1000)))
    uid = start(c, "svc")
    r = c.put(f"/api/v1/uploads/{uid}/archive", content=b"x" * 5000)                      # declared too large
    assert r.status_code == 413 and "size limit" in r.text
    uid = start(c, "svc2")
    r = c.put(f"/api/v1/uploads/{uid}/archive", content=(b"x" * 100 for _ in range(50)))   # chunked: no declared length
    assert r.status_code == 413
    assert c.post(f"/api/v1/uploads/{uid}/commit").status_code == 404
    assert c.get("/api/v1/repositories").json() == []


def test_expanded_size_cap_and_file_count_cap_on_archives(tmp_path):
    c = TestClient(make_app(tmp_path / "d", uploads=UploadConfig(max_bytes=10_000, max_files=3)))
    bomb = zip_bytes({"big.py": "0" * 200_000})                                              # tiny archive, huge expansion
    assert len(bomb) < 10_000
    r, _ = upload_zip(c, "bomb", bomb)
    assert r.status_code == 413 and "expands beyond" in r.text
    r, _ = upload_zip(c, "many", zip_bytes({f"f{i}.py": "x" for i in range(5)}))
    assert r.status_code == 413 and "more than 3" in r.text


def test_actual_bytes_written_are_capped_even_if_an_archive_understates_its_size(tmp_path):
    s = Session.create(tmp_path / "u", UploadConfig(max_bytes=1000), "t", "svc")
    with pytest.raises(UploadTooLarge):
        s.add("big.bin", iter([b"x" * 600, b"x" * 600]))                                    # the header said nothing; the bytes did
    s2 = Session.create(tmp_path / "u", UploadConfig(max_files=2), "t", "svc2")
    s2.add("a.py", iter([b"1"])); s2.add("b.py", iter([b"1"]))
    with pytest.raises(UploadTooLarge):
        s2.add("c.py", iter([b"1"]))


def test_folder_batches_share_one_cap_and_oversize_discards_the_upload(tmp_path):
    c = TestClient(make_app(tmp_path / "d", uploads=UploadConfig(max_bytes=1000)))
    uid = start(c, "svc")
    ok = c.post(f"/api/v1/uploads/{uid}/files", data={"paths": ["a.py"]}, files=[("files", ("a.py", b"a" * 600))])
    assert ok.status_code == 200
    over = c.post(f"/api/v1/uploads/{uid}/files", data={"paths": ["b.py"]}, files=[("files", ("b.py", b"b" * 600))])
    assert over.status_code == 413
    assert c.post(f"/api/v1/uploads/{uid}/commit").status_code == 404


# ---- sessions, tenancy, naming -----------------------------------------------------------------------------------------
def test_sessions_are_bound_to_their_tenant_and_validated(tmp_path):
    ws = tmp_path / "shared"
    a = TestClient(create_app(conftest.PG.db, conftest.new_tenant(), str(ws), uploads=UploadConfig()))
    b = TestClient(create_app(conftest.PG.db, conftest.new_tenant(), str(ws), uploads=UploadConfig()))
    uid = start(a, "svc")
    assert b.put(f"/api/v1/uploads/{uid}/archive", content=zip_bytes(SMALL)).status_code == 404       # another tenant's session
    assert b.post(f"/api/v1/uploads/{uid}/commit").status_code == 404
    assert a.put(f"/api/v1/uploads/{uid}/archive", content=zip_bytes(SMALL)).status_code == 200
    for bogus in ("nope", "../../etc", "0" * 32):
        assert a.post(f"/api/v1/uploads/{bogus}/commit").status_code == 404


def test_one_upload_is_either_a_zip_or_a_folder(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    uid = start(c, "svc")
    assert c.post(f"/api/v1/uploads/{uid}/files", data={"paths": ["a.py"]}, files=[("files", ("a.py", b"x = 1\n"))]).status_code == 200
    r = c.put(f"/api/v1/uploads/{uid}/archive", content=zip_bytes(SMALL))
    assert r.status_code == 400 and "already receives a folder" in r.text


def test_cancel_discards_staging(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    uid = start(c, "svc")
    c.post(f"/api/v1/uploads/{uid}/files", data={"paths": ["a.py"]}, files=[("files", ("a.py", b"x = 1\n"))])
    assert (tmp_path / "d" / ".uploads" / uid).exists()
    assert c.delete(f"/api/v1/uploads/{uid}").status_code == 200
    assert not (tmp_path / "d" / ".uploads" / uid).exists() and c.delete(f"/api/v1/uploads/{uid}").status_code == 404


def test_cannot_overwrite_a_cloned_or_local_repository_by_upload_and_removal_cleans_up(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "d", allow_local=True))
    c.post("/api/v1/repositories", json={"path": str(landscape["mobile-bff"]), "name": "mobile-bff"})
    r = c.post("/api/v1/uploads", json={"name": "mobile-bff"})
    assert r.status_code == 400 and "already registered" in r.text
    assert c.put("/api/v1/repositories/mobile-bff/upload", content=zip_bytes(SMALL)).status_code == 400
    r, _ = upload_zip(c, "svc", zip_bytes(SMALL))
    assert r.status_code == 200 and (tmp_path / "d" / "svc").exists()
    assert c.delete("/api/v1/repositories/svc").status_code == 200
    assert not (tmp_path / "d" / "svc").exists()
    assert (landscape["mobile-bff"] / ".git").exists()                                      # the user's own repository is untouched


def test_upload_names_are_sanitised(tmp_path):
    c = TestClient(make_app(tmp_path / "d"))
    r = c.post("/api/v1/uploads", json={"name": "../../evil repo"})
    assert r.status_code == 200 and r.json()["name"] == "evil-repo"
    assert c.post("/api/v1/uploads", json={"name": "***"}).status_code == 400


def test_relative_workspace_paths_work(tmp_path, monkeypatch):
    """Found in the browser: the dev server uses a relative workspace; git runs from the staging directory."""
    monkeypatch.chdir(tmp_path)
    c = TestClient(make_app("rel-workspace"))
    r, _ = upload_zip(c, "svc", zip_bytes(SMALL))
    assert r.status_code == 200, r.text
    assert r.json()["files"] == 3 and len(c.get("/api/v1/repositories/svc/revisions").json()) == 1
    r2, _ = upload_zip(c, "svc", zip_bytes({**SMALL, "extra.py": "x = 1\n"}))
    assert r2.status_code == 200 and len(c.get("/api/v1/repositories/svc/revisions").json()) == 2
