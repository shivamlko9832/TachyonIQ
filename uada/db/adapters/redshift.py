"""
Redshift Adapter (P1-5)
========================
Wraps sqlalchemy-redshift (psycopg2 driver) to expose the DatabaseAdapter ABC.
Install: pip install sqlalchemy-redshift psycopg2-binary
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


class RedshiftAdapter(DatabaseAdapter):
    """
    Redshift adapter via sqlalchemy-redshift + psycopg2.
    Uses statement_timeout to enforce the query timeout at the session level.
    """

    def __init__(self, config: ConnectionConfig, settings: Settings) -> None:
        try:
            import sqlalchemy as sa
            import redshift_connector  # noqa: F401 — verifies driver present
        except ImportError as exc:
            raise ImportError(
                "sqlalchemy-redshift and psycopg2-binary are required for Redshift. "
                "Install: pip install sqlalchemy-redshift psycopg2-binary"
            ) from exc

        from urllib.parse import quote_plus

        password = quote_plus(config.password.get_secret_value())
        url = (
            f"redshift+psycopg2://{config.username}:{password}"
            f"@{config.host}:{config.port}/{config.database}"
        )
        timeout_ms = settings.db_query_timeout_seconds * 1000
        self._engine = sa.create_engine(
            url,
            connect_args={"options": f"-c statement_timeout={timeout_ms}"},
            pool_pre_ping=True,
        )
        self._settings = settings
        self._dialect_name = "redshift"
        self._database_name = config.database or "redshift"

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
            raise ConnectionError(f"Redshift connection failed: {exc}") from exc

    def execute_query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryExecutionResult:
        import sqlalchemy as sa
        t0 = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                conn.execute(sa.text(f"SET statement_timeout = {timeout_seconds * 1000}"))
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
        for tname in insp.get_table_names(schema="public"):
            cols = [RawColumnInfo(
                        name=c["name"], data_type=str(c["type"]), nullable=bool(c.get("nullable", True)),
                        is_primary_key=False, is_foreign_key=False,
                        references_table=None, references_column=None,
                        default_value=None, comment=c.get("comment"),
                    )
                    for c in insp.get_columns(tname, schema="public")]
            tables.append(
                RawTableInfo(
                    name=tname,
                    schema_name="public",
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

    def explain_query(self, sql: str) -> str:
        import sqlalchemy as sa
        try:
            with self._engine.connect() as conn:
                r = conn.execute(sa.text(f"EXPLAIN {sql}"))
                return "\n".join(str(row[0]) for row in r.fetchall())
        except Exception as exc:
            return f"EXPLAIN failed: {exc}"

    def dispose(self) -> None:
        self._engine.dispose()
