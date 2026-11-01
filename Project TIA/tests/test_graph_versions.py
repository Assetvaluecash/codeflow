"""UC1-11: the dependency graph is persisted as immutable versions; the head advances incrementally; analyses use it."""
import dataclasses
import subprocess

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import fresh_store, make_app
from tia import graphstore
from tia.graph import build_graph
from tia.impact import AnalysisRequest, _extract, analyze


from conftest import git


def extracts_of(fx, store, repos=None):
    return {n: _extract(n, p, "main", store, ()) for n, p in fx["paths"].items() if not repos or n in repos}


def same_graph(a, b):
    return (a.nodes == b.nodes and [dataclasses.asdict(e) for e in a.edges] == [dataclasses.asdict(e) for e in b.edges]
            and a.unresolved == b.unresolved and a.resolution_stats == b.resolution_stats
            and a.ambiguous_calls == b.ambiguous_calls)


def test_graph_round_trips_through_the_database_unchanged(fx):
    store = fresh_store()
    ex = extracts_of(fx, store)
    g1, gid1, built1 = graphstore.get_or_build(store, ex)
    g2, gid2, built2 = graphstore.get_or_build(store, ex)
    assert built1 is True and built2 is False and gid1 == gid2 and gid1.startswith("g-")
    assert same_graph(g1, g2) and same_graph(g2, build_graph(ex))                      # loaded == freshly built
    for nid in list(g1.nodes)[:50]:                                                     # traversal index rebuilt identically
        assert [(d, e.rel) for d, e in g1.impacted_by(nid)] == [(d, e.rel) for d, e in g2.impacted_by(nid)]


def test_version_identity_follows_the_inputs(fx):
    store = fresh_store()
    ex = extracts_of(fx, store)
    _, a, _ = graphstore.get_or_build(store, ex)
    _, b, _ = graphstore.get_or_build(store, {k: v for k, v in ex.items() if k != "notifications"})
    _, c, _ = graphstore.get_or_build(store, ex, strip=("/v1",))
    assert len({a, b, c}) == 3                                                          # repos and config are part of the identity
    v = {x["id"]: x for x in store.list_graphs()}
    assert v[a]["inputs"]["repos"]["payments-service"] == ex["payments-service"].commit
    assert v[a]["stats"]["nodes"] > 0 and v[a]["stats"]["cross_repo_relationships"] >= 3


def test_graph_versions_are_tenant_isolated(fx):
    s1, s2 = fresh_store(), fresh_store()
    _, gid, _ = graphstore.get_or_build(s1, extracts_of(fx, s1))
    assert s1.get_graph(gid) is not None and s2.get_graph(gid) is None and s2.list_graphs() == []
    s1.set_head("landscape", gid)
    assert s2.get_head("landscape") is None


def test_head_moves_only_when_the_graph_changes_and_remembers_the_previous(fx):
    s = fresh_store()
    ex = extracts_of(fx, s)
    _, g1, _ = graphstore.get_or_build(s, ex)
    assert s.set_head("landscape", g1) is True and s.set_head("landscape", g1) is False
    _, g2, _ = graphstore.get_or_build(s, {k: v for k, v in ex.items() if k != "audit-reporting"})
    assert s.set_head("landscape", g2) is True
    h = s.get_head("landscape")
    assert h["graph_id"] == g2 and h["previous_id"] == g1


def test_analysis_uses_the_persisted_graph_and_does_not_rebuild(fx, monkeypatch):
    store = fresh_store()
    req = lambda: AnalysisRequest(repos=fx["paths"], target="payments-service", base=fx["shas"]["base"], head=fx["shas"]["breaking"])
    first = analyze(req(), store)
    assert len(store.list_graphs()) == 1
    calls = []
    real = graphstore.build_graph
    monkeypatch.setattr(graphstore, "build_graph", lambda *a, **k: calls.append(1) or real(*a, **k))
    second = analyze(req(), store)
    assert calls == []                                                                  # loaded, not rebuilt
    for r in (first, second):
        r.pop("generated_at")
    assert first == second and len(first["findings"]) == 5


# ---- through the API: the head follows the repositories ----------------------------------------------------------
def versions(c):
    return c.get("/api/v1/graph/versions").json()


def test_head_advances_incrementally_with_repository_changes(landscape, tmp_path):
    c = TestClient(make_app(tmp_path / "data", allow_local=True))
    for n, p in landscape.items():
        assert c.post("/api/v1/repositories", json={"path": str(p), "name": n}).status_code == 200
    v1 = versions(c)
    assert len(v1["versions"]) >= 1 and v1["head"]["graph_id"] in [x["id"] for x in v1["versions"]]
    assert [x["current"] for x in v1["versions"]].count(True) == 1
    first_head = v1["head"]["graph_id"]
    assert c.get("/api/v1/graph").status_code == 200 and versions(c)["head"]["graph_id"] == first_head   # reading changes nothing

    # a one-line change in one repository, then sync: exactly one new version, the old one is still there
    app_py = landscape["payments-service"] / "app.py"
    app_py.write_text(app_py.read_text().replace('"SETTLED"', '"COMPLETED"'))
    git(landscape["payments-service"], "commit", "-qam", "implementation tweak")
    assert c.post("/api/v1/repositories/payments-service/sync", json={}).status_code == 200
    v2 = versions(c)
    assert v2["head"]["graph_id"] != first_head and v2["head"]["previous_id"] == first_head
    assert first_head in [x["id"] for x in v2["versions"]]                               # history is kept
    old = c.get(f"/api/v1/graph/versions/{first_head}").json()
    new = c.get(f"/api/v1/graph/versions/{v2['head']['graph_id']}").json()
    assert old["inputs"]["repos"]["payments-service"] != new["inputs"]["repos"]["payments-service"]
    assert old["inputs"]["repos"]["mobile-bff"] == new["inputs"]["repos"]["mobile-bff"]  # untouched repos keep their commit

    # a no-op sync does not create another version
    n_before = len(versions(c)["versions"])
    c.post("/api/v1/repositories/payments-service/sync", json={})
    assert len(versions(c)["versions"]) == n_before

    # removing a repository is a graph change too
    assert c.delete("/api/v1/repositories/notifications").status_code == 200
    v3 = versions(c)
    assert v3["head"]["graph_id"] not in (first_head, v2["head"]["graph_id"])
    assert "notifications" not in c.get(f"/api/v1/graph/versions/{v3['head']['graph_id']}").json()["inputs"]["repos"]
    events = [a["event"] for a in c.get("/api/v1/audit").json()]
    assert events.count("graph_version_advanced") >= 3
    assert c.get("/api/v1/graph/versions/g-nope").status_code == 404
    assert c.get("/api/v1/audit/verify").json()["ok"] is True


def count_cached_files(tenant) -> int:
    import psycopg
    with psycopg.connect(conftest.PG.admin) as conn:
        return conn.execute("select count(*) from file_extracts where tenant_id = %s", (tenant,)).fetchone()[0]


def test_one_file_push_reparses_exactly_one_file_through_the_whole_stack(landscape, tmp_path):
    """AC-021 end to end: registering parses every file once; a one-file push adds exactly one cached file fact."""
    from tia.api import create_app
    tenant = conftest.new_tenant()
    c = TestClient(create_app(conftest.PG.db, tenant, str(tmp_path / "data"), allow_local=True))
    for n, p in landscape.items():
        assert c.post("/api/v1/repositories", json={"path": str(p), "name": n}).status_code == 200
    all_files = count_cached_files(tenant)
    assert all_files >= 6
    c.post("/api/v1/repositories/payments-service/sync", json={})
    assert count_cached_files(tenant) == all_files                                       # nothing changed, nothing re-parsed

    target = landscape["payments-service"] / "app.py"
    target.write_text(target.read_text() + "\n# one-line change\n")
    git(landscape["payments-service"], "commit", "-qam", "touch one file")
    c.post("/api/v1/repositories/payments-service/sync", json={})
    assert count_cached_files(tenant) == all_files + 1                                   # exactly the changed file


# ---- full-rescan path ---------------------------------------------------------------------------------------------
def test_full_rescan_agrees_with_the_incremental_result_on_a_healthy_system(landscape, tmp_path):
    from tia.api import create_app
    tenant = conftest.new_tenant()
    c = TestClient(create_app(conftest.PG.db, tenant, str(tmp_path / "data"), allow_local=True))
    for n, p in landscape.items():
        c.post("/api/v1/repositories", json={"path": str(p), "name": n})
    head = versions(c)["head"]["graph_id"]
    r = c.post("/api/v1/graph/rescan").json()
    assert all(x["drift"] is False and x["parsed"] == x["files"] for x in r["repositories"])   # everything re-parsed, nothing differs
    assert r["graph_drift"] is False and r["graph_id"] == head and r["head_moved"] is False
    assert "graph_rescanned" in [a["event"] for a in c.get("/api/v1/audit").json()]


def test_full_rescan_detects_and_repairs_a_corrupted_cache(landscape, tmp_path):
    import psycopg
    from tia.api import create_app
    from tia.store import Store
    tenant = conftest.new_tenant()
    c = TestClient(create_app(conftest.PG.db, tenant, str(tmp_path / "data"), allow_local=True))
    for n, p in landscape.items():
        c.post("/api/v1/repositories", json={"path": str(p), "name": n})
    gid = versions(c)["head"]["graph_id"]
    store = Store(conftest.PG.db, tenant)
    good_graph = store.get_graph(gid)

    with psycopg.connect(conftest.PG.admin, autocommit=True) as conn:            # simulate a faulty extractor run / bit rot
        conn.execute("update extractions set data = jsonb_set(data, '{endpoints}', '[]') "
                     "where tenant_id = %s and key like 'payments-service@%%'", (tenant,))
        conn.execute("update graph_versions set data = jsonb_set(data, '{edges}', '[]') where tenant_id = %s", (tenant,))

    assert store.get_graph(gid)["edges"] == []                                    # the persisted graph is now wrong
    r = c.post("/api/v1/graph/rescan").json()
    drifted = [x["repo"] for x in r["repositories"] if x["drift"]]
    assert drifted == ["payments-service"] and r["graph_drift"] is True
    assert store.get_graph(gid) == good_graph                                      # repaired to exactly the healthy graph
    again = c.post("/api/v1/graph/rescan").json()
    assert not any(x["drift"] for x in again["repositories"]) and again["graph_drift"] is False
    assert any(a["event"] == "graph_rescanned" and a["graph_drift"] for a in c.get("/api/v1/audit").json())
