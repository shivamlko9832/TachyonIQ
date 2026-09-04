"""
Auth Middleware
================
Bearer-token authentication for the API. Disabled entirely when
`settings.api_key` is None (local dev, per config.py's own docstring:
"If None, auth is disabled").
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


class AuthMiddleware(BaseHTTPMiddleware):
    """Requires `Authorization: Bearer {api_key}` on every request when configured."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._api_key = settings.api_key.get_secret_value() if settings.api_key else None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._api_key is None:
            return await call_next(request)

        expected = f"Bearer {self._api_key}"
        if request.headers.get("Authorization") != expected:
            logger.warning(
                "Rejected request to '%s': missing or invalid API key.", request.url.path
            )
            return JSONResponse(
                status_code=401, content={"detail": "Invalid or missing API key."}
            )
        return await call_next(request)
