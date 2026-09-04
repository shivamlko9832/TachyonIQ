"""Health check endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    """Liveness probe: process is up, and whether the database connection works."""
    orchestrator = request.app.state.orchestrator
    database_status = "connected"
    try:
        orchestrator.db_adapter.test_connection()
    except Exception:
        logger.warning("Health check: database connection failed.", exc_info=True)
        database_status = "error"

    return HealthResponse(status="ok", version=_VERSION, database=database_status)
