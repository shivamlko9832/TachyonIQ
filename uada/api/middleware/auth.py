"""
Auth Middleware (P2-6 RBAC extension)
=======================================
Two-tier authentication:

  Tier 1 — API key (existing):
    Authorization: Bearer {api_key}
    Enabled when settings.api_key is set. Disabled in local dev (api_key=None).

  Tier 2 — JWT RBAC (P2-6):
    Authorization: Bearer {jwt_token}
    Enabled when settings.rbac_enabled is True AND settings.jwt_secret_key is set.
    Decodes the token → {user_id, role} and attaches them to request.state.
    Falls back to API-key mode when RBAC is disabled.

Roles and ConnectionPolicy:
    admin    — full access to all endpoints and all connections
    analyst  — read + query; cannot create/delete connections
    viewer   — query only; cannot modify anything; cannot list raw SQL

Request state attributes set by this middleware:
    request.state.user_id  : str   (from JWT 'sub' claim, or "anonymous")
    request.state.role     : str   (from JWT 'role' claim; default "viewer")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from uada.config import Settings

logger = logging.getLogger(__name__)

# Endpoints that are always public (health checks, UI assets)
_PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/", "/favicon.ico"})

# Role hierarchy — higher index = broader access
_ROLE_RANK: dict[str, int] = {"viewer": 0, "analyst": 1, "admin": 2}

# Methods that require at least analyst rank
_ANALYST_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Paths that mutate connections (require admin)
_ADMIN_PATHS = frozenset({"/connections"})  # prefix match


class ConnectionPolicy:
    """Defines what a given role may do with connections and queries."""

    def __init__(self, user_id: str, role: str) -> None:
        self.user_id = user_id
        self.role = role
        self._rank = _ROLE_RANK.get(role, 0)

    # -- Permission predicates -------------------------------------------------

    @property
    def can_create_connection(self) -> bool:
        return self._rank >= _ROLE_RANK["admin"]

    @property
    def can_delete_connection(self) -> bool:
        return self._rank >= _ROLE_RANK["admin"]

    @property
    def can_query(self) -> bool:
        return self._rank >= _ROLE_RANK["viewer"]

    @property
    def can_see_sql(self) -> bool:
        """Viewers should not see the generated SQL in responses."""
        return self._rank >= _ROLE_RANK["analyst"]

    @property
    def can_manage_schema(self) -> bool:
        return self._rank >= _ROLE_RANK["analyst"]

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "role": self.role,
            "can_create_connection": self.can_create_connection,
            "can_delete_connection": self.can_delete_connection,
            "can_query": self.can_query,
            "can_see_sql": self.can_see_sql,
        }


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Unified auth middleware: API-key OR JWT depending on settings.
    Always sets request.state.user_id and request.state.role.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._api_key = settings.api_key.get_secret_value() if settings.api_key else None
        self._rbac_enabled = settings.rbac_enabled
        self._jwt_secret = (
            settings.jwt_secret_key.get_secret_value()
            if settings.jwt_secret_key is not None
            else None
        )
        self._jwt_algorithm = settings.jwt_algorithm

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Always initialise defaults so downstream code can read unconditionally
        request.state.user_id = "anonymous"
        request.state.role = "viewer"
        request.state.connection_policy = ConnectionPolicy("anonymous", "viewer")

        # Liveness and static UI paths stay reachable by load balancers before
        # application credentials are available. Query and mutation routes
        # remain protected by the configured API key or JWT policy.
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        if self._rbac_enabled and self._jwt_secret:
            return await self._jwt_dispatch(request, call_next)
        elif self._rbac_enabled and not self._jwt_secret:
            # A deployment that explicitly enables RBAC without a verifier
            # must fail closed.  Falling through to dev-admin mode would turn
            # a configuration mistake into an authentication bypass.
            return JSONResponse(
                status_code=503,
                content={"detail": "RBAC is enabled but JWT verification is not configured."},
            )
        elif self._api_key:
            return await self._api_key_dispatch(request, call_next)
        else:
            # Auth disabled (local dev)
            request.state.user_id = "dev"
            request.state.role = "admin"
            request.state.connection_policy = ConnectionPolicy("dev", "admin")
            return await call_next(request)

    # ── API-key mode ──────────────────────────────────────────────────────────

    async def _api_key_dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        expected = f"Bearer {self._api_key}"
        if request.headers.get("Authorization") != expected:
            logger.warning(
                "Rejected request to '%s': missing or invalid API key.", request.url.path
            )
            return JSONResponse(
                status_code=401, content={"detail": "Invalid or missing API key."}
            )
        # API-key callers get admin access
        request.state.user_id = "api_key_caller"
        request.state.role = "admin"
        request.state.connection_policy = ConnectionPolicy("api_key_caller", "admin")
        return await call_next(request)

    # ── JWT / RBAC mode ───────────────────────────────────────────────────────

    async def _jwt_dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authorization header missing or not Bearer."},
            )
        token = auth_header[len("Bearer "):]
        payload = self._decode_jwt(token)
        if payload is None:
            return JSONResponse(
                status_code=401, content={"detail": "Invalid or expired JWT."}
            )

        user_id = payload.get("sub")
        if not isinstance(user_id, str) or not user_id.strip():
            # A token without a stable subject cannot be associated with an
            # owner, audit trail, or tenant. Reject it rather than silently
            # assigning the request to an anonymous identity.
            return JSONResponse(
                status_code=401,
                content={"detail": "JWT subject (sub) is required."},
            )
        user_id = user_id.strip()
        role: str = payload.get("role", "viewer")
        if role not in _ROLE_RANK:
            logger.warning("JWT for user '%s' has unknown role '%s'; defaulting to viewer.", user_id, role)
            role = "viewer"

        policy = ConnectionPolicy(user_id, role)
        request.state.user_id = user_id
        request.state.role = role
        request.state.connection_policy = policy

        # Enforce coarse-grained policy at middleware level
        denied = self._check_policy(request, policy)
        if denied:
            return denied

        return await call_next(request)

    def _decode_jwt(self, token: str) -> dict | None:
        try:
            import jwt  # PyJWT — lazy import; not always installed
            payload = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=[self._jwt_algorithm],
            )
            return payload  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            logger.debug("JWT decode failed: %s", exc)
            return None

    def _check_policy(self, request: Request, policy: ConnectionPolicy) -> JSONResponse | None:
        path = request.url.path
        method = request.method.upper()

        # Admin-only: create/delete connections
        if any(path.startswith(p) for p in _ADMIN_PATHS) and method in _ANALYST_METHODS:
            if not policy.can_create_connection:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Role '{policy.role}' cannot modify connections."},
                )

        # Analysts may save and remove analyses; viewers can only read/query.
        if path.startswith("/analyses") and method in {"POST", "DELETE", "PUT", "PATCH"}:
            if policy._rank < _ROLE_RANK["analyst"]:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Role '{policy.role}' cannot modify saved analyses."},
                )

        if path.startswith("/session/") and method == "DELETE":
            if policy._rank < _ROLE_RANK["analyst"]:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Role '{policy.role}' cannot delete sessions."},
                )

        return None
