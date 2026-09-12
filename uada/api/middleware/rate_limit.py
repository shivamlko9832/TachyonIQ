"""
Rate Limit Middleware (P4-C-2)
================================
Sliding-window rate limiter for the UADA FastAPI application.

Algorithm
---------
Per client key (default: remote IP), we maintain a ``collections.deque``
of monotonic timestamps.  On each request:

1. Prune entries older than the window (60 s by default).
2. If the remaining entry count >= capacity → reject with HTTP 429.
3. Otherwise append the current timestamp and allow the request.

This is an **in-process** store — suitable for single-process deployments
(one Uvicorn worker, or when using Gunicorn with ``--workers 1``).  For
multi-process production use, replace ``_SlidingWindow`` with a Redis-backed
equivalent (e.g. a Lua ZADD/ZCOUNT script) while keeping the same
middleware interface.

Configuration (via ``Settings``)
---------------------------------
  rate_limit_enabled: bool   — toggle; False → middleware is a no-op
  rate_limit_rpm:     int    — requests per minute per client (default 60)
  rate_limit_burst:   int    — extra allowance above rpm (default 10)

Exempt paths
------------
``/health`` and ``/ready`` are always exempt so that liveness / readiness
probes cannot exhaust the quota.

Security note
-------------
The client key uses ``X-Forwarded-For`` only when the explicit
``trust_proxy_headers`` setting is enabled; otherwise it uses the ASGI
``client`` tuple. This prevents direct clients from spoofing their quota key.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from time import monotonic
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from uada.config import Settings

logger = logging.getLogger(__name__)

# Paths that bypass rate limiting unconditionally.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/ready", "/metrics"})

_WINDOW_SECONDS: float = 60.0


class _SlidingWindow:
    """
    Thread-safe in-process sliding-window counter.

    ``capacity`` is the maximum number of requests allowed within the
    rolling ``window`` period.
    """

    def __init__(
        self,
        capacity: int,
        window: float = _WINDOW_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._capacity = capacity
        self._window = window
        self._clock = clock
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, key: str) -> tuple[bool, float]:
        """
        Check whether ``key`` is within quota and record the attempt.

        Returns:
            (allowed, retry_after_seconds) — when *allowed* is ``False``,
            ``retry_after_seconds`` is the number of seconds until the
            oldest entry expires (i.e. when capacity is next available).
        """
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            # Remove expired entries
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self._capacity:
                retry_after = math.ceil(self._window - (now - bucket[0]))
                return False, max(retry_after, 1)

            bucket.append(now)
            return True, 0.0

    # -- Maintenance -----------------------------------------------------------

    def evict_stale_keys(self) -> int:
        """Remove keys whose buckets are entirely expired. Returns evicted count."""
        now = self._clock()
        cutoff = now - self._window
        removed = 0
        with self._lock:
            stale = [k for k, dq in self._buckets.items() if not dq or dq[-1] < cutoff]
            for k in stale:
                del self._buckets[k]
                removed += 1
        return removed

    @property
    def active_keys(self) -> int:
        with self._lock:
            return len(self._buckets)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate-limit middleware.

    Disabled entirely when ``settings.rate_limit_enabled`` is ``False``
    (the default for local development), so there is zero overhead in dev.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._enabled = settings.rate_limit_enabled
        self._trust_proxy_headers = bool(getattr(settings, "trust_proxy_headers", False))
        capacity = settings.rate_limit_rpm + settings.rate_limit_burst
        self._window = _SlidingWindow(capacity=capacity)
        logger.debug(
            "RateLimitMiddleware initialised (enabled=%s, capacity=%d/60 s)",
            self._enabled,
            capacity,
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        key = self._client_key(request)
        allowed, retry_after = self._window.check_and_record(key)
        if not allowed:
            logger.warning(
                "Rate limit exceeded for client '%s' on path '%s'",
                key,
                request.url.path,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please slow down.",
                    "retry_after_seconds": int(retry_after),
                },
                headers={"Retry-After": str(int(retry_after))},
            )

        return await call_next(request)

    def _client_key(self, request: Request) -> str:
        """Derive a stable identifier for the client making this request."""
        forwarded = request.headers.get("X-Forwarded-For", "")
        if self._trust_proxy_headers and forwarded:
            # Use only the first (leftmost = client) address.
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"
