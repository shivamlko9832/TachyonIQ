"""
Tests for GET /health and GET /ready (P4-C-3).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uada.api.routes.health import router as health_router


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_app(db_ok: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)

    mock_orchestrator = MagicMock()
    if db_ok:
        mock_orchestrator.db_adapter.test_connection.return_value = None
    else:
        mock_orchestrator.db_adapter.test_connection.side_effect = RuntimeError("DB down")

    mock_conn_mgr = MagicMock()
    mock_conn_mgr._connections = {}

    app.state.orchestrator = mock_orchestrator
    app.state.connection_manager = mock_conn_mgr
    return app


# ── /health tests ─────────────────────────────────────────────────────────────

class TestHealthEndpoint:

    def test_returns_200(self):
        client = TestClient(_make_app())
        assert client.get("/health").status_code == 200

    def test_status_ok(self):
        r = TestClient(_make_app()).get("/health")
        assert r.json()["status"] == "ok"

    def test_version_present(self):
        r = TestClient(_make_app()).get("/health")
        assert "version" in r.json()

    def test_uptime_seconds_present_and_positive(self):
        r = TestClient(_make_app()).get("/health")
        assert r.json()["uptime_seconds"] >= 0

    def test_db_down_still_returns_200(self):
        """Liveness probe must not block even when DB is unreachable."""
        client = TestClient(_make_app(db_ok=False))
        assert client.get("/health").status_code == 200

    def test_request_id_included_when_middleware_sets_it(self):
        """When RequestIdMiddleware is active, request_id appears in response."""
        from uada.api.middleware.request_id import RequestIdMiddleware
        app = _make_app()
        app.add_middleware(RequestIdMiddleware)
        client = TestClient(app)
        r = client.get("/health")
        assert r.json()["request_id"] is not None


# ── /ready tests ──────────────────────────────────────────────────────────────

class TestReadinessEndpoint:

    def test_returns_200_when_all_ok(self):
        client = TestClient(_make_app(db_ok=True))
        assert client.get("/ready").status_code == 200

    def test_status_ready_when_all_ok(self):
        r = TestClient(_make_app(db_ok=True)).get("/ready")
        assert r.json()["status"] == "ready"

    def test_returns_503_when_db_down(self):
        client = TestClient(_make_app(db_ok=False), raise_server_exceptions=False)
        assert client.get("/ready").status_code == 503

    def test_status_unavailable_when_db_down(self):
        client = TestClient(_make_app(db_ok=False), raise_server_exceptions=False)
        r = client.get("/ready")
        assert r.json()["status"] == "unavailable"

    def test_checks_list_present(self):
        r = TestClient(_make_app()).get("/ready")
        body = r.json()
        assert "checks" in body
        assert isinstance(body["checks"], list)
        assert len(body["checks"]) >= 1

    def test_orchestrator_db_check_ok(self):
        r = TestClient(_make_app(db_ok=True)).get("/ready")
        checks = {c["name"]: c for c in r.json()["checks"]}
        assert "orchestrator_db" in checks
        assert checks["orchestrator_db"]["status"] == "ok"

    def test_orchestrator_db_check_error(self):
        client = TestClient(_make_app(db_ok=False), raise_server_exceptions=False)
        r = client.get("/ready")
        checks = {c["name"]: c for c in r.json()["checks"]}
        assert "orchestrator_db" in checks
        assert checks["orchestrator_db"]["status"] == "error"

    def test_latency_ms_present_on_ok(self):
        r = TestClient(_make_app(db_ok=True)).get("/ready")
        checks = {c["name"]: c for c in r.json()["checks"]}
        assert checks["orchestrator_db"]["latency_ms"] is not None

    def test_uptime_seconds_present(self):
        r = TestClient(_make_app()).get("/ready")
        assert r.json()["uptime_seconds"] >= 0

    def test_connection_manager_check_present(self):
        r = TestClient(_make_app()).get("/ready")
        check_names = [c["name"] for c in r.json()["checks"]]
        assert "connection_manager" in check_names


# ── Query input validation tests ──────────────────────────────────────────────

class TestQueryRequestValidation:

    def test_prompt_max_length_enforced(self):
        """Questions longer than prompt_max_length must be rejected with 422."""
        from uada.api.routes.query import QueryRequest
        from pydantic import ValidationError
        import uada.api.routes.query as qmod

        original = qmod._get_max_length
        qmod._get_max_length = lambda: 10  # type: ignore[method-assign]
        try:
            with pytest.raises(ValidationError):
                QueryRequest(question="A" * 11)
        finally:
            qmod._get_max_length = original

    def test_whitespace_only_question_rejected(self):
        from uada.api.routes.query import QueryRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            QueryRequest(question="   ")

    def test_valid_question_accepted(self):
        from uada.api.routes.query import QueryRequest
        q = QueryRequest(question="How many orders last month?")
        assert q.question == "How many orders last month?"

    def test_question_stripped(self):
        from uada.api.routes.query import QueryRequest
        q = QueryRequest(question="  hello  ")
        assert q.question == "hello"

    def test_connection_id_field_accepted(self):
        from uada.api.routes.query import QueryRequest
        q = QueryRequest(question="test", connection_id="conn-123")
        assert q.connection_id == "conn-123"

    def test_connection_id_defaults_none(self):
        from uada.api.routes.query import QueryRequest
        q = QueryRequest(question="test")
        assert q.connection_id is None
