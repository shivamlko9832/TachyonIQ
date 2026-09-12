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
    ConnectionError,
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
        self._database_name = config.database or "snowflake"

    @property
    def dialect(self) -> str:
        return self._dialect_name

    @property
    def database_name(self) -> str:
        return self._database_name

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
            rows = [list(row) for row in raw_rows[:max_rows]]
            column_types = ["str"] * len(cols)
            for row in rows:
                for index, value in enumerate(row):
                    if value is not None and column_types[index] == "str":
                        column_types[index] = type(value).__name__
            return QueryExecutionResult(
                column_names=cols,
                column_types=column_types,
                rows=rows,
                execution_time_ms=elapsed,
                is_truncated=is_truncated,
                row_count=len(rows),
            )
        except Exception as exc:
            raise QueryExecutionError(str(exc), sql, self._dialect_name) from exc

    def get_raw_schema(self) -> RawSchemaInfo:
        import hashlib, sqlalchemy as sa
        insp = sa.inspect(self._engine)
        tables: list[RawTableInfo] = []
        for tname in insp.get_table_names():
            cols = [RawColumnInfo(
                        name=c["name"], data_type=str(c["type"]), nullable=bool(c.get("nullable", True)),
                        is_primary_key=c["name"] in set(insp.get_pk_constraint(tname).get("constrained_columns", []) or []),
                        is_foreign_key=False, references_table=None, references_column=None,
                        default_value=None, comment=c.get("comment"),
                    )
                    for c in insp.get_columns(tname)]
            tables.append(
                RawTableInfo(
                    name=tname,
                    schema_name=None,
                    columns=cols,
                    row_count_estimate=None,
                    comment=None,
                    indexes=[],
                )
            )
        fingerprint = hashlib.sha256(
            "\n".join(sorted(f"{t.name}.{c.name}" for t in tables for c in t.columns)).encode()
        ).hexdigest()
        return RawSchemaInfo(
            dialect=self._dialect_name,
            database_name=self._database_name,
            tables=tables,
            schema_fingerprint=fingerprint,
        )

    def dispose(self) -> None:
        self._engine.dispose()

    def explain_query(self, sql: str) -> str:
        import sqlalchemy as sa
        try:
            with self._engine.connect() as conn:
                r = conn.execute(sa.text(f"EXPLAIN {sql}"))
                return "\n".join(str(row) for row in r.fetchall())
        except Exception as exc:
            return f"EXPLAIN failed: {exc}"
