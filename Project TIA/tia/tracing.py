"""Distributed tracing (PL-007): W3C Trace Context in and out, spans around the work that matters, optional OTLP/HTTP export.

A request's trace continues through the job queue into the worker, and out to GitHub / Vault, so one trace shows a pull request's
whole life. Spans go to the structured log (always) and, when TIA_OTLP_ENDPOINT is set, to an OpenTelemetry collector as OTLP/HTTP
JSON from a background thread: export never blocks work, drops when its bounded queue is full, and counts what it dropped.
Span attributes pass through the secret scrubber; no span carries a request body, a token or user content.
"""
from __future__ import annotations

import contextvars
import json
import os
import queue
import random
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Optional

from . import metrics
from .scrub import scrub_text

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
KIND = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
_current: contextvars.ContextVar = contextvars.ContextVar("tia_span", default=None)


def _hex(n: int) -> str:
    return "%0*x" % (n, random.getrandbits(n * 4))


def parse_traceparent(header: Optional[str]) -> Optional[dict]:
    """{'trace_id', 'span_id', 'sampled'} from a traceparent header, or None when absent or malformed (all-zero ids are invalid)."""
    m = TRACEPARENT.match((header or "").strip().lower())
    if not m or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None
    return {"trace_id": m.group(1), "span_id": m.group(2), "sampled": bool(int(m.group(3), 16) & 1)}


class Span:
    def __init__(self, name: str, kind: str, trace_id: str, parent_id: Optional[str], sampled: bool, attrs: dict):
        self.name, self.kind, self.trace_id, self.parent_id, self.sampled = name, kind, trace_id, parent_id, sampled
        self.span_id, self.attrs = _hex(16), dict(attrs)
        self.start_ns, self.end_ns, self.status, self.error = time.time_ns(), None, "ok", None

    def set(self, **attrs):
        self.attrs.update(attrs)

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "trace_id": self.trace_id, "span_id": self.span_id, "parent_span_id": self.parent_id,
                "start_ns": self.start_ns, "end_ns": self.end_ns, "duration_ms": round(((self.end_ns or time.time_ns()) - self.start_ns) / 1e6, 3),
                "status": self.status, "attributes": {k: scrub_text(str(v), True)[0] if isinstance(v, str) else v for k, v in self.attrs.items()},
                **({"error": scrub_text(self.error, True)[0]} if self.error else {})}


class OtlpExporter:
    """OTLP/HTTP JSON to a collector (POST <endpoint>/v1/traces), batched in a background thread."""

    def __init__(self, endpoint: str, headers: Optional[dict] = None, service_name: str = "tia", max_queue: int = 2048,
                 batch: int = 100, interval_s: float = 2.0, timeout_s: float = 5.0):
        from .transport import check_outbound
        self.url = check_outbound(endpoint.rstrip("/"), "TIA_OTLP_ENDPOINT") + "/v1/traces"
        self.headers, self.service, self.batch, self.interval, self.timeout = dict(headers or {}), service_name, batch, interval_s, timeout_s
        self.q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="tia-otlp")
        self.thread.start()

    def export(self, span: Span) -> None:
        try:
            self.q.put_nowait(span.to_dict())
        except queue.Full:
            metrics.TRACE_EXPORT.labels(outcome="dropped").inc()

    @staticmethod
    def _attr(k, v):
        if isinstance(v, bool):
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, float):
            val = {"doubleValue": v}
        else:
            val = {"stringValue": str(v)}
        return {"key": k, "value": val}

    def payload(self, spans: list) -> dict:
        return {"resourceSpans": [{
            "resource": {"attributes": [self._attr("service.name", self.service), self._attr("service.namespace", "tia")]},
            "scopeSpans": [{"scope": {"name": "tia"}, "spans": [{
                "traceId": s["trace_id"], "spanId": s["span_id"], **({"parentSpanId": s["parent_span_id"]} if s["parent_span_id"] else {}),
                "name": s["name"], "kind": KIND.get(s["kind"], 1), "startTimeUnixNano": str(s["start_ns"]), "endTimeUnixNano": str(s["end_ns"]),
                "attributes": [self._attr(k, v) for k, v in s["attributes"].items()],
                "status": {"code": 2 if s["status"] == "error" else 1, **({"message": s["error"]} if s.get("error") else {})}} for s in spans]}]}]}

    def flush(self) -> None:
        while True:
            spans = []
            try:
                while len(spans) < self.batch:
                    spans.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if not spans:
                return
            req = urllib.request.Request(self.url, method="POST", data=json.dumps(self.payload(spans)).encode(),
                                         headers={"Content-Type": "application/json", **self.headers})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    r.read()
                metrics.TRACE_EXPORT.labels(outcome="exported").inc(len(spans))
            except (urllib.error.URLError, OSError):
                metrics.TRACE_EXPORT.labels(outcome="failed").inc(len(spans))      # tracing is best effort: never retried into a backlog

    def _run(self):
        while not self._stop.wait(self.interval):
            self.flush()

    def shutdown(self):
        self._stop.set()
        self.flush()


class Tracer:
    def __init__(self, exporter: Optional[OtlpExporter] = None, sample_ratio: float = 1.0, logger=None):
        self.exporter, self.ratio, self.logger = exporter, max(0.0, min(1.0, sample_ratio)), logger
        self.recent: list = []                                           # the last spans, for tests and diagnostics
        self.keep = 200

    def current(self) -> Optional[Span]:
        return _current.get()

    @contextmanager
    def span(self, name: str, kind: str = "internal", parent: Optional[dict] = None, **attrs):
        """Run a block inside a span. parent: a parsed traceparent to continue; otherwise the current span, otherwise a new trace."""
        cur = _current.get()
        if parent:
            trace_id, parent_id, sampled = parent["trace_id"], parent["span_id"], parent["sampled"]
        elif cur:
            trace_id, parent_id, sampled = cur.trace_id, cur.span_id, cur.sampled
        else:
            trace_id, parent_id, sampled = _hex(32), None, random.random() < self.ratio
        sp = Span(name, kind, trace_id, parent_id, sampled, attrs)
        token = _current.set(sp)
        try:
            yield sp
        except BaseException as e:
            sp.status, sp.error = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            sp.end_ns = time.time_ns()
            _current.reset(token)
            self._finish(sp)

    def _finish(self, sp: Span) -> None:
        d = sp.to_dict()
        self.recent.append(d)
        del self.recent[:-self.keep]
        if self.logger is not None:
            self.logger.info("span", extra={"span": d})
        if sp.sampled and self.exporter:
            self.exporter.export(sp)


TRACER = Tracer()


def configure(exporter: Optional[OtlpExporter] = None, sample_ratio: float = 1.0, logger=None) -> Tracer:
    global TRACER
    TRACER = Tracer(exporter, sample_ratio, logger)
    return TRACER


def configure_from_env(logger=None) -> Tracer:
    from .credentials import env_secret
    endpoint = os.environ.get("TIA_OTLP_ENDPOINT")
    headers = {}
    raw = env_secret("TIA_OTLP_HEADERS")                                  # "Authorization=Bearer xyz,X-Tenant=1" (may be a secret reference)
    for item in (raw or "").split(","):
        if "=" in item:
            k, _, v = item.partition("=")
            headers[k.strip()] = v.strip()
    ex = OtlpExporter(endpoint, headers, os.environ.get("TIA_SERVICE_NAME", "tia")) if endpoint else None
    return configure(ex, float(os.environ.get("TIA_TRACE_SAMPLE", "1.0")), logger)


def span(name: str, kind: str = "internal", **attrs):
    return TRACER.span(name, kind, **attrs)


def current_traceparent() -> Optional[str]:
    s = _current.get()
    return s.traceparent if s else None


def propagation_headers() -> dict:
    """Headers that continue the current trace in an outbound call."""
    tp = current_traceparent()
    return {"traceparent": tp} if tp else {}
