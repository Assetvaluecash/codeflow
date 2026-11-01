"""Structured logging (PL-007): one JSON object per line, with the request and trace it belongs to, and no secrets.

    {"ts": "...", "level": "INFO", "logger": "tia.http", "msg": "request", "request_id": "...", "trace_id": "...", "span_id": "...",
     "method": "GET", "route": "/api/v1/impact-analyses/{aid}", "status": 200, "duration_ms": 12.4}

Every string passes through the secret scrubber (generic tier included), so a token in an exception message cannot reach a log. Request
context rides in contextvars, so it follows the work across threadpool hops without being passed around. TIA_LOG_FORMAT=json (default
for the server) or text; TIA_LOG_LEVEL (default INFO).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from typing import Optional

from . import tracing
from .scrub import scrub_text

request_id_var: contextvars.ContextVar = contextvars.ContextVar("tia_request_id", default=None)
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


def new_request_id(incoming: Optional[str] = None) -> str:
    """A caller's X-Request-ID if it is safe to log and echo (short, no control characters or spaces), otherwise a fresh one."""
    return incoming if incoming and REQUEST_ID.match(incoming) else uuid.uuid4().hex


def _clean(v):
    if isinstance(v, str):
        return scrub_text(v, True)[0]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return scrub_text(str(v), True)[0]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        sp = tracing._current.get()
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
             "level": record.levelname, "logger": record.name, "msg": scrub_text(record.getMessage(), True)[0]}
        rid = request_id_var.get()
        if rid:
            d["request_id"] = rid
        if sp:
            d["trace_id"], d["span_id"] = sp.trace_id, sp.span_id
        for k, v in record.__dict__.items():
            if k not in RESERVED and not k.startswith("_"):
                d[k] = _clean(v)
        if record.exc_info:
            d["exception"] = scrub_text("".join(traceback.format_exception(*record.exc_info)), True)[0]
        return json.dumps(d, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get()
        msg = scrub_text(record.getMessage(), True)[0]
        extra = {k: _clean(v) for k, v in record.__dict__.items() if k not in RESERVED and not k.startswith("_")}
        base = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:5} {record.name}: {msg}"
        return base + (f" [{rid}]" if rid else "") + (f" {extra}" if extra else "")


def setup_logging(level: Optional[str] = None, fmt: Optional[str] = None, stream=None) -> logging.Logger:
    """Configure the 'tia' logger tree (and uvicorn's, so its lines are structured too). Idempotent."""
    level = (level or os.environ.get("TIA_LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.environ.get("TIA_LOG_FORMAT", "json")).lower()
    h = logging.StreamHandler(stream or sys.stderr)
    h.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    h._tia = True
    for name in ("tia", "uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = [x for x in lg.handlers if not getattr(x, "_tia", False)] + [h]
        lg.setLevel(level)
        lg.propagate = False
    logging.getLogger("uvicorn.access").disabled = True                     # replaced by the 'tia.http' request log (route template, no query)
    return logging.getLogger("tia")


http_log = logging.getLogger("tia.http")
log = logging.getLogger("tia")
