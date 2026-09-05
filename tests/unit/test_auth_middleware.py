"""
Tests for ConnectionPolicy and AuthMiddleware (uada/api/middleware/auth.py).

ConnectionPolicy tests are pure unit tests with no HTTP.
AuthMiddleware tests use a minimal FastAPI app + TestClient — no real DB,
no orchestrator, just the middleware itself.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from uada.api.middleware.auth import ConnectionPolicy
from uada.config import Settings

pytestmark = pytest.mark.unit


# ── Helpers ────────────────────────────────────────────────────────────────────

def _settings(**kwargs: Any) -> Settings:
    """Build a minimal Settings for auth tests; db_url is required by the model."""
    return Settings(db_url="sqlite:///:memory:", **kwargs)  # type: ignore[call-arg]


def _app_with_middleware(settings: Settings) -> FastAPI:
    """Minimal FastAPI app that has AuthMiddleware and one echo endpoint."""
    from uada.api.middleware.auth import AuthMiddleware

    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        return JSONResponse({
            "user_id": request.state.user_id,
            "role": request.state.role,
        })

    @app.post("/connections")
    async def create_conn(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


def _make_jwt(payload: dict, secret: str, algorithm: str = "HS256") -> str:
    jwt = pytest.importorskip("jwt", reason="PyJWT not installed")
    return jwt.encode(payload, secret, algorithm=algorithm)


# ── ConnectionPolicy ───────────────────────────────────────────────────────────

class TestConnectionPolicy:
    def test_admin_can_create_and_delete(self) -> None:
        p = ConnectionPolicy("alice", "admin")
        assert p.can_create_connection is True
        assert p.can_delete_connection is True

    def test_admin_can_query_and_see_sql(self) -> None:
        p = ConnectionPolicy("alice", "admin")
        assert p.can_query is True
        assert p.can_see_sql is True

    def test_analyst_cannot_create_or_delete_connections(self) -> None:
        p = ConnectionPolicy("bob", "analyst")
        assert p.can_create_connection is False
        assert p.can_delete_connection is False

    def test_analyst_can_query_see_sql_and_manage_schema(self) -> None:
        p = ConnectionPolicy("bob", "analyst")
        assert p.can_query is True
        assert p.can_see_sql is True
        assert p.can_manage_schema is True

    def test_viewer_can_only_query(self) -> None:
        p = ConnectionPolicy("carol", "viewer")
        assert p.can_query is True
        assert p.can_see_sql is False
        assert p.can_manage_schema is False
        assert p.can_create_connection is False
        assert p.can_delete_connection is False

    def test_unknown_role_defaults_to_viewer_permissions(self) -> None:
        p = ConnectionPolicy("dave", "superuser")
        # _rank defaults to 0 (viewer) for unknown roles
        assert p.can_query is True
        assert p.can_see_sql is False
        assert p.can_create_connection is False

    def test_to_dict_contains_expected_keys(self) -> None:
        p = ConnectionPolicy("alice", "admin")
        d = p.to_dict()
        assert set(d) == {
            "user_id", "role", "can_create_connection",
            "can_delete_connection", "can_query", "can_see_sql",
        }


# ── AuthMiddleware — API-key mode ──────────────────────────────────────────────

class TestApiKeyMode:
    @pytest.fixture
    def client(self) -> TestClient:
        settings = _settings(api_key="secret-key")  # type: ignore[call-arg]
        return TestClient(_app_with_middleware(settings), raise_server_exceptions=True)

    def test_valid_key_passes(self, client: TestClient) -> None:
        r = client.get("/probe", headers={"Authorization": "Bearer secret-key"})
        assert r.status_code == 200
        data = r.json()
        assert data["user_id"] == "api_key_caller"
        assert data["role"] == "admin"

    def test_missing_key_returns_401(self, client: TestClient) -> None:
        r = client.get("/probe")
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client: TestClient) -> None:
        r = client.get("/probe", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_public_path_bypasses_auth(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200


# ── AuthMiddleware — dev mode (no api_key, no RBAC) ───────────────────────────

class TestDevMode:
    @pytest.fixture
    def client(self) -> TestClient:
        settings = _settings()  # no api_key
        return TestClient(_app_with_middleware(settings))

    def test_no_auth_required_in_dev_mode(self, client: TestClient) -> None:
        r = client.get("/probe")
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "admin"
        assert data["user_id"] == "dev"


# ── AuthMiddleware — JWT / RBAC mode ──────────────────────────────────────────

JWT_SECRET = "test-jwt-secret"


class TestJwtRbacMode:
    @pytest.fixture
    def client(self) -> TestClient:
        settings = _settings(  # type: ignore[call-arg]
            rbac_enabled=True,
            jwt_secret_key=JWT_SECRET,
        )
        return TestClient(_app_with_middleware(settings), raise_server_exceptions=True)

    def test_valid_admin_jwt_passes(self, client: TestClient) -> None:
        token = _make_jwt({"sub": "alice", "role": "admin"}, JWT_SECRET)
        r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["user_id"] == "alice"
        assert data["role"] == "admin"

    def test_valid_viewer_jwt_passes(self, client: TestClient) -> None:
        token = _make_jwt({"sub": "carol", "role": "viewer"}, JWT_SECRET)
        r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["role"] == "viewer"

    def test_missing_bearer_returns_401(self, client: TestClient) -> None:
        r = client.get("/probe")
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/probe", headers={"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401

    def test_wrong_secret_returns_401(self, client: TestClient) -> None:
        token = _make_jwt({"sub": "eve", "role": "admin"}, "wrong-secret")
        r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_expired_token_returns_401(self, client: TestClient) -> None:
        token = _make_jwt(
            {"sub": "old", "role": "admin", "exp": int(time.time()) - 3600},
            JWT_SECRET,
        )
        r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_unknown_role_defaults_to_viewer(self, client: TestClient) -> None:
        token = _make_jwt({"sub": "frank", "role": "superuser"}, JWT_SECRET)
        r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["role"] == "viewer"

    def test_viewer_cannot_post_to_connections(self, client: TestClient) -> None:
        token = _make_jwt({"sub": "carol", "role": "viewer"}, JWT_SECRET)
        r = client.post(
            "/connections",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert r.status_code == 403

    def test_admin_can_post_to_connections(self, client: TestClient) -> None:
        token = _make_jwt({"sub": "alice", "role": "admin"}, JWT_SECRET)
        r = client.post(
            "/connections",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        # The endpoint itself returns 200 OK (no real logic in the stub)
        assert r.status_code == 200

    def test_public_path_bypasses_jwt(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
