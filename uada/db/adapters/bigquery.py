"""
BigQuery Adapter (P1-4)
========================
Wraps sqlalchemy-bigquery to expose the DatabaseAdapter ABC.
Install: pip install sqlalchemy-bigquery google-cloud-bigquery
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


class BigQueryAdapter(DatabaseAdapter):
    """
    BigQuery adapter via sqlalchemy-bigquery.
    Supports project, dataset, OAuth (ADC) and service-account JSON credentials.
    """

    def __init__(self, config: ConnectionConfig, settings: Settings) -> None:
        try:
            import sqlalchemy as sa
        except ImportError as exc:
            raise ImportError(
                "sqlalchemy-bigquery is required for BigQuery connections. "
                "Install: pip install sqlalchemy-bigquery google-cloud-bigquery"
            ) from exc

        extra = config.extra or {}
        project = extra.get("project", config.database)
        dataset = extra.get("dataset", "")
        creds_json = extra.get("credentials_base64")  # base64-encoded service-account JSON
        job_timeout = extra.get("job_timeout_ms", settings.db_query_timeout_seconds * 1000)

        url = f"bigquery://{project}/{dataset}"
        create_kwargs: dict = {"execution_options": {"job_timeout_ms": job_timeout}}
        if creds_json:
            import base64, json, tempfile, os
            decoded = base64.b64decode(creds_json).decode()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
            tmp.write(decoded)
            tmp.close()
            create_kwargs["credentials_path"] = tmp.name

        self._engine = sa.create_engine(url, **create_kwargs)
        self._settings = settings
        self._dialect_name = "bigquery"

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
            raise ConnectionError(f"BigQuery connection failed: {exc}") from exc

    def execute_query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryExecutionResult:
        import sqlalchemy as sa
        t0 = time.perf_counter()
        try:
            with self._engine.connect() as conn:
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
                                  is_primary_key=False, is_foreign_key=False, references=None)
                    for c in insp.get_columns(tname)]
            tables.append(RawTableInfo(name=tname, columns=cols, primary_keys=[], foreign_keys=[]))
        fingerprint = hashlib.sha256(
            "\n".join(sorted(f"{t.name}.{c.name}" for t in tables for c in t.columns)).encode()
        ).hexdigest()
        return RawSchemaInfo(tables=tables, schema_fingerprint=fingerprint)

    def explain_query(self, sql: str) -> str:
        return f"-- BigQuery EXPLAIN not available via SQLAlchemy; use the BigQuery console.\n-- SQL: {sql[:200]}"
