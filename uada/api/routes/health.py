"""
Health and readiness endpoints (P4-C-3)
=========================================

GET /health  — Liveness probe
  Lightweight: always returns HTTP 200 while the process is alive.
  Reports whether the application orchestrator has been initialised without
  performing database I/O, so a transient database outage does not cause
  container restarts.
  Safe to call at high frequency from a load balancer.

GET /ready   — Readiness probe
  Heavyweight: actively tests the database connection and only returns
  HTTP 200 when all critical dependencies are reachable.
  Returns HTTP 503 when any dependency is unhealthy so that the
  orchestrator (Kubernetes, ECS, …) stops routing traffic here.
  Call less frequently (e.g. every 10 s from a readiness probe).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

_VERSION = "0.1.0"
_START_TIME = time.monotonic()


# ── Response models ─────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    request_id: str | None = None
    database: str = "unknown"


class ReadinessCheck(BaseModel):
    name: str
    status: str          # "ok" | "error"
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: str          # "ready" | "degraded" | "unavailable"
    version: str
    uptime_seconds: float
    checks: list[ReadinessCheck]


# ── Liveness ────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    """
    Liveness probe — fast path, no I/O.

    Always returns HTTP 200 while the process is running.
    """
    request_id: str | None = getattr(request.state, "request_id", None)
    return HealthResponse(
        status="ok",
        version=_VERSION,
        uptime_seconds=round(time.monotonic() - _START_TIME, 1),
        request_id=request_id,
        database="connected" if getattr(request.app.state, "orchestrator", None) is not None else "unknown",
    )


# ── Readiness ───────────────────────────────────────────────────────────────

@router.get("/ready", response_model=ReadinessResponse)
async def get_ready(request: Request, response: Response) -> ReadinessResponse:
    """
    Readiness probe — actively checks all critical dependencies.

    Returns HTTP 200 only when every check passes.
    Returns HTTP 503 when any critical dependency is unhealthy.
    """
    checks: list[ReadinessCheck] = []
    all_ok = True

    # ── 1. Orchestrator DB adapter ───────────────────────────────────────────
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is not None:
        db_check = _check_orchestrator_db(orchestrator)
        checks.append(db_check)
        if db_check.status != "ok":
            all_ok = False
    else:
        checks.append(ReadinessCheck(name="orchestrator_db", status="error", detail="Orchestrator not initialised"))
        all_ok = False

    # ── 2. Connection manager (multi-tenant DB connections) ──────────────────
    conn_mgr = getattr(request.app.state, "connection_manager", None)
    if conn_mgr is not None:
        cm_check = _check_connection_manager(conn_mgr)
        checks.append(cm_check)
        # Connection manager failures are non-critical (no connections yet = ok)

    uptime = round(time.monotonic() - _START_TIME, 1)

    if all_ok:
        overall = "ready"
    else:
        overall = "unavailable"
        response.status_code = 503

    return ReadinessResponse(
        status=overall,
        version=_VERSION,
        uptime_seconds=uptime,
        checks=checks,
    )


# ── Dependency checkers ──────────────────────────────────────────────────────

def _check_orchestrator_db(orchestrator: Any) -> ReadinessCheck:
    t0 = time.monotonic()
    try:
        orchestrator.db_adapter.test_connection()
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return ReadinessCheck(name="orchestrator_db", status="ok", latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.warning("Readiness check: orchestrator_db failed: %s", exc)
        return ReadinessCheck(
            name="orchestrator_db",
            status="error",
            latency_ms=latency_ms,
            detail="Database readiness check failed.",
        )


def _check_connection_manager(conn_mgr: Any) -> ReadinessCheck:
    try:
        count = len(getattr(conn_mgr, "_meta", {}))
        return ReadinessCheck(
            name="connection_manager",
            status="ok",
            detail=f"{count} connection(s) registered",
        )
    except Exception:
        return ReadinessCheck(
            name="connection_manager",
            status="error",
            detail="Connection manager check failed.",
        )
