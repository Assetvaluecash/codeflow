"""The in-process cache of extracts and graph versions (store.ROW_CACHE): faster, and never wrong - a rewritten row is not served stale,
a reader that changes what it got cannot affect the next reader, and one tenant never reads another's entry."""
import conftest
from tia import store as store_mod
from tia.models import Endpoint, RepoExtract
from tia.store import Store


def extract(name="svc", path="/a"):
    ex = RepoExtract(repo="r", service="svc", commit="c" * 40, extractor_version="v-test")
    ex.endpoints.append(Endpoint(id=f"svc:GET {path}", service="svc", repo="r", method="GET", path=path, norm_path=path,
                                 handler="h", handler_id="r:h", file="app.py", line=1))
    return ex


def test_hits_are_independent_copies():
    st = conftest.fresh_store()
    st.put_extract(extract())
    a = st.get_extract("r", "c" * 40, "v-test")
    a.endpoints.clear()                                                      # a reader changes what it got
    b = st.get_extract("r", "c" * 40, "v-test")
    assert [e.path for e in b.endpoints] == ["/a"] and b is not a


def test_a_rewritten_row_is_never_served_stale():
    st = conftest.fresh_store()
    st.put_extract(extract(path="/a"))
    assert st.get_extract("r", "c" * 40, "v-test").endpoints[0].path == "/a"
    other_process = Store(conftest.PG.db, st.tenant_id)                       # another writer (a full rescan in another process)
    other_process.put_extract(extract(path="/b"))
    assert st.get_extract("r", "c" * 40, "v-test").endpoints[0].path == "/b"
    st.put_graph("g-test", {}, {}, {"nodes": {"n": {"type": "x"}}, "edges": [], "unresolved": [], "resolution_stats": {}, "ambiguous_calls": []})
    g1 = st.get_graph("g-test")
    g1["nodes"]["added"] = {"type": "runtime_ref"}                           # an analysis adds runtime nodes to its own copy
    assert "added" not in st.get_graph("g-test")["nodes"]
    st.put_graph("g-test", {}, {}, {"nodes": {}, "edges": [], "unresolved": [], "resolution_stats": {}, "ambiguous_calls": []}, overwrite=True)
    assert st.get_graph("g-test")["nodes"] == {}


def test_another_tenant_never_gets_a_hit():
    a, b = conftest.fresh_store(), conftest.fresh_store()
    a.put_extract(extract())
    assert a.get_extract("r", "c" * 40, "v-test") is not None
    assert b.get_extract("r", "c" * 40, "v-test") is None                     # same key, other tenant: row-level security, and no cache entry


def test_the_cache_is_bounded_and_can_be_switched_off():
    c = store_mod._RowCache(10_000)
    for i in range(100):
        c.put(("t", "x", str(i)), "1", {"i": i, "pad": "x" * 200})
    assert c.used <= 10_000 and c.get(("t", "x", "99"), "1") == {"i": 99, "pad": "x" * 200} and c.get(("t", "x", "0"), "1") is None
    assert c.get(("t", "x", "99"), "2") is None                              # another version of the row: a miss
    off = store_mod._RowCache(0)
    off.put(("t", "x", "1"), "1", {"a": 1})
    assert off.get(("t", "x", "1"), "1") is None
