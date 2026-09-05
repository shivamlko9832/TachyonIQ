"""
Request ID Middleware (P4-C-1)
================================
Generates and propagates a unique ``X-Request-ID`` header for every
request.  The ID is:

  - Taken from the incoming ``X-Request-ID`` header if supplied (e.g. by
    a reverse proxy that already set it).
  - Generated as a fresh UUID4 otherwise.

The ID is stored in:
  - ``request.state.request_id``   — for route handlers and other
    middleware to read.
  - A module-level ``ContextVar``   — so logging filters and any
    code in the same async context can include it without threading
    the value explicitly through every call site.
  - The outgoing ``X-Request-ID`` response header.

Usage in application code::

    from uada.api.middleware.request_id import get_request_id

    logger.info("Processing query", extra={"request_id": get_request_id()})

Usage as a logging filter::

    import logging
    from uada.api.middleware.request_id import RequestIdFilter
    handler.addFilter(RequestIdFilter())
    # Then use %(request_id)s in your format string.

Security note
-------------
Incoming ``X-Request-ID`` values are sanitised: only the first 64
alphanumeric / hyphen / underscore characters are kept.  Anything else is
replaced with a fresh UUID so the header cannot be used as an injection
vector in structured logs.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Module-level ContextVar — readable anywhere in the same async context.
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9\-_]{1,64}$")


def get_request_id() -> str:
    """Return the current request ID, or ``'-'`` outside a request context."""
    return _request_id_var.get()


class RequestIdFilter(logging.Filter):
    """
    Logging filter that injects the current request ID as ``request_id``
    into every ``LogRecord``.

    Add to any handler whose format string references ``%(request_id)s``::

        handler.addFilter(RequestIdFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()  # type: ignore[attr-defined]
        return True


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Starlette/FastAPI middleware that ensures every request carries a
    validated ``X-Request-ID`` and exposes it via ``request.state`` and
    the module-level ``ContextVar``.
    """

    def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self._header = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw = request.headers.get(self._header, "")
        if raw and _SAFE_ID_RE.match(raw):
            request_id = raw
        else:
            request_id = str(uuid.uuid4())

        # Make the ID available via ContextVar (logging) and request.state.
        token = _request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)

        response.headers[self._header] = request_id
        return response
