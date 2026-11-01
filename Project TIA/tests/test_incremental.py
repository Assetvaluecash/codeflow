"""UC1-11 / AC-021: a push that changes one file re-extracts only that file, and the result equals a full rescan."""
import dataclasses
import subprocess

import pytest

from conftest import fresh_store
from tia import gitutil
from tia.extract_python import extract_repo
from tia.extraction import extract_commit
from tia.graph import build_graph

A = '''from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    id: str
    status: str


@app.get("/items/{item_id}", response_model=Item)
def get_item(item_id: str):
    return helper(item_id)


def helper(x):
    return {"id": x, "status": "ok"}
'''
B = '''import os

import requests

BASE = os.getenv("UPSTREAM_URL")


def fetch(i):
    return requests.get(BASE + "/other/" + i).json()
'''
C = "def util(x):\n    return x + 1\n"
D = "def other(x):\n    return x * 2\n"
T = '''from a import get_item


def test_get_item():
    assert get_item("1")
'''


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    p = tmp_path / "svc"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    (p / "tests").mkdir()
    for name, text in {"a.py": A, "b.py": B, "c.py": C, "d.py": D, "tests/test_a.py": T, "README.md": "# x\n"}.items():
        (p / name).write_text(text)
    return p


def graph_dict(ex):
    g = build_graph({ex.repo: ex})
    return g.nodes, [dataclasses.asdict(e) for e in g.edges], g.unresolved, g.resolution_stats


def full(repo, commit_sha):
    return extract_repo("svc", commit_sha, gitutil.read_tree(str(repo), commit_sha))


def assert_equals_full_rescan(repo, ex, sha):
    f = full(repo, sha)
    assert ex.to_dict() == f.to_dict()
    assert graph_dict(ex) == graph_dict(f)             # the resulting graph equals a full rescan too


def test_first_extraction_parses_everything_then_reuses_everything(repo):
    store = fresh_store()
    c1 = commit(repo, "one")
    ex, st = extract_commit("svc", str(repo), c1, store)
    assert st == {"files": 5, "reused": 0, "parsed": 5}
    assert_equals_full_rescan(repo, ex, c1)
    ex2, st2 = extract_commit("svc", str(repo), c1, store)                           # nothing changed
    assert st2["parsed"] == 0 and st2["reused"] == 5 and ex2.to_dict() == ex.to_dict()


def test_one_file_change_reextracts_only_that_file(repo):
    store = fresh_store()
    c1 = commit(repo, "one")
    extract_commit("svc", str(repo), c1, store)
    (repo / "c.py").write_text(C + "\n\ndef util2(x):\n    return util(x)\n")
    c2 = commit(repo, "two")
    ex, st = extract_commit("svc", str(repo), c2, store)
    assert st == {"files": 5, "reused": 4, "parsed": 1}
    assert_equals_full_rescan(repo, ex, c2)
    assert any(f.qualname == "util2" for f in ex.functions)
    # a docs-only change re-extracts nothing
    (repo / "README.md").write_text("# changed\n")
    c3 = commit(repo, "docs")
    ex3, st3 = extract_commit("svc", str(repo), c3, store)
    assert st3["parsed"] == 0 and ex3.commit == c3
    assert_equals_full_rescan(repo, ex3, c3)


def test_add_delete_rename_and_syntax_error_equal_full_rescan(repo):
    store = fresh_store()
    c1 = commit(repo, "one")
    extract_commit("svc", str(repo), c1, store)
    (repo / "d.py").unlink()                                              # delete
    (repo / "e.py").write_text("def new():\n    return 1\n")              # add
    git(repo, "mv", "c.py", "util_mod.py")                                # rename: same blob, new path -> new facts
    (repo / "broken.py").write_text("def oops(:\n    pass\n")             # syntax error
    c2 = commit(repo, "many")
    ex, st = extract_commit("svc", str(repo), c2, store)
    assert st["parsed"] == 3 and st["reused"] == 3                         # e.py, util_mod.py, broken.py parsed
    assert [f["file"] for f in ex.files_failed] == ["broken.py"]
    assert_equals_full_rescan(repo, ex, c2)


def test_endpoint_change_propagates_through_incremental_extraction(repo):
    store = fresh_store()
    c1 = commit(repo, "one")
    extract_commit("svc", str(repo), c1, store)
    (repo / "a.py").write_text(A.replace("    status: str\n", "    state: str\n"))
    c2 = commit(repo, "rename field")
    ex, st = extract_commit("svc", str(repo), c2, store)
    assert st["parsed"] == 1
    [item] = [s for s in ex.schemas if s.name == "Item"]
    assert set(item.fields) == {"id", "state"}
    assert_equals_full_rescan(repo, ex, c2)


def test_service_map_change_invalidates_dependent_facts(repo):
    """The service identity is part of every fact (ids/ownership), so changing it must not reuse stale facts."""
    store = fresh_store()
    c1 = commit(repo, "one")
    extract_commit("svc", str(repo), c1, store)
    (repo / "tia-services.yaml").write_text("service: renamed-service\n")
    c2 = commit(repo, "service map")
    ex, st = extract_commit("svc", str(repo), c2, store)
    assert st["parsed"] == 5 and ex.service == "renamed-service"
    assert all(e.service == "renamed-service" for e in ex.endpoints)
    assert_equals_full_rescan(repo, ex, c2)


def test_cache_is_tenant_isolated_and_optional(repo):
    c1 = commit(repo, "one")
    a, b = fresh_store(), fresh_store()
    extract_commit("svc", str(repo), c1, a)
    assert extract_commit("svc", str(repo), c1, a)[1]["parsed"] == 0
    assert extract_commit("svc", str(repo), c1, b)[1]["parsed"] == 5       # another tenant sees none of it
    ex, st = extract_commit("svc", str(repo), c1, None)                    # no store: full parse, same result
    assert st["parsed"] == 5 and ex.to_dict() == extract_commit("svc", str(repo), c1, a)[0].to_dict()


def test_tree_listing_and_blob_reading_handle_awkward_files(repo):
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "módulo.py").write_text("x = 1\n")
    (repo / "logo.png").write_bytes(bytes(range(256)))
    (repo / "latin1.py").write_bytes("caf\xe9 = 1\n".encode("latin-1"))
    c = commit(repo, "awkward")
    tree = gitutil.list_tree(str(repo), c)
    assert "dir with space/módulo.py" in tree and "logo.png" in tree
    blobs = gitutil.read_blobs(str(repo), [tree["logo.png"], tree["a.py"]])
    assert blobs[tree["logo.png"]] == bytes(range(256)) and blobs[tree["a.py"]].decode() == A
    assert gitutil.read_blobs(str(repo), []) == {}
    with pytest.raises(gitutil.GitError):
        gitutil.read_blobs(str(repo), ["0" * 40])
    ex, _ = extract_commit("svc", str(repo), c, fresh_store())
    assert {"file": "latin1.py", "error": "not valid UTF-8"} in ex.files_failed      # reported, not silently dropped
    assert ex.non_python_files == 0                                                   # a png is not a 'source file in an unsupported language'
