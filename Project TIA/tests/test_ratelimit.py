"""SECURITY T17: rate limiting per principal, per tenant and per unauthenticated client address; 429 with Retry-After; health never limited."""
import pytest
from fastapi.testclient import TestClient

from test_auth import bearer, make_auth_app, token_for
from tia.ratelimit import ANALYSIS_COST, Limits, RateLimiter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_token_buckets_refill_and_isolate_callers():
    clk = Clock()
    rl = RateLimiter(Limits(per_minute=6, anon_per_minute=3, tenant_per_minute=10), clock=clk)
    assert [rl.check("/api/v1/repositories", "GET", "t1", "alice", "1.1.1.1") for _ in range(6)] == [0.0] * 6
    wait = rl.check("/api/v1/repositories", "GET", "t1", "alice", "1.1.1.1")
    assert 9.9 < wait <= 10.0                                        # one token every 10 s at 6/min
    assert rl.check("/api/v1/repositories", "GET", "t1", "bob", "1.1.1.1") == 0.0      # another principal has its own bucket
    clk.t += 10
    assert rl.check("/api/v1/repositories", "GET", "t1", "alice", "1.1.1.1") == 0.0
    # the tenant bucket caps everyone in it together
    rl2 = RateLimiter(Limits(per_minute=100, tenant_per_minute=5), clock=clk)
    assert all(rl2.check("/x", "GET", "t2", f"u{i}", "c") == 0.0 for i in range(5))
    assert rl2.check("/x", "GET", "t2", "u9", "c") > 0 and rl2.check("/x", "GET", "t3", "u9", "c") == 0.0
    # anonymous callers by address; health is never limited
    assert [rl.check("/auth/login", "GET", None, None, "9.9.9.9") for _ in range(3)] == [0.0] * 3
    assert rl.check("/auth/login", "GET", None, None, "9.9.9.9") > 0 and rl.check("/auth/login", "GET", None, None, "8.8.8.8") == 0.0
    assert all(rl.check("/healthz", "GET", None, None, "9.9.9.9") == 0.0 for _ in range(50))


def test_starting_analyses_costs_more():
    rl = RateLimiter(Limits(per_minute=ANALYSIS_COST * 2), clock=Clock())
    assert rl.check("/api/v1/impact-analyses", "POST", "t", "a", "c") == 0.0
    assert rl.check("/api/v1/impact-analyses", "POST", "t", "a", "c") == 0.0
    assert rl.check("/api/v1/impact-analyses", "POST", "t", "a", "c") > 0


def test_the_app_answers_429_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv("TIA_RATE_LIMIT_PER_MINUTE", "5")
    monkeypatch.setenv("TIA_RATE_LIMIT_ANON_PER_MINUTE", "4")
    app, tid = make_auth_app(tmp_path)
    c = TestClient(app)
    tok_a, tok_b = token_for(tid, ["viewer"]), token_for(tid, ["viewer"])
    codes = [c.get("/api/v1/repositories", headers=bearer(tok_a)).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    r = c.get("/api/v1/repositories", headers=bearer(tok_a))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["code"] == "rate_limited"
    assert c.get("/api/v1/repositories", headers=bearer(tok_b)).status_code == 200        # another token is not affected
    anon = [c.get("/api/v1/repositories").status_code for _ in range(6)]
    assert anon[:4] == [401] * 4 and 429 in anon[4:]                                     # unauthenticated callers are limited by address
    assert all(c.get("/healthz").status_code == 200 for _ in range(20))
    from tia import metrics
    assert "tia_rate_limited_total{caller=\"principal\"}" in metrics.REGISTRY.render()


def test_off_without_authentication_unless_asked(tmp_path, monkeypatch):
    from conftest import make_app
    monkeypatch.setenv("TIA_RATE_LIMIT_ANON_PER_MINUTE", "2")
    c = TestClient(make_app(tmp_path / "a"))
    assert all(c.get("/api/v1/repositories").status_code == 200 for _ in range(10))      # development mode
    monkeypatch.setenv("TIA_RATE_LIMIT", "true")
    c = TestClient(make_app(tmp_path / "b"))
    assert [c.get("/api/v1/repositories").status_code for _ in range(3)] == [200, 200, 429]
