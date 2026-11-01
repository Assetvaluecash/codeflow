"""Platform and analysis metrics in the Prometheus text exposition format (PL-007, TFD 14). No dependency: a small registry.

Two families, deliberately separate because "a healthy service can still produce an incomplete report":
  platform   tia_http_*, tia_db_*, tia_jobs_*, tia_worker_*, tia_connector_*, tia_publish_*, tia_gate_*      is the service working?
  analysis   tia_analysis_*, tia_resolution_*, tia_extraction_*, tia_graph_*, tia_repositories*              how complete is what it produced?

Label values are always drawn from small fixed sets (a route TEMPLATE, never a raw path; a status, never an id) so cardinality is
bounded whatever callers send. Nothing here records a tenant id, a repository name, a path or any user content.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Iterable, Optional

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
START = time.time()


def _esc_help(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n")


def _esc_label(s) -> str:
    return str(s).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(v: float) -> str:
    if v == math.inf:
        return "+Inf"
    if v == -math.inf:
        return "-Inf"
    if v != v:
        return "NaN"
    return repr(int(v)) if float(v).is_integer() and abs(v) < 1e15 else repr(float(v))


class _Child:
    def __init__(self, metric, key):
        self.m, self.key = metric, key

    def inc(self, n: float = 1.0):
        if n < 0 and self.m.kind == "counter":
            raise ValueError("a counter cannot decrease")
        with self.m.lock:
            self.m.values[self.key] = self.m.values.get(self.key, 0.0) + n

    def dec(self, n: float = 1.0):
        self.inc(-n)

    def set(self, v: float):
        if self.m.kind != "gauge":
            raise ValueError("only a gauge can be set")
        with self.m.lock:
            self.m.values[self.key] = float(v)

    def observe(self, v: float):
        if self.m.kind != "histogram":
            raise ValueError("only a histogram can observe")
        with self.m.lock:
            b = self.m.values.setdefault(self.key, {"buckets": [0] * len(self.m.buckets), "sum": 0.0, "count": 0})
            for i, bound in enumerate(self.m.buckets):
                if v <= bound:
                    b["buckets"][i] += 1
            b["sum"] += v
            b["count"] += 1


class Metric:
    def __init__(self, kind: str, name: str, help_: str, labels: tuple, buckets: Optional[tuple] = None):
        self.kind, self.name, self.help, self.label_names = kind, name, help_, tuple(labels)
        self.buckets = tuple(buckets or DEFAULT_BUCKETS)
        self.values: dict = {}
        self.lock = threading.Lock()

    def labels(self, *args, **kw) -> _Child:
        if args and kw:
            raise ValueError("use positional or keyword labels, not both")
        vals = tuple(str(a) for a in args) if args else tuple(str(kw[k]) for k in self.label_names if k in kw)
        if len(vals) != len(self.label_names) or (kw and set(kw) != set(self.label_names)):
            raise ValueError(f"{self.name} takes labels {self.label_names}")
        return _Child(self, vals)

    def inc(self, n: float = 1.0):                          # unlabelled shortcuts
        self.labels().inc(n)

    def dec(self, n: float = 1.0):
        self.labels().dec(n)

    def set(self, v: float):
        self.labels().set(v)

    def observe(self, v: float):
        self.labels().observe(v)

    def samples(self) -> Iterable[tuple]:
        with self.lock:
            items = sorted(self.values.items())
        for key, v in items:
            base = dict(zip(self.label_names, key))
            if self.kind == "histogram":
                for bound, n in zip(self.buckets, v["buckets"]):
                    yield f"{self.name}_bucket", {**base, "le": _fmt(bound)}, n
                yield f"{self.name}_bucket", {**base, "le": "+Inf"}, v["count"]
                yield f"{self.name}_sum", base, v["sum"]
                yield f"{self.name}_count", base, v["count"]
            else:
                yield self.name, base, v


class Registry:
    def __init__(self):
        self.metrics: dict = {}
        self.collectors: dict = {}
        self.lock = threading.Lock()

    def _add(self, kind, name, help_, labels, buckets=None) -> Metric:
        with self.lock:
            if name in self.metrics:
                m = self.metrics[name]
                if m.kind != kind or m.label_names != tuple(labels):
                    raise ValueError(f"metric {name} is already registered differently")
                return m
            m = self.metrics[name] = Metric(kind, name, help_, labels, buckets)
            return m

    def counter(self, name, help_, labels=()) -> Metric:
        return self._add("counter", name, help_, labels)

    def gauge(self, name, help_, labels=()) -> Metric:
        return self._add("gauge", name, help_, labels)

    def histogram(self, name, help_, labels=(), buckets=None) -> Metric:
        return self._add("histogram", name, help_, labels, buckets)

    def collector(self, name: str, fn: Callable[[], Iterable[tuple]]) -> None:
        """Register (or replace) a named collector. fn() yields (name, kind, help, {labels: value}) families computed at scrape time (queue depth, pool stats, freshness)."""
        self.collectors[name] = fn

    def render(self) -> str:
        out = []
        for name in sorted(self.metrics):
            m = self.metrics[name]
            out.append(f"# HELP {name} {_esc_help(m.help)}")
            out.append(f"# TYPE {name} {m.kind}")
            for sname, labels, v in m.samples():
                out.append(_line(sname, labels, v))
        for fn in list(self.collectors.values()):
            try:
                families = list(fn())
            except Exception:                                            # noqa: BLE001 - a broken collector must not break the scrape
                COLLECTOR_ERRORS.inc()
                continue
            for name, kind, help_, series in families:
                out.append(f"# HELP {name} {_esc_help(help_)}")
                out.append(f"# TYPE {name} {kind}")
                for labels, v in sorted(series, key=lambda x: sorted(x[0].items())):
                    out.append(_line(name, labels, v))
        return "\n".join(out) + "\n"

    def value(self, name: str, **labels) -> Optional[float]:
        """Current value of one series (for tests and health): counter/gauge value, histogram count."""
        m = self.metrics.get(name)
        if not m:
            return None
        key = tuple(str(labels[k]) for k in m.label_names)
        v = m.values.get(key)
        return v["count"] if isinstance(v, dict) else v


def _line(name: str, labels: dict, v) -> str:
    lab = ",".join(f'{k}="{_esc_label(val)}"' for k, val in labels.items())
    return f"{name}{{{lab}}} {_fmt(v)}" if lab else f"{name} {_fmt(v)}"


REGISTRY = Registry()
C, G, H = REGISTRY.counter, REGISTRY.gauge, REGISTRY.histogram

# ---- platform -------------------------------------------------------------------------------------------------------------------
HTTP_REQUESTS = C("tia_http_requests_total", "HTTP requests by method, route template and status.", ("method", "route", "status"))
HTTP_DURATION = H("tia_http_request_duration_seconds", "HTTP request duration by method and route template.", ("method", "route"))
HTTP_IN_FLIGHT = G("tia_http_requests_in_flight", "HTTP requests being served.")
GATE_DENIALS = C("tia_gate_denials_total", "Requests refused by the tenant lifecycle or storage quota.", ("code",))
JOB_RUNS = C("tia_jobs_runs_total", "Job executions by kind and outcome (succeeded, failed, retried).", ("kind", "outcome"))
JOB_DURATION = H("tia_jobs_duration_seconds", "Job execution time by kind.", ("kind",), buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 900))
WORKER_POLL = G("tia_worker_last_poll_timestamp_seconds", "Unix time the worker last polled the queue (liveness).")
CONNECTOR = C("tia_connector_requests_total", "Calls to source-code and SCM systems by provider, operation and outcome.",
              ("provider", "operation", "outcome"))
CONNECTOR_DURATION = H("tia_connector_duration_seconds", "Duration of calls to source-code and SCM systems.", ("provider", "operation"),
                       buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900))
PUBLISH = C("tia_publish_total", "Pull-request publications by kind (comment, check_run) and outcome.", ("kind", "outcome"))
TRACE_EXPORT = C("tia_trace_export_spans_total", "Spans handed to the trace exporter by outcome (exported, dropped, failed).", ("outcome",))
COLLECTOR_ERRORS = C("tia_metrics_collector_errors_total", "Scrape-time collectors that raised.")
SECRETS_WITHHELD = C("tia_secrets_withheld_total", "Secret-like values withheld from records and reports.")

# ---- analysis ---------------------------------------------------------------------------------------------------------------------
ANALYSES = C("tia_analysis_runs_total", "Analyses by completion status and change type.", ("status", "change_type"))
ANALYSIS_DURATION = H("tia_analysis_duration_seconds", "Time to produce an analysis.", (), buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
FINDINGS = C("tia_analysis_findings_total", "Findings produced by severity.", ("severity",))
UNKNOWNS = C("tia_analysis_unknowns_total", "Unknowns reported by area.", ("area",))
TRAVERSAL_NODES = H("tia_analysis_traversal_nodes", "Graph nodes reached by one traversal.", (), buckets=(1, 5, 10, 25, 50, 100, 200, 500, 1000, 5000))
TRUNCATED = C("tia_analysis_truncated_total", "Analyses whose traversal hit a depth, result or time limit.")
RESOLUTION = C("tia_resolution_calls_total", "Outbound call sites by whether they resolved to a provider.", ("result",))
RESOLUTION_STRATEGY = C("tia_resolution_by_strategy_total", "Resolved call sites by resolution strategy (R1-R5).", ("strategy",))
EXTRACTION_FILES = C("tia_extraction_files_total", "Files extracted by outcome (parsed, reused, failed).", ("outcome",))
EXTRACTION_DURATION = H("tia_extraction_duration_seconds", "Time to extract one repository revision.", (),
                        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
GRAPH_NODES = G("tia_graph_nodes", "Nodes in the most recently built graph.")
GRAPH_EDGES = G("tia_graph_edges", "Edges in the most recently built graph.")
BUILD_INFO = G("tia_build_info", "Always 1; labels carry the extractor and configuration versions.", ("extractors", "config"))
UP_SINCE = G("tia_process_start_time_seconds", "Unix time this process started.")
UP_SINCE.set(START)


def timed(hist: Metric, **labels):
    """Context manager that observes the elapsed seconds into a histogram."""
    class _T:
        def __enter__(self):
            self.t = time.perf_counter()
            return self

        def __exit__(self, *a):
            (hist.labels(**labels) if labels else hist.labels()).observe(time.perf_counter() - self.t)
            return False
    return _T()
