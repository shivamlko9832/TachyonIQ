"""
Connection Management Routes
==============================
REST endpoints for registering, testing, and managing database connections.
Credentials are NEVER returned in any response body.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr

router = APIRouter(prefix="/connections", tags=["connections"])
logger = logging.getLogger(__name__)


def _get_mgr(request: Request):
    mgr = getattr(request.app.state, "connection_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Connection manager not initialised.")
    return mgr


# ── Request / Response models ─────────────────────────────────────────────────

class ConnectionCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    dialect: str = Field(..., description="postgresql|mysql|sqlite|mssql|snowflake|bigquery|redshift|duckdb")
    host: str = Field(default="")
    port: int = Field(default=5432)
    database: str = Field(..., min_length=1)
    username: str = Field(default="")
    password: SecretStr = Field(default=SecretStr(""))
    ssl: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)
    test_on_create: bool = Field(default=True)


class ConnectionCreatedResponse(BaseModel):
    connection_id: str
    name: str
    dialect: str
    test_result: dict[str, Any] | None = None


class TestConnectionResponse(BaseModel):
    ok: bool
    latency_ms: float | None = None
    server_version: str | None = None
    error: str | None = None


class DiscoverResponse(BaseModel):
    table_count: int
    column_count: int
    relationship_count: int
    row_estimate: int


class ColumnRefreshSummary(BaseModel):
    """Per-column profiling result. Sample values are deliberately excluded."""
    column_name: str
    data_type: str
    null_pct: float | None = None
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    is_temporal: bool = False


class TableRefreshSummary(BaseModel):
    table_name: str
    row_count: int | None = None
    column_count: int = 0
    columns: list[ColumnRefreshSummary] = Field(default_factory=list)


class SchemaRefreshResponse(BaseModel):
    connection_id: str
    schema_fingerprint: str
    table_count: int
    column_count: int
    profiled_table_count: int
    profiled_column_count: int
    tables: list[TableRefreshSummary] = Field(default_factory=list)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", status_code=201, response_model=ConnectionCreatedResponse)
async def create_connection(body: ConnectionCreateRequest, mgr=Depends(_get_mgr)):
    """Register a new database connection. Password is accepted but NEVER returned."""
    from uada.db.connection_manager import ConnectionConfig

    config = ConnectionConfig(
        name=body.name,
        dialect=body.dialect,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        password=body.password,
        ssl=body.ssl,
        extra=body.extra,
    )
    connection_id = mgr.register(config)
    logger.info("Registered connection %s (dialect=%s)", connection_id, body.dialect)

    test_result = None
    if body.test_on_create:
        try:
            r = mgr.test_connection(connection_id)
            test_result = {"ok": r.ok, "latency_ms": r.latency_ms,
                           "server_version": r.server_version, "error": r.error}
        except Exception as exc:
            logger.warning("test_on_create failed for %s: %s", connection_id, exc)
            test_result = {"ok": False, "error": str(exc)}

    return ConnectionCreatedResponse(
        connection_id=connection_id, name=body.name,
        dialect=body.dialect, test_result=test_result,
    )


@router.get("")
async def list_connections(mgr=Depends(_get_mgr)) -> dict[str, Any]:
    """List all registered connections. Credentials are NEVER included."""
    summaries = mgr.list_connections()
    return {"connections": [s.model_dump() for s in summaries]}


@router.get("/{connection_id}")
async def get_connection(connection_id: str, mgr=Depends(_get_mgr)) -> dict[str, Any]:
    """Get a single connection summary. Credentials are NEVER included."""
    summary = mgr.get_summary(connection_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found.")
    return summary.model_dump()


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, mgr=Depends(_get_mgr)) -> None:
    """Remove a registered connection and close its pool."""
    try:
        mgr.delete_connection(connection_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found.")
    logger.info("Deleted connection %s", connection_id)


@router.post("/{connection_id}/test", response_model=TestConnectionResponse)
async def test_connection(connection_id: str, mgr=Depends(_get_mgr)) -> TestConnectionResponse:
    """Run SELECT 1 against the connection and return latency + server version."""
    if mgr.get_summary(connection_id) is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found.")
    try:
        r = mgr.test_connection(connection_id)
        return TestConnectionResponse(ok=r.ok, latency_ms=r.latency_ms,
                                      server_version=r.server_version, error=r.error)
    except Exception as exc:
        return TestConnectionResponse(ok=False, error=str(exc))


@router.post("/{connection_id}/discover", response_model=DiscoverResponse)
async def discover_schema(connection_id: str, mgr=Depends(_get_mgr)) -> DiscoverResponse:
    """Reflect the database schema: table/column/FK counts, row estimates."""
    if mgr.get_summary(connection_id) is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found.")
    try:
        r = mgr.discover_schema(connection_id)
        return DiscoverResponse(table_count=r.table_count, column_count=r.column_count,
                                relationship_count=r.relationship_count, row_estimate=r.row_estimate)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{connection_id}/profile")
async def profile_connection(connection_id: str, mgr=Depends(_get_mgr)) -> dict[str, Any]:
    """Deprecated stub — use /refresh instead."""
    summary = mgr.get_summary(connection_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found.")
    return {
        "connection_id": connection_id,
        "name": summary.name,
        "status": "deprecated",
        "message": "Use POST /connections/{id}/refresh for full schema profiling.",
    }


@router.post("/{connection_id}/refresh", response_model=SchemaRefreshResponse)
async def refresh_schema(connection_id: str, mgr=Depends(_get_mgr)) -> SchemaRefreshResponse:
    """
    Reflect the full schema and run per-column profiling (null%, distinct count,
    min/max, temporal range).  Sample values are computed internally but NEVER
    returned in the response or written to logs.
    """
    if mgr.get_summary(connection_id) is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found.")

    from uada.pipeline.data_profiler import DataProfiler

    try:
        adapter = mgr.get_adapter(connection_id)
        raw = adapter.get_raw_schema()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not connect: {exc}") from exc

    allowed_tables = frozenset(t.name for t in raw.tables)
    total_columns = sum(len(t.columns) for t in raw.tables)

    # Run profiling (best-effort; individual table failures are skipped)
    try:
        profile = DataProfiler().profile(adapter, allowed_tables)
    except Exception as exc:
        logger.warning("DataProfiler failed for %s: %s", connection_id, exc)
        profile = None

    # Build per-table summaries from the schema + profile
    table_summaries: list[TableRefreshSummary] = []
    profiled_cols = 0
    for tinfo in raw.tables:
        tname = tinfo.name
        table_profile = profile.tables.get(tname) if profile else None
        col_summaries: list[ColumnRefreshSummary] = []
        for col in tinfo.columns:
            cp = (
                table_profile
                and next((c for c in table_profile.columns if c.column_name == col.name), None)
            )
            if cp:
                profiled_cols += 1
            col_summaries.append(ColumnRefreshSummary(
                column_name=col.name,
                data_type=col.data_type,
                null_pct=cp.null_pct if cp else None,
                distinct_count=cp.distinct_count if cp else None,
                min_value=cp.min_value if cp else None,
                max_value=cp.max_value if cp else None,
                is_temporal=cp.is_temporal if cp else False,
            ))
        table_summaries.append(TableRefreshSummary(
            table_name=tname,
            row_count=table_profile.row_count if table_profile else None,
            column_count=len(col_summaries),
            columns=col_summaries,
        ))

    # Persist updated table count
    try:
        mgr._meta[connection_id]["table_count"] = len(raw.tables)
        mgr._meta[connection_id]["status"] = "connected"
        mgr._save_registry()
    except Exception:  # noqa: BLE001
        pass

    profiled_table_count = len(profile.tables) if profile else 0
    logger.info(
        "Schema refresh for %s: %d tables, %d profiled columns",
        connection_id, len(raw.tables), profiled_cols,
    )
    return SchemaRefreshResponse(
        connection_id=connection_id,
        schema_fingerprint=raw.schema_fingerprint,
        table_count=len(raw.tables),
        column_count=total_columns,
        profiled_table_count=profiled_table_count,
        profiled_column_count=profiled_cols,
        tables=table_summaries,
    )
