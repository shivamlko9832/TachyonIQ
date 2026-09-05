"""
Snowflake Adapter (P1-3)
=========================
Wraps snowflake-sqlalchemy to expose the DatabaseAdapter ABC.
Install: pip install snowflake-sqlalchemy
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from uada.db.interface import (
    DatabaseAdapter,
    QueryExecutionError,
    QueryExecutionResult,
    QueryTimeoutError,
    RawColumnInfo,
    RawSchemaInfo,
    RawTableInfo,
)

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.db.connection_manager import ConnectionConfig

logger = logging.getLogger(__name__)


class SnowflakeAdapter(DatabaseAdapter):
    """
    Snowflake adapter via snowflake-sqlalchemy.
    Supports warehouse, role, schema, and session-level query timeout.
    """

    def __init__(self, config: ConnectionConfig, settings: Settings) -> None:
        try:
            from snowflake.sqlalchemy import URL as SnowflakeURL  # type: ignore[import-untyped]
            import sqlalchemy as sa
        except ImportError as exc:
            raise ImportError(
                "snowflake-sqlalchemy is required for Snowflake connections. "
                "Install it with: pip install snowflake-sqlalchemy"
            ) from exc

        extra = config.extra or {}
        url = SnowflakeURL(
            account=config.host,
            user=config.username,
            password=config.password.get_secret_value(),
            database=config.database,
            schema=extra.get("schema", "PUBLIC"),
            warehouse=extra.get("warehouse"),
            role=extra.get("role"),
        )
        self._engine = sa.create_engine(url, pool_pre_ping=True)
        self._settings = settings
        self._dialect_name = "snowflake"
        self._timeout_s = settings.db_query_timeout_seconds

    @property
    def dialect(self) -> str:
        return self._dialect_name

    def test_connection(self) -> bool:
        import sqlalchemy as sa
        try:
            with self._engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return True
        except Exception as exc:
            raise ConnectionError(f"Snowflake connection failed: {exc}") from exc

    def execute_query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryExecutionResult:
        import sqlalchemy as sa
        t0 = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                conn.execute(sa.text(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}"))
                result = conn.execute(sa.text(sql))
                cols = list(result.keys())
                raw_rows = result.fetchmany(max_rows + 1)
                is_truncated = len(raw_rows) > max_rows
                rows = [dict(zip(cols, r)) for r in raw_rows[:max_rows]]
            elapsed = int((time.perf_counter() - t0) * 1000)
            return QueryExecutionResult(columns=cols, rows=rows, execution_time_ms=elapsed,
                                        is_truncated=is_truncated, row_count=len(rows))
        except Exception as exc:
            raise QueryExecutionError(str(exc), sql, self._dialect_name) from exc

    def get_raw_schema(self) -> RawSchemaInfo:
        import hashlib, sqlalchemy as sa
        insp = sa.inspect(self._engine)
        tables: list[RawTableInfo] = []
        for tname in insp.get_table_names():
            cols = [RawColumnInfo(name=c["name"], data_type=str(c["type"]),
                                  is_primary_key=c["name"] in {k["name"] for k in insp.get_pk_constraint(tname).get("constrained_columns", [])},
                                  is_foreign_key=False, references=None)
                    for c in insp.get_columns(tname)]
            tables.append(RawTableInfo(name=tname, columns=cols, primary_keys=[], foreign_keys=[]))
        fingerprint = hashlib.sha256(
            "\n".join(sorted(f"{t.name}.{c.name}" for t in tables for c in t.columns)).encode()
        ).hexdigest()
        return RawSchemaInfo(tables=tables, schema_fingerprint=fingerprint)

    def explain_query(self, sql: str) -> str:
        import sqlalchemy as sa
        try:
            with self._engine.connect() as conn:
                r = conn.execute(sa.text(f"EXPLAIN {sql}"))
                return "\n".join(str(row) for row in r.fetchall())
        except Exception as exc:
            return f"EXPLAIN failed: {exc}"
