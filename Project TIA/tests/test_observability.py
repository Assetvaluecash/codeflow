"""UC1-22: platform observability - metrics, structured logs, traces, health and readiness - against real PostgreSQL and real git.
Platform health (is the service working) is kept apart from analysis completeness (how complete is what it produced)."""
import io
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import make_app
from fixtures.build_fixture import build
from test_webhooks import TOKEN, FakeGitHub, env, gh, origins, set_pr  # noqa: F401  (fixtures)
from tia import health, metrics, obs, tracing
from tia.analysis_service import make_handlers
from tia.credentials import SecretError
from tia.jobs import JobQueue
from tia.metrics import REGISTRY, Registry
from tia.tenants import Tenants
from tia.tracing import OtlpExporter, Tracer, parse_traceparent
from tia.transport import TransportError
from tia.worker import Worker
from tlsutil import FakeCollector


# ---- helpers ------------------------------------------------------------------------------------------------------------------------
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (-?[0-9.eE+]+|[+-]Inf|NaN)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prom(text):
    """Parse Prometheus text and validate its structure. -> {(name, frozenset(labels.items())): value}."""
    out, seen_type, helped = {}, {}, set()
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# HELP "):
            name = line.split(" ", 3)[2]
            assert name not in helped, f"duplicate HELP for {name}"
            helped.add(name)
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            assert name not in seen_type, f"duplicate TYPE for {name}"
            assert kind in ("counter", "gauge", "histogram"), line
            seen_type[name] = kind
        else:
            m = SAMPLE.match(line)
            assert m, f"not a valid sample line: {line!r}"
            name, labels = m.group(1), dict((k, v) for k, v in LABEL.findall(m.group(3) or ""))
            base = re.sub(r"_(bucket|sum|count)$", "", name)
            assert name in seen_type or base in seen_type, f"sample before TYPE: {line!r}"
            out[(name, frozenset(labels.items()))] = float(m.group(4).replace("+Inf", "inf"))
    return out


def val(name, **labels):
    return REGISTRY.value(name, **labels) or 0


def samples_of(parsed, name):
    return {dict(k[1]).get("status") or dict(k[1]).get("severity") or dict(k[1]).get("area") or dict(k[1]).get("outcome") or "": v
            for k, v in parsed.items() if k[0] == name}


def scrape(client, **headers):
    r = client.get("/metrics", headers=headers)
    assert r.status_code == 200, r.text
    return parse_prom(r.text), r.text


@pytest.fixture(autouse=True)
def open_metrics(monkeypatch):
    monkeypatch.setenv("TIA_METRICS_ALLOW_ANY", "true")
    monkeypatch.setenv("TIA_METRICS_CACHE_TTL", "0")                       # scrape-time gauges are cached for 5s in production
    monkeypatch.delenv("TIA_METRICS_TOKEN", raising=False)
    monkeypatch.delenv("TIA_METRICS_TOKEN_FILE", raising=False)


@pytest.fixture
def tracer():
    old = tracing.TRACER
    t = tracing.configure(None, 1.0)
    yield t
    tracing.TRACER = old


@pytest.fixture
def logbuf():
    buf = io.StringIO()
    obs.setup_logging("DEBUG", "json", buf)
    yield buf
    for n in ("tia", "uvicorn", "uvicorn.error"):
        lg = logging.getLogger(n)
        lg.handlers = [h for h in lg.handlers if not getattr(h, "_tia", False)]
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = False


def logs(buf):
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


@pytest.fixture(scope="module")
def fxm(tmp_path_factory):
    return build(str(tmp_path_factory.mktemp("obs-landscape")))


@pytest.fixture
def app(fxm, tmp_path):
    tenant = conftest.new_tenant()
    c = TestClient(make_app(tmp_path / "ws", tenant=tenant, allow_local=True, gate_ttl=0))
    c.tenant, c.fx, c.ws = tenant, fxm, tmp_path / "ws"
    for n, p in fxm["paths"].items():
        assert c.post("/api/v1/repositories", json={"path": p, "name": n}).status_code == 200
    return c


def analyse(c, head="breaking", **kw):
    r = c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": c.fx["shas"].get(head, head), "base": "main", **kw})
    assert r.status_code == 200, r.text
    return r.json()["report"]


# =====================================================================================================================
# the metrics registry
# =====================================================================================================================
def test_counter_gauge_histogram_semantics_and_misuse():
    r = Registry()
    c, g, h = r.counter("t_c_total", "c", ("k",)), r.gauge("t_g", "g"), r.histogram("t_h_seconds", "h", ("k",), buckets=(1, 5))
    c.labels(k="a").inc(); c.labels("a").inc(2); c.labels(k="b").inc()
    g.set(5); g.inc(); g.dec(3)
    for v in (0.5, 1, 3, 9):
        h.labels(k="x").observe(v)
    assert r.value("t_c_total", k="a") == 3 and r.value("t_c_total", k="b") == 1 and r.value("t_g") == 3 and r.value("t_h_seconds", k="x") == 4
    with pytest.raises(ValueError, match="cannot decrease"):
        c.labels(k="a").inc(-1)
    with pytest.raises(ValueError, match="takes labels"):
        c.labels(wrong="a")
    with pytest.raises(ValueError, match="takes labels"):
        c.labels("a", "b")
    with pytest.raises(ValueError, match="only a gauge"):
        c.labels(k="a").set(1)
    with pytest.raises(ValueError, match="only a histogram"):
        g.observe(1)
    with pytest.raises(ValueError, match="registered differently"):
        r.gauge("t_c_total", "x", ("k",))
    assert r.counter("t_c_total", "c", ("k",)) is c                                         # same definition: the same metric


def test_exposition_is_valid_prometheus_text_with_cumulative_buckets_and_escaping():
    r = Registry()
    h = r.histogram("t_seconds", "Latency.\nSecond line \\ backslash", ("route",), buckets=(0.1, 1, 10))
    for v in (0.05, 0.5, 0.5, 5, 50):
        h.labels(route='/a "quoted"\\path\nx').observe(v)
    r.gauge("t_g", "g", ("l",)).labels(l="v").set(1.5)
    text = r.render()
    p = parse_prom(text)
    buckets = sorted((float(dict(k[1])["le"].replace("+Inf", "inf")), v) for k, v in p.items() if k[0] == "t_seconds_bucket")
    assert [b for _, b in buckets] == [1, 3, 4, 5] and [b for _, b in buckets] == sorted(b for _, b in buckets)    # cumulative, ends at the count
    assert p[("t_seconds_count", frozenset({("route", '/a \\"quoted\\"\\\\path\\nx')}))] == 5
    assert abs(next(v for k, v in p.items() if k[0] == "t_seconds_sum") - 56.05) < 1e-9
    assert "# HELP t_seconds Latency.\\nSecond line \\\\ backslash" in text and p[("t_g", frozenset({("l", "v")}))] == 1.5
    assert text.endswith("\n") and "\n\n" not in text


def test_metrics_are_thread_safe():
    r = Registry()
    c = r.counter("t_total", "c", ("k",))
    h = r.histogram("t_h", "h")

    def work():
        for _ in range(1000):
            c.labels(k="x").inc()
            h.observe(0.1)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert r.value("t_total", k="x") == 8000 and r.value("t_h") == 8000


def test_a_broken_collector_does_not_break_the_scrape():
    r = Registry()
    r.counter("t_ok_total", "ok").inc()
    r.collector("bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r.collector("good", lambda: [("t_live", "gauge", "live", [({"a": "b"}, 7)])])
    before = val("tia_metrics_collector_errors_total")
    p = parse_prom(r.render())
    assert p[("t_ok_total", frozenset())] == 1 and p[("t_live", frozenset({("a", "b")}))] == 7
    assert val("tia_metrics_collector_errors_total") == before + 1
    r.collector("bad", lambda: [])                                                           # collectors are named: replaced, never duplicated
    assert len(r.collectors) == 2


def test_timed_observes_elapsed_seconds():
    r = Registry()
    h = r.histogram("t_s", "s", ("k",))
    with metrics.timed(h, k="a"):
        time.sleep(0.02)
    assert r.value("t_s", k="a") == 1 and next(iter(h.values.values()))["sum"] >= 0.02


# =====================================================================================================================
# HTTP: metrics endpoint, request metrics, request ids, traces
# =====================================================================================================================
def test_metrics_endpoint_access_rules(app, monkeypatch):
    monkeypatch.delenv("TIA_METRICS_ALLOW_ANY")
    r = app.get("/metrics")                                                                    # TestClient is not the local machine
    assert r.status_code == 403 and "this machine only" in r.text
    monkeypatch.setenv("TIA_METRICS_TOKEN", "scrape-token-123")
    assert app.get("/metrics").status_code == 403 and app.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert app.get("/metrics", headers={"Authorization": "Basic scrape-token-123"}).status_code == 403
    ok = app.get("/metrics", headers={"Authorization": "Bearer scrape-token-123"})
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain; version=0.0.4") and "tia_build_info" in ok.text
    monkeypatch.delenv("TIA_METRICS_TOKEN")
    monkeypatch.setenv("TIA_METRICS_ALLOW_ANY", "true")
    assert app.get("/metrics").status_code == 200
    monkeypatch.delenv("TIA_METRICS_ALLOW_ANY")
    assert health.metrics_allowed("127.0.0.1", None)[0] and health.metrics_allowed("::1", None)[0]
    assert not health.metrics_allowed("10.1.2.3", None)[0] and not health.metrics_allowed("127.example.com", None)[0]


def test_http_metrics_use_route_templates_so_cardinality_stays_bounded(app):
    p0, _ = scrape(app)
    n0 = val("tia_http_requests_total", method="GET", route="/api/v1/impact-analyses/{aid}", status="404")
    for i in range(40):
        assert app.get(f"/api/v1/impact-analyses/tia-{uuid.uuid4().hex}").status_code == 404
    for i in range(40):
        app.get(f"/definitely/not/a/route/{i}/{uuid.uuid4().hex}")
    p, text = scrape(app)
    assert val("tia_http_requests_total", method="GET", route="/api/v1/impact-analyses/{aid}", status="404") == n0 + 40
    assert val("tia_http_requests_total", method="GET", route="unmatched", status="404") >= 40
    series = {dict(k[1]).get("route") for k in p if k[0] == "tia_http_requests_total"}
    assert not any("tia-" in s or "definitely" in s for s in series)                         # no raw path ever becomes a label value
    assert len(series) < 60 and val("tia_http_requests_in_flight") == 0
    assert val("tia_http_request_duration_seconds", method="GET", route="/api/v1/impact-analyses/{aid}") >= 40


def test_unknown_http_methods_do_not_create_series(app):
    for m in ("PROPFIND", "BREW", "X" + uuid.uuid4().hex[:6]):
        app.request(m, "/api/v1/repositories")
    methods = {dict(k[1]).get("method") for k in parse_prom(app.get("/metrics").text) if k[0] == "tia_http_requests_total"}
    assert methods <= {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "OTHER"}


def test_request_ids_are_echoed_when_safe_and_replaced_when_not(app):
    r = app.get("/api/v1/health", headers={"X-Request-ID": "req-abc_123.45"})
    assert r.headers["x-request-id"] == "req-abc_123.45" and re.fullmatch(r"[0-9a-f]{32}", r.headers["x-trace-id"])
    for bad in ("has space", "x" * 65, "semi;colon", "quote\"s", "<script>", "a/b"):
        got = app.get("/api/v1/health", headers={"X-Request-ID": bad}).headers["x-request-id"]
        assert got != bad and re.fullmatch(r"[0-9a-f]{32}", got), bad
    assert app.get("/api/v1/health").headers["x-request-id"] != app.get("/api/v1/health").headers["x-request-id"]


def test_an_incoming_trace_is_continued_and_a_malformed_one_ignored(app, tracer):
    tid, parent = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    r = app.get("/api/v1/health", headers={"traceparent": f"00-{tid}-{parent}-01"})
    assert r.headers["x-trace-id"] == tid
    sp = next(s for s in tracer.recent if s["trace_id"] == tid)
    assert sp["name"] == "http.request" and sp["kind"] == "server" and sp["parent_span_id"] == parent and sp["attributes"]["route"] == "/api/v1/health"
    for bad in ("garbage", f"00-{'0' * 32}-{parent}-01", f"00-{tid}-{'0' * 16}-01", f"01-{tid}-{parent}-01", f"00-{tid.upper()}-{parent}-zz", ""):
        assert app.get("/api/v1/health", headers={"traceparent": bad}).headers["x-trace-id"] != tid, bad


def test_refused_requests_are_counted_too(app):
    n403 = val("tia_http_requests_total", method="GET", route="/api/v1/repositories", status="403")
    d = val("tia_gate_denials_total", code="tenant_suspended")
    name = Tenants(conftest.PG.admin).get(app.tenant)["name"]
    Tenants(conftest.PG.admin).suspend(name, "test")
    assert app.get("/api/v1/repositories").status_code == 403
    assert val("tia_gate_denials_total", code="tenant_suspended") == d + 1
    assert val("tia_http_requests_total", method="GET", route="unmatched", status="403") + val(
        "tia_http_requests_total", method="GET", route="/api/v1/repositories", status="403") > n403 - 1                   # observed outermost
    Tenants(conftest.PG.admin).resume(name)


# =====================================================================================================================
# domain metrics: what the work produced
# =====================================================================================================================
def test_an_analysis_is_visible_in_the_analysis_metrics(app):
    b = {k: val(k) for k in ("tia_analysis_duration_seconds", "tia_analysis_traversal_nodes", "tia_analysis_truncated_total")}
    runs = val("tia_analysis_runs_total", status="complete_with_gaps", change_type="api_change")
    sev0 = {s: val("tia_analysis_findings_total", severity=s) for s in ("critical", "high", "medium", "low")}
    unk0 = {a: val("tia_analysis_unknowns_total", area=a) for a in ("event_consumers", "unresolved_outbound_calls")}
    res0 = {r: val("tia_resolution_calls_total", result=r) for r in ("resolved", "unresolved")}
    st0 = {s: val("tia_resolution_by_strategy_total", strategy=s) for s in ("R1", "R2", "R3", "R5")}
    rep = analyse(app)
    assert val("tia_analysis_runs_total", status="complete_with_gaps", change_type="api_change") == runs + 1
    for s, n in rep["summary"]["by_severity"].items():
        if s in sev0:
            assert val("tia_analysis_findings_total", severity=s) == sev0[s] + n, s
    for a in unk0:
        assert val("tia_analysis_unknowns_total", area=a) == unk0[a] + sum(1 for u in rep["unknowns"] if u["area"] == a)
    cov = rep["coverage"]
    assert val("tia_resolution_calls_total", result="resolved") == res0["resolved"] + cov["outbound_call_sites_resolved"]
    assert val("tia_resolution_calls_total", result="unresolved") == res0["unresolved"] + cov["outbound_call_sites"] - cov["outbound_call_sites_resolved"]
    for s, n in cov["resolution_by_strategy"].items():
        assert val("tia_resolution_by_strategy_total", strategy=s) == st0[s] + n
    assert val("tia_analysis_duration_seconds") == b["tia_analysis_duration_seconds"] + 1
    assert val("tia_analysis_traversal_nodes") == b["tia_analysis_traversal_nodes"] + 1 and cov["traversal"]["reached"] > 0
    assert val("tia_analysis_truncated_total") == b["tia_analysis_truncated_total"]
    analyse(app, max_results=2)
    assert val("tia_analysis_truncated_total") == b["tia_analysis_truncated_total"] + 1
    assert val("tia_analysis_runs_total", status="partial", change_type="api_change") >= 1
    assert val("tia_graph_nodes") > 0 and val("tia_graph_edges") > 0


def test_extraction_metrics_show_the_incremental_cache(app):
    before = {o: val("tia_extraction_files_total", outcome=o) for o in ("parsed", "reused", "failed")}
    n = val("tia_extraction_duration_seconds")
    analyse(app, "impl")                                                                      # the landscape was scanned on registration
    after = {o: val("tia_extraction_files_total", outcome=o) for o in ("parsed", "reused", "failed")}
    assert after["parsed"] >= before["parsed"] and after["reused"] > before["reused"]         # cached files are reused, not re-parsed
    assert val("tia_extraction_duration_seconds") > n
    app.post("/api/v1/graph/rescan")
    assert val("tia_extraction_files_total", outcome="parsed") > after["parsed"]               # a full rescan re-derives everything


def test_connector_metrics_count_git_outcomes(app, tmp_path):
    ok = val("tia_connector_requests_total", provider="git", operation="fetch", outcome="success")
    bad_clone = val("tia_connector_requests_total", provider="git", operation="clone", outcome="error")
    r = app.post("/api/v1/repositories", json={"url": (tmp_path / "does-not-exist.git").as_uri(), "name": "ghost"})
    assert r.status_code == 400
    assert val("tia_connector_requests_total", provider="git", operation="clone", outcome="error") == bad_clone + 1
    assert val("tia_connector_duration_seconds", provider="git", operation="clone") >= 1
    assert val("tia_connector_requests_total", provider="git", operation="fetch", outcome="success") >= ok


def test_publication_and_github_calls_are_counted(env, monkeypatch):
    monkeypatch.setattr("tia.api.github_full_name", lambda url: "acme/payments-service")
    gh_ok = val("tia_connector_requests_total", provider="github", operation="comment", outcome="success")
    pub = {k: val("tia_publish_total", kind=k[0], outcome=k[1]) for k in (("comment", "created"), ("comment", "updated"), ("check_run", "created"), ("check_run", "skipped"))}
    rep = env.c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": env.fx["shas"]["breaking"], "base": "main"}).json()["report"]
    url = f"/api/v1/impact-analyses/{rep['analysis_id']}/publish"
    assert env.c.post(url, json={"repository": "payments-service", "pr": 1}).status_code == 200
    assert env.c.post(url, json={"repository": "payments-service", "pr": 1}).status_code == 200
    assert val("tia_publish_total", kind="comment", outcome="created") == pub[("comment", "created")] + 1
    assert val("tia_publish_total", kind="comment", outcome="updated") == pub[("comment", "updated")] + 1
    assert val("tia_publish_total", kind="check_run", outcome="created") == pub[("check_run", "created")] + 1
    assert val("tia_connector_requests_total", provider="github", operation="comment", outcome="success") >= gh_ok + 3     # list + create + list + patch
    env.gh.check_status = 403                                                                  # a personal token cannot create check runs
    other = env.c.post(url, json={"repository": "payments-service", "pr": 2}).json()
    assert "skipped" in other["check_run"]
    assert val("tia_publish_total", kind="check_run", outcome="skipped") == pub[("check_run", "skipped")] + 1
    assert val("tia_connector_requests_total", provider="github", operation="check_run", outcome="http_4xx") >= 1
    env.gh.fail_status = 500
    before = val("tia_publish_total", kind="comment", outcome="failed")
    assert env.c.post(url, json={"repository": "payments-service", "pr": 3}).status_code == 502
    assert val("tia_publish_total", kind="comment", outcome="failed") == before + 1


def test_queue_and_worker_metrics(env):
    kind = "impact_analysis"
    set_pr(env.origins, 71, env.fx["shas"]["breaking"])
    jobs_before = val("tia_jobs_runs_total", kind="github_pull_request", outcome="succeeded")
    dur = val("tia_jobs_duration_seconds", kind="github_pull_request")
    env.post(env.event(71, env.fx["shas"]["breaking"]))
    p, _ = scrape(env.c)
    queued = next(v for k, v in p.items() if k[0] == "tia_jobs" and dict(k[1]) == {"status": "queued"})
    assert queued >= 1 and next(v for k, v in p.items() if k[0] == "tia_jobs_oldest_queued_seconds") >= 0                  # the collector sees the queue
    env.drain()
    assert val("tia_jobs_runs_total", kind="github_pull_request", outcome="succeeded") == jobs_before + 1
    assert val("tia_jobs_duration_seconds", kind="github_pull_request") == dur + 1
    assert time.time() - val("tia_worker_last_poll_timestamp_seconds") < 30
    p2, _ = scrape(env.c)
    assert next(v for k, v in p2.items() if k[0] == "tia_jobs" and dict(k[1]) == {"status": "succeeded"}) >= 1
    assert {"tia_db_pool_size", "tia_db_pool_available", "tia_db_pool_requests_waiting"} <= {k[0] for k in p2}


def test_a_failing_job_is_counted_as_retried_then_failed(env, monkeypatch):
    kind = "kind-" + uuid.uuid4().hex[:8]
    q = JobQueue(conftest.PG.worker_db)
    JobQueue(conftest.PG.db).enqueue(env.tenant, kind, {}, key="boom", max_attempts=2)
    calls = []

    def boom(job):
        calls.append(job.attempts)
        raise RuntimeError("explode")
    w = Worker(q, {kind: boom}, base_delay_s=0)
    while w.run_once():
        pass
    assert calls == [1, 2]
    assert val("tia_jobs_runs_total", kind=kind, outcome="retried") == 1 and val("tia_jobs_runs_total", kind=kind, outcome="failed") == 1


def test_repository_freshness_and_relationship_metrics(app):
    p, _ = scrape(app)
    repos = {dict(k[1])["scan_status"]: v for k, v in p.items() if k[0] == "tia_repositories"}
    assert sum(repos.values()) == 6 and repos.get("ok", 0) + repos.get("partial", 0) == 6
    assert next(v for k, v in p.items() if k[0] == "tia_repository_oldest_successful_scan_seconds") >= 0
    rel = {dict(k[1])["state"]: v for k, v in p.items() if k[0] == "tia_relationships"}
    assert set(rel) == {"current", "stale"} and rel["current"] >= 1


def test_platform_health_is_separate_from_analysis_completeness(app):
    """A partial analysis must not make the platform unready, and the metric families do not overlap."""
    analyse(app, max_results=2)                                                                # status=partial
    code = app.get("/readyz").status_code
    body = app.get("/readyz").json()
    assert code == 200 and body["status"] in ("ready", "degraded") and "analysis" not in json.dumps(body["checks"]).lower()
    names = {k[0] for k in parse_prom(app.get("/metrics").text)}
    analysis = {n for n in names if n.startswith(("tia_analysis_", "tia_resolution_", "tia_extraction_", "tia_graph_"))}
    platform = {n for n in names if n.startswith(("tia_http_", "tia_db_", "tia_jobs", "tia_worker_", "tia_connector_", "tia_publish_", "tia_gate_"))}
    assert analysis and platform and not (analysis & platform)


def test_no_metric_label_carries_a_tenant_repository_or_path(app):
    analyse(app)
    p, text = scrape(app)
    leaked = [s for s in (app.tenant, "payments-service", "mobile-bff", str(app.ws), "reconciliation") if s in text]
    assert not leaked, leaked
    assert val("tia_build_info", extractors=__import__("tia").EXTRACTOR_VERSION, config=__import__("tia").CONFIG_VERSION) == 1


def test_withheld_secrets_are_counted(tmp_path):
    from test_secretstore import LEAKY_APP
    from fixtures.build_fixture import _commit, _run, _write
    p = tmp_path / "leaky"
    p.mkdir()
    _run(p, "init", "-q", "-b", "main")
    _write(p, {"app.py": LEAKY_APP})
    _commit(p, "init")
    _run(p, "checkout", "-q", "-b", "c", "main")
    _write(p, {"app.py": LEAKY_APP.replace("def item", "def item2")})
    _commit(p, "c")
    c = TestClient(make_app(tmp_path / "ws", allow_local=True, gate_ttl=0))
    c.post("/api/v1/repositories", json={"path": str(p), "name": "leaky"})
    before = val("tia_secrets_withheld_total")
    rep = c.post("/api/v1/impact-analyses", json={"target": "leaky", "head": "c", "base": "main"}).json()["report"]
    n = next(u["count"] for u in rep["unknowns"] if u["area"] == "secrets_withheld")
    assert val("tia_secrets_withheld_total") == before + n and n >= 1


# =====================================================================================================================
# structured logs
# =====================================================================================================================
def test_every_log_line_is_json_with_the_request_and_trace(app, logbuf):
    r = app.get("/api/v1/repositories", headers={"X-Request-ID": "rid-42"})
    recs = logs(logbuf)
    req = next(x for x in recs if x["msg"] == "request" and x["route"] == "/api/v1/repositories")
    assert req["request_id"] == "rid-42" and req["trace_id"] == r.headers["x-trace-id"] and req["span_id"]
    assert req["method"] == "GET" and req["status"] == 200 and req["duration_ms"] >= 0 and req["level"] == "INFO" and req["logger"] == "tia.http"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", req["ts"])
    assert all("level" in x and "ts" in x and "msg" in x and "logger" in x for x in recs)


def test_logs_never_carry_query_strings_bodies_or_secrets(app, logbuf):
    app.get(f"/api/v1/repositories?token={TOKEN}&password=hunter2hunter2")
    app.post("/api/v1/repositories", json={"url": "https://example.invalid/x.git", "token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "name": "x"})
    blob = logbuf.getvalue()
    for secret in (TOKEN, "hunter2hunter2", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "token=", "?"):
        assert secret not in blob, secret
    assert any(x["route"] == "/api/v1/repositories" for x in logs(logbuf) if x["msg"] == "request")


def test_secrets_in_messages_extras_and_exceptions_are_scrubbed(logbuf):
    log = logging.getLogger("tia.test")
    gh = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    try:
        raise RuntimeError(f"clone failed for https://u:S3cretPw@git.corp/x with {gh}")
    except RuntimeError:
        log.error("boom password=hunter2hunter2", extra={"repo": "r", "detail": f"token={gh}", "nested": {"k": "AKIAIOSFODNN7EXAMPLE"}}, exc_info=True)
    rec = logs(logbuf)[-1]
    blob = json.dumps(rec)
    for secret in (gh, "S3cretPw", "hunter2hunter2", "AKIAIOSFODNN7EXAMPLE"):
        assert secret not in blob, secret
    assert "[REDACTED" in blob and rec["repo"] == "r" and "RuntimeError" in rec["exception"] and rec["level"] == "ERROR"


def test_request_context_is_isolated_between_concurrent_requests(app, logbuf):
    ids = [f"conc-{i}" for i in range(12)]

    def hit(i):
        app.get("/api/v1/repositories", headers={"X-Request-ID": i})
    ts = [threading.Thread(target=hit, args=(i,)) for i in ids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    got = [x["request_id"] for x in logs(logbuf) if x["msg"] == "request" and x["route"] == "/api/v1/repositories"]
    assert sorted(got) == sorted(ids)                                                           # each line carries its own request's id


def test_probe_endpoints_are_logged_at_debug_only(app):
    buf = io.StringIO()
    obs.setup_logging("INFO", "json", buf)
    try:
        for p in ("/healthz", "/readyz", "/metrics", "/api/v1/health"):
            app.get(p)
        app.get("/api/v1/repositories")
        routes = [x["route"] for x in logs(buf) if x["msg"] == "request"]
        assert routes == ["/api/v1/repositories"]                                              # orchestrator probes do not flood the log
    finally:
        logging.getLogger("tia").handlers = [h for h in logging.getLogger("tia").handlers if not getattr(h, "_tia", False)]


def test_text_format_level_filter_and_idempotent_setup():
    buf = io.StringIO()
    obs.setup_logging("WARNING", "text", buf)
    obs.setup_logging("WARNING", "text", buf)                                                   # twice: still one handler
    lg = logging.getLogger("tia.t")
    lg.info("not shown")
    lg.warning("shown password=hunter2hunter2")
    out = buf.getvalue()
    assert out.count("shown") == 1 and "not shown" not in out and "hunter2hunter2" not in out and "WARNING" in out
    assert sum(1 for h in logging.getLogger("tia").handlers if getattr(h, "_tia", False)) == 1
    logging.getLogger("tia").handlers = []
    logging.getLogger("tia").propagate = True


# =====================================================================================================================
# traces
# =====================================================================================================================
@pytest.mark.parametrize("header,ok", [
    ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", True), ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00", True),
    ("  00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01  ", True), ("00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01", True),
    ("00-00000000000000000000000000000000-00f067aa0ba902b7-01", False), ("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01", False),
    ("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", False), ("00-short-00f067aa0ba902b7-01", False), ("", False), (None, False),
    ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7", False), ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra", False)])
def test_traceparent_parsing(header, ok):
    p = parse_traceparent(header)
    assert (p is not None) == ok
    if ok:
        assert p["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736" and p["span_id"] == "00f067aa0ba902b7"
        assert p["sampled"] == (header.strip().endswith("-01"))


def test_spans_nest_carry_attributes_and_mark_errors(tracer):
    with tracer.span("outer", "server", a=1) as o:
        with tracer.span("inner", token="ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8") as i:
            assert i.trace_id == o.trace_id and i.parent_id == o.span_id and tracing.current_traceparent() == i.traceparent
        with pytest.raises(ValueError):
            with tracer.span("failing"):
                raise ValueError("bad password=hunter2hunter2")
    assert tracing.current_traceparent() is None
    by = {s["name"]: s for s in tracer.recent}
    assert by["inner"]["parent_span_id"] == by["outer"]["span_id"] and by["outer"]["parent_span_id"] is None
    assert by["failing"]["status"] == "error" and "ValueError" in by["failing"]["error"] and "hunter2hunter2" not in json.dumps(by)
    assert "ghp_A1b2" not in json.dumps(by) and by["outer"]["attributes"] == {"a": 1} and by["outer"]["duration_ms"] >= 0


def test_sampling_decision_is_made_once_and_propagated(tracer):
    t0 = Tracer(None, 0.0)
    with t0.span("root") as root:
        assert root.sampled is False and root.traceparent.endswith("-00")
        with t0.span("child") as ch:
            assert ch.sampled is False
    t1 = Tracer(None, 1.0)
    with t1.span("root") as root:
        assert root.traceparent.endswith("-01")
    with t0.span("continued", parent={"trace_id": "a" * 32, "span_id": "b" * 16, "sampled": True}) as s:
        assert s.sampled is True                                                              # the caller's decision wins over local sampling


def test_a_request_produces_a_trace_through_analysis_extraction_and_git(app, tracer):
    tid = "0af7651916cd43dd8448eb211c80319c"
    r = app.post("/api/v1/repositories/payments-service/sync", json={}, headers={"traceparent": f"00-{tid}-b7ad6b7169203331-01"})
    assert r.status_code == 200
    app.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": app.fx["shas"]["impl"], "base": "main"},
             headers={"traceparent": f"00-{tid}-b7ad6b7169203331-01"})
    spans = [s for s in tracer.recent if s["trace_id"] == tid]
    names = {s["name"] for s in spans}
    assert {"http.request", "analysis.run", "extraction.run"} <= names
    by_id = {s["span_id"]: s for s in spans}
    ana = next(s for s in spans if s["name"] == "analysis.run")
    assert by_id[ana["parent_span_id"]]["name"] == "http.request"                              # analysis hangs off its request
    assert ana["attributes"]["status"] in ("complete", "complete_with_gaps", "partial") and ana["attributes"]["target"] == "payments-service"
    ext = next(s for s in spans if s["name"] == "extraction.run" and s["parent_span_id"] == ana["span_id"])
    assert ext["attributes"]["files"] > 0 and "payments-service" == ext["attributes"]["repo"]
    assert all(s["end_ns"] >= s["start_ns"] for s in spans)


def test_the_trace_is_propagated_to_github(env, tracer, monkeypatch):
    monkeypatch.setattr("tia.api.github_full_name", lambda url: "acme/payments-service")
    rep = env.c.post("/api/v1/impact-analyses", json={"target": "payments-service", "head": env.fx["shas"]["breaking"], "base": "main"}).json()["report"]
    tid = "11111111111111111111111111111111"
    env.gh.traceparents.clear()
    r = env.c.post(f"/api/v1/impact-analyses/{rep['analysis_id']}/publish", json={"repository": "payments-service", "pr": 5},
                   headers={"traceparent": f"00-{tid}-2222222222222222-01"})
    assert r.status_code == 200
    seen = [parse_traceparent(t) for t in env.gh.traceparents]
    assert seen and all(s and s["trace_id"] == tid and s["sampled"] for s in seen)              # every GitHub call continued the trace
    assert all(s["span_id"] != "2222222222222222" for s in seen)                                # ...as a child span, not the caller's own id
    gh_spans = [s for s in tracer.recent if s["name"].startswith("github.") and s["trace_id"] == tid]
    assert gh_spans and all(s["kind"] == "client" for s in gh_spans)
    assert {s["span_id"] for s in gh_spans} == {x["span_id"] for x in seen}                     # the header's span id is the client span


def test_the_trace_continues_through_the_queue_into_the_worker(env, tracer):
    tid = "33333333333333333333333333333333"
    set_pr(env.origins, 81, env.fx["shas"]["breaking"])
    body = {"target": "payments-service", "head": env.fx["shas"]["impl"], "base": "main"}
    r1 = env.c.post("/api/v1/impact-analyses?async=true", json=body, headers={"traceparent": f"00-{tid}-4444444444444444-01"}).json()
    r2 = env.c.post("/api/v1/impact-analyses?async=true", json=body, headers={"traceparent": f"00-{'5' * 32}-6666666666666666-01"}).json()
    assert r2["job_id"] == r1["job_id"] and r2["deduplicated"] is True                           # the trace is not part of the job's identity
    env.drain()
    run = next(s for s in tracer.recent if s["name"] == "job.run" and s["trace_id"] == tid)
    assert run["kind"] == "consumer" and run["attributes"]["job_kind"] == "impact_analysis" and run["attributes"]["outcome"] == "succeeded"
    assert any(s["name"] == "analysis.run" and s["trace_id"] == tid for s in tracer.recent)      # the worker's analysis is in the request's trace
    http = next(s for s in tracer.recent if s["name"] == "http.request" and s["trace_id"] == tid)
    assert run["parent_span_id"] and run["parent_span_id"] != http["parent_span_id"]


# ---- OTLP export --------------------------------------------------------------------------------------------------------------------
def test_otlp_export_delivers_valid_spans_to_a_collector():
    col = FakeCollector()
    ex = OtlpExporter(col.endpoint, {"Authorization": "Bearer otel-token"}, "tia-test", interval_s=60)
    try:
        t = Tracer(ex, 1.0)
        with t.span("http.request", "server", route="/x", status=200, ok=True, ratio=0.5, token="ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8") as root:
            with t.span("analysis.run", "internal", n=3):
                pass
            with pytest.raises(RuntimeError):
                with t.span("git.fetch", "client"):
                    raise RuntimeError("fetch failed")
        ex.flush()
        spans = col.spans()
        assert len(spans) == 3 and col.headers[0]["Authorization"] == "Bearer otel-token" and col.headers[0]["Content-Type"] == "application/json"
        rs = col.payloads[0]["resourceSpans"][0]
        assert {"key": "service.name", "value": {"stringValue": "tia-test"}} in rs["resource"]["attributes"]
        by = {s["name"]: s for s in spans}
        for s in spans:
            assert re.fullmatch(r"[0-9a-f]{32}", s["traceId"]) and re.fullmatch(r"[0-9a-f]{16}", s["spanId"])
            assert s["traceId"] == root.trace_id and int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"])
        assert "parentSpanId" not in by["http.request"] and by["analysis.run"]["parentSpanId"] == by["http.request"]["spanId"]
        assert by["http.request"]["kind"] == 2 and by["analysis.run"]["kind"] == 1 and by["git.fetch"]["kind"] == 3
        assert by["git.fetch"]["status"]["code"] == 2 and "RuntimeError" in by["git.fetch"]["status"]["message"] and by["analysis.run"]["status"]["code"] == 1
        attrs = {a["key"]: a["value"] for a in by["http.request"]["attributes"]}
        assert attrs["route"] == {"stringValue": "/x"} and attrs["status"] == {"intValue": "200"} and attrs["ok"] == {"boolValue": True}
        assert attrs["ratio"] == {"doubleValue": 0.5} and "ghp_A1b2" not in json.dumps(spans) and "[REDACTED:github_token]" in json.dumps(spans)
    finally:
        ex.shutdown()
        col.close()


def test_unsampled_spans_are_not_exported_and_batches_are_bounded():
    col = FakeCollector()
    ex = OtlpExporter(col.endpoint, None, "tia", batch=2, interval_s=60)
    try:
        off = Tracer(ex, 0.0)
        with off.span("dropped"):
            pass
        on = Tracer(ex, 1.0)
        for i in range(5):
            with on.span(f"s{i}"):
                pass
        ex.flush()
        assert len(col.payloads) == 3 and [len(p["resourceSpans"][0]["scopeSpans"][0]["spans"]) for p in col.payloads] == [2, 2, 1]
        assert {s["name"] for s in col.spans()} == {f"s{i}" for i in range(5)}
    finally:
        ex.shutdown()
        col.close()


def test_a_dead_or_slow_collector_never_blocks_or_breaks_the_work():
    col = FakeCollector(status=503)
    ex = OtlpExporter(col.endpoint, None, "tia", max_queue=3, interval_s=60)
    try:
        t = Tracer(ex, 1.0)
        failed, dropped = val("tia_trace_export_spans_total", outcome="failed"), val("tia_trace_export_spans_total", outcome="dropped")
        t0 = time.perf_counter()
        for i in range(10):
            with t.span(f"s{i}"):
                pass
        assert time.perf_counter() - t0 < 1.0                                                  # export is off the request path
        assert val("tia_trace_export_spans_total", outcome="dropped") == dropped + 7           # the queue is bounded: overflow is counted
        ex.flush()
        assert val("tia_trace_export_spans_total", outcome="failed") == failed + 3             # a failed batch is counted, not retried forever
    finally:
        ex.shutdown()
        col.close()
    ex2 = OtlpExporter("http://127.0.0.1:1", None, "tia", interval_s=60)                       # nothing listens there
    with Tracer(ex2, 1.0).span("x"):
        pass
    ex2.flush()                                                                                 # does not raise
    ex2.shutdown()


def test_otlp_endpoint_must_be_https_unless_local():
    with pytest.raises(TransportError, match="TIA_OTLP_ENDPOINT must use https"):
        OtlpExporter("http://collector.corp.example:4318")
    ex = OtlpExporter("https://collector.corp.example:4318/", None, interval_s=3600)
    assert ex.url == "https://collector.corp.example:4318/v1/traces"
    ex.shutdown()


def test_tracer_configuration_from_the_environment(monkeypatch, tmp_path):
    col = FakeCollector()
    f = tmp_path / "hdrs"
    f.write_text("Authorization=Bearer from-file,X-Scope=tia\n")
    monkeypatch.setenv("TIA_OTLP_ENDPOINT", col.endpoint)
    monkeypatch.setenv("TIA_OTLP_HEADERS", f"file:{f}")
    monkeypatch.setenv("TIA_TRACE_SAMPLE", "0.25")
    old = tracing.TRACER
    try:
        t = tracing.configure_from_env()
        assert t.ratio == 0.25 and t.exporter.headers == {"Authorization": "Bearer from-file", "X-Scope": "tia"}
        t.exporter.shutdown()
    finally:
        tracing.TRACER = old
        col.close()


# =====================================================================================================================
# health and readiness
# =====================================================================================================================
def test_liveness_has_no_dependencies(app, monkeypatch):
    r = app.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["uptime_seconds"] >= 0
    name = Tenants(conftest.PG.admin).get(app.tenant)["name"]
    Tenants(conftest.PG.admin).suspend(name, "x")
    assert app.get("/healthz").status_code == 200 and app.get("/readyz").status_code == 200          # a suspended tenant is not a sick platform
    assert app.get("/api/v1/repositories").status_code == 403
    Tenants(conftest.PG.admin).resume(name)


def test_readiness_when_everything_works(app):
    r = app.get("/readyz")
    body = r.json()
    assert r.status_code == 200 and body["status"] in ("ready", "degraded")
    assert body["checks"]["database"]["ok"] and body["checks"]["schema"]["ok"] and body["checks"]["workspace"]["ok"]
    assert body["checks"]["schema"]["detail"].startswith("schema 00")
    q = body["checks"]["queue"]["detail"]
    assert set(q) == {"queued", "oldest_queued_seconds", "workers_seen_last_minute"} and "platform readiness only" in body["note"]
    text = json.dumps(body)
    assert app.tenant not in text and "postgres" not in text.lower() and str(app.ws) not in text                      # nothing sensitive is returned


def test_readiness_fails_when_the_database_is_unreachable(app):
    class Dead:
        def connection(self, *a, **k):
            raise RuntimeError("connection refused to postgres://user:pw@host/db")
    db = type("D", (), {"pool": Dead()})()
    code, body = health.readiness(db, app.tenant, str(app.ws))
    assert code == 503 and body["status"] == "not_ready" and body["checks"]["database"]["ok"] is False
    assert body["checks"]["schema"]["detail"] == "not checked: database unreachable" and "queue" not in body["checks"]
    assert "pw" not in json.dumps(body) and "RuntimeError" in body["checks"]["database"]["detail"]                      # the class, never the message


def test_readiness_fails_when_the_schema_is_behind_the_code(app):
    code, body = health.readiness(conftest.PG.db, app.tenant, str(app.ws), expected="0099_future")
    assert code == 503 and body["status"] == "not_ready" and "behind this code (0099_future)" in body["checks"]["schema"]["detail"]
    assert "python -m tia.migrate" in body["checks"]["schema"]["detail"]
    code, body = health.readiness(conftest.PG.db, app.tenant, str(app.ws), expected="0001_init")
    assert code == 200 and "newer than this code" in body["checks"]["schema"]["detail"]                                 # a rolling upgrade is fine


def test_readiness_fails_when_the_workspace_is_not_writable(app, tmp_path):
    f = tmp_path / "a-file-not-a-directory"
    f.write_text("x")
    code, body = health.readiness(conftest.PG.db, app.tenant, str(f / "sub"))
    assert code == 503 and body["checks"]["workspace"]["ok"] is False and str(tmp_path) not in json.dumps(body)


def test_readiness_is_degraded_when_nothing_consumes_the_queue(app, monkeypatch):
    import psycopg
    with psycopg.connect(conftest.PG.admin, autocommit=True) as c:             # other tests' workers are not this scenario's workers
        c.execute("delete from worker_heartbeats")
    JobQueue(conftest.PG.db).enqueue(app.tenant, "impact_analysis", {}, key="stuck-" + uuid.uuid4().hex)
    code, body = health.readiness(conftest.PG.db, app.tenant, str(app.ws))
    assert code == 200 and body["status"] == "degraded" and "no worker has been seen recently" in body["degraded"]
    JobQueue(conftest.PG.worker_db).beat("w-" + uuid.uuid4().hex, {"v": 1})                                              # a worker announces itself
    code, body = health.readiness(conftest.PG.db, app.tenant, str(app.ws))
    assert body["checks"]["queue"]["detail"]["workers_seen_last_minute"] >= 1 and "no worker has been seen recently" not in body.get("degraded", [])
    monkeypatch.setattr(health, "QUEUE_STALE_SECONDS", 0)
    time.sleep(0.05)
    code, body = health.readiness(conftest.PG.db, app.tenant, str(app.ws))
    assert code == 200 and body["status"] == "degraded" and any("has waited" in d for d in body["degraded"])


def test_schema_version_function_and_heartbeat_privileges():
    import psycopg
    with psycopg.connect(conftest.PG.app_dsn) as c:
        assert c.execute("select tia_schema_version()").fetchone()[0] == health.expected_schema_version()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into worker_heartbeats (worker_id) values ('x')")                                          # only workers write them
    with psycopg.connect(conftest.PG.app_dsn) as c:
        assert c.execute("select count(*) from worker_heartbeats").fetchone()[0] >= 0                                    # the API can read them
    with psycopg.connect(conftest.PG.worker_dsn, autocommit=True) as c:
        c.execute("insert into worker_heartbeats (worker_id) values ('hb-test') on conflict (worker_id) do update set last_seen = now()")


def test_worker_endpoints_serve_metrics_and_health():
    import urllib.request, urllib.error
    srv = health.serve_worker_endpoints(0, conftest.PG.worker_db)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        metrics.WORKER_POLL.set(time.time())
        assert json.loads(urllib.request.urlopen(base + "/healthz", timeout=5).read())["status"] == "ok"
        m = urllib.request.urlopen(base + "/metrics", timeout=5).read().decode()                                          # loopback is allowed by default
        assert "tia_worker_last_poll_timestamp_seconds" in m and parse_prom(m)
        ready = json.loads(urllib.request.urlopen(base + "/readyz", timeout=5).read())
        assert ready["status"] == "ready" and ready["checks"]["poll"]["ok"]
        metrics.WORKER_POLL.set(time.time() - 600)                                                                      # a worker that stopped polling
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/readyz", timeout=5)
        assert e.value.code == 503
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/nope", timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()
        metrics.WORKER_POLL.set(time.time())
