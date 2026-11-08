"""Request rate limiting (SECURITY T17, PRD 11 SaaS operations).

Token buckets per caller, held in each server process:
  * an authenticated principal (person or service token) of a tenant: TIA_RATE_LIMIT_PER_MINUTE (default 600) requests per minute, bursting to
    the same number; analysis-starting and evidence-writing calls cost ANALYSIS_COST tokens, so a client cannot queue unbounded work;
  * an unauthenticated caller (sign-in pages, signed webhooks, the shared report page without a session), by client address:
    TIA_RATE_LIMIT_ANON_PER_MINUTE (default 120);
  * a whole tenant: TIA_RATE_LIMIT_TENANT_PER_MINUTE (default 6000), so one organisation cannot starve the others on a shared server.
On by default whenever authentication is required (production); TIA_RATE_LIMIT=true/false overrides. Health, readiness and metrics are
never limited. A refused request gets 429 with Retry-After and is counted in metrics; nothing is queued.
With several API replicas the limits apply per replica: divide them by the replica count (TIA_RATE_LIMIT_REPLICAS) or enforce the global
limit at the gateway as well. The client address is the socket peer; X-Forwarded-For is used only with TIA_TRUST_PROXY=true (behind the
gateway), and then only its last hop added by that proxy.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Optional

EXEMPT = ("/healthz", "/readyz", "/metrics", "/api/v1/health")
ANALYSIS_COST = 10
MAX_KEYS = 50_000


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


class Limits:
    def __init__(self, per_minute: Optional[int] = None, anon_per_minute: Optional[int] = None, tenant_per_minute: Optional[int] = None,
                 default_on: bool = True):
        replicas = _env_int("TIA_RATE_LIMIT_REPLICAS", 1)
        self.principal = (per_minute or _env_int("TIA_RATE_LIMIT_PER_MINUTE", 600)) / replicas
        self.anon = (anon_per_minute or _env_int("TIA_RATE_LIMIT_ANON_PER_MINUTE", 120)) / replicas
        self.tenant = (tenant_per_minute or _env_int("TIA_RATE_LIMIT_TENANT_PER_MINUTE", 6000)) / replicas
        flag = os.environ.get("TIA_RATE_LIMIT")
        self.enabled = default_on if flag is None else flag.lower() not in ("0", "false", "no", "off")


class RateLimiter:
    def __init__(self, limits: Optional[Limits] = None, clock=time.monotonic):
        self.limits = limits or Limits()
        self.clock = clock
        self._buckets: "OrderedDict[str, list]" = OrderedDict()           # key -> [tokens, last refill]
        self._lock = threading.Lock()

    def _take(self, key: str, rate_per_min: float, cost: float) -> float:
        """Takes `cost` tokens; returns 0 when allowed, else the seconds until enough tokens are back."""
        now = self.clock()
        b = self._buckets.get(key)
        if b is None:
            b = [rate_per_min, now]
            self._buckets[key] = b
            if len(self._buckets) > MAX_KEYS:
                self._buckets.popitem(last=False)                       # forget the least recently used caller
        else:
            self._buckets.move_to_end(key)
            b[0] = min(rate_per_min, b[0] + (now - b[1]) * rate_per_min / 60.0)
            b[1] = now
        if b[0] >= cost:
            b[0] -= cost
            return 0.0
        return (cost - b[0]) * 60.0 / rate_per_min

    def check(self, path: str, method: str, tenant: Optional[str], principal: Optional[str], client: str) -> float:
        """0 when the request may proceed, else the Retry-After seconds."""
        if not self.limits.enabled or path in EXEMPT:
            return 0.0
        heavy = method == "POST" and (path == "/api/v1/impact-analyses" or path.startswith(("/api/v1/evidence/", "/api/v1/releases"))
                                      or path.endswith("/reproduce"))
        cost = ANALYSIS_COST if heavy else 1
        with self._lock:
            if principal:
                wait = self._take(f"p:{tenant}:{principal}", self.limits.principal, cost)
                if wait:
                    return wait
                return self._take(f"t:{tenant}", self.limits.tenant, cost)
            return self._take(f"a:{client}", self.limits.anon, cost)


    def failed_auth_blocked(self, client: str) -> float:
        """Before authentication: the seconds an address must wait because its failed sign-ins used up its budget (0 = may try)."""
        if not self.limits.enabled:
            return 0.0
        with self._lock:
            b = self._buckets.get(f"f:{client}")
            if b is None:
                return 0.0
            now = self.clock()
            tokens = min(self.limits.anon, b[0] + (now - b[1]) * self.limits.anon / 60.0)
            return 0.0 if tokens >= 1 else (1 - tokens) * 60.0 / self.limits.anon

    def charge_failed_auth(self, client: str) -> None:
        """A request refused for its credential (401) costs its address one token: guessing tokens is limited too."""
        if self.limits.enabled:
            with self._lock:
                self._take(f"f:{client}", self.limits.anon, 1)


def client_address(request) -> str:
    peer = request.client.host if request.client else "unknown"
    if os.environ.get("TIA_TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        fwd = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
        if fwd:
            return fwd[-1]
    return peer
