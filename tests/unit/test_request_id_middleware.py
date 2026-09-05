"""
Tests for uada.api.middleware.request_id (P4-C-1).
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from uada.api.middleware.request_id import (
    RequestIdMiddleware,
    RequestIdFilter,
    get_request_id,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    async def ping(request: Request) -> dict:
        return {"request_id": request.state.request_id}

    return app


_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRequestIdMiddleware:

    def test_generates_uuid_when_no_header(self):
        client = TestClient(_make_app())
        r = client.get("/ping")
        assert r.status_code == 200
        rid = r.json()["request_id"]
        assert _UUID4_RE.match(rid), f"Expected UUID4, got {rid!r}"

    def test_returns_id_in_response_header(self):
        client = TestClient(_make_app())
        r = client.get("/ping")
        assert "X-Request-ID" in r.headers
        assert _UUID4_RE.match(r.headers["X-Request-ID"])

    def test_uses_supplied_valid_header(self):
        client = TestClient(_make_app())
        my_id = "my-request-12345"
        r = client.get("/ping", headers={"X-Request-ID": my_id})
        assert r.json()["request_id"] == my_id
        assert r.headers["X-Request-ID"] == my_id

    def test_rejects_invalid_header_and_generates_new(self):
        """Header with invalid chars must be replaced, not propagated."""
        client = TestClient(_make_app())
        bad = "../../etc/passwd"
        r = client.get("/ping", headers={"X-Request-ID": bad})
        rid = r.json()["request_id"]
        assert rid != bad
        assert _UUID4_RE.match(rid), f"Should have generated a fresh UUID, got {rid!r}"

    def test_rejects_overlong_header(self):
        """Header longer than 64 chars must be replaced."""
        client = TestClient(_make_app())
        long_id = "a" * 65
        r = client.get("/ping", headers={"X-Request-ID": long_id})
        rid = r.json()["request_id"]
        assert rid != long_id

    def test_state_matches_response_header(self):
        """request.state.request_id must equal the X-Request-ID response header."""
        client = TestClient(_make_app())
        r = client.get("/ping")
        assert r.json()["request_id"] == r.headers["X-Request-ID"]

    def test_different_requests_get_different_ids(self):
        client = TestClient(_make_app())
        ids = {client.get("/ping").json()["request_id"] for _ in range(5)}
        assert len(ids) == 5, "Each request should get a unique ID"


class TestGetRequestId:

    def test_default_outside_request_context(self):
        """get_request_id() returns '-' when called outside a request."""
        assert get_request_id() == "-"


class TestRequestIdFilter:

    def test_filter_injects_request_id_attribute(self):
        import logging
        f = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        f.filter(record)
        assert hasattr(record, "request_id")
        # Outside a request context it should be "-"
        assert record.request_id == "-"
