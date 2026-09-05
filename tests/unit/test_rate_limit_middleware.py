"""
Tests for uada.api.middleware.rate_limit (P4-C-2).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uada.api.middleware.rate_limit import RateLimitMiddleware, _SlidingWindow


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_settings(enabled: bool = True, rpm: int = 3, burst: int = 0) -> MagicMock:
    s = MagicMock()
    s.rate_limit_enabled = enabled
    s.rate_limit_rpm = rpm
    s.rate_limit_burst = burst
    return s


def _make_app(settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, settings=settings)

    @app.get("/data")
    async def data() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


# ── _SlidingWindow unit tests ─────────────────────────────────────────────────

class TestSlidingWindow:

    def test_allows_within_capacity(self):
        sw = _SlidingWindow(capacity=3)
        for _ in range(3):
            allowed, _ = sw.check_and_record("key")
            assert allowed

    def test_rejects_over_capacity(self):
        sw = _SlidingWindow(capacity=2)
        sw.check_and_record("key")
        sw.check_and_record("key")
        allowed, retry = sw.check_and_record("key")
        assert not allowed
        assert retry >= 1

    def test_independent_keys(self):
        sw = _SlidingWindow(capacity=1)
        allowed_a, _ = sw.check_and_record("a")
        allowed_b, _ = sw.check_and_record("b")
        assert allowed_a
        assert allowed_b

    def test_active_keys_count(self):
        sw = _SlidingWindow(capacity=5)
        sw.check_and_record("x")
        sw.check_and_record("y")
        assert sw.active_keys == 2

    def test_evict_stale_keys_removes_nothing_when_fresh(self):
        sw = _SlidingWindow(capacity=5, window=60.0)
        sw.check_and_record("fresh")
        removed = sw.evict_stale_keys()
        assert removed == 0  # not yet expired

    def test_evict_stale_keys_removes_expired(self):
        sw = _SlidingWindow(capacity=5, window=0.001)  # 1 ms window
        sw.check_and_record("old")
        import time; time.sleep(0.01)  # wait for expiry
        removed = sw.evict_stale_keys()
        assert removed == 1


# ── Middleware integration tests ───────────────────────────────────────────────

class TestRateLimitMiddleware:

    def test_disabled_never_blocks(self):
        settings = _make_settings(enabled=False, rpm=1, burst=0)
        client = TestClient(_make_app(settings))
        # 10 requests with capacity=1 — should all succeed when disabled
        for _ in range(10):
            assert client.get("/data").status_code == 200

    def test_blocks_after_capacity(self):
        settings = _make_settings(enabled=True, rpm=2, burst=0)
        client = TestClient(_make_app(settings))
        assert client.get("/data", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
        assert client.get("/data", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
        r = client.get("/data", headers={"X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 429

    def test_429_response_has_retry_after_header(self):
        settings = _make_settings(enabled=True, rpm=1, burst=0)
        client = TestClient(_make_app(settings))
        client.get("/data", headers={"X-Forwarded-For": "10.0.0.1"})
        r = client.get("/data", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) >= 1

    def test_429_body_has_detail_and_retry_after(self):
        settings = _make_settings(enabled=True, rpm=1, burst=0)
        client = TestClient(_make_app(settings))
        client.get("/data", headers={"X-Forwarded-For": "9.9.9.9"})
        r = client.get("/data", headers={"X-Forwarded-For": "9.9.9.9"})
        body = r.json()
        assert "detail" in body
        assert "retry_after_seconds" in body

    def test_exempt_paths_never_blocked(self):
        """Health / ready / metrics paths must never be rate-limited."""
        settings = _make_settings(enabled=True, rpm=1, burst=0)
        client = TestClient(_make_app(settings))
        for _ in range(5):
            r = client.get("/health", headers={"X-Forwarded-For": "5.5.5.5"})
            assert r.status_code == 200

    def test_burst_extends_capacity(self):
        settings = _make_settings(enabled=True, rpm=2, burst=3)  # total 5
        client = TestClient(_make_app(settings))
        for _ in range(5):
            assert client.get("/data", headers={"X-Forwarded-For": "7.7.7.7"}).status_code == 200
        r = client.get("/data", headers={"X-Forwarded-For": "7.7.7.7"})
        assert r.status_code == 429

    def test_different_clients_independent(self):
        settings = _make_settings(enabled=True, rpm=1, burst=0)
        client = TestClient(_make_app(settings))
        # Exhaust client A
        client.get("/data", headers={"X-Forwarded-For": "1.1.1.1"})
        # Client B should still be fine
        assert client.get("/data", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
