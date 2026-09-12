"""DuckDB adapter with the same typed, read-only contract as SQLAlchemyAdapter."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from uada.db.interface import (
    ConnectionError,
    DatabaseAdapter,
    QueryExecutionError,
    QueryExecutionResult,
    RawColumnInfo,
    RawSchemaInfo,
    RawTableInfo,
)

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.db.connection_manager import ConnectionConfig


class DuckDBAdapter(DatabaseAdapter):
    """Small native DuckDB adapter used for local OLAP/demo databases.

    The connection is opened read-only so a validated analytical query cannot
    mutate the file even if a future caller bypasses the normal route.
    """

    def __init__(self, config: ConnectionConfig, settings: Settings) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError("duckdb is required for DuckDB connections") from exc

        database = config.database or ":memory:"
        try:
            self._connection = duckdb.connect(database=database, read_only=database != ":memory:")
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError("Failed to open DuckDB database.") from exc
        self._database_name = str(Path(database).name) if database != ":memory:" else ":memory:"
        self._settings = settings
        self._lock = threading.RLock()

    @property
    def dialect(self) -> str:
        return "duckdb"

    @property
    def database_name(self) -> str:
        return self._database_name

    def test_connection(self) -> bool:
        try:
            with self._lock:
                self._connection.execute("SELECT 1").fetchone()
            return True
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError("DuckDB connection check failed.") from exc

    def execute_query(
        self, sql: str, timeout_seconds: int = 30, max_rows: int = 1000
    ) -> QueryExecutionResult:
        del timeout_seconds  # DuckDB's native cancellation is not portable here.
        started = time.perf_counter()
        try:
            with self._lock:
                cursor = self._connection.execute(sql)
                column_names = [str(item[0]) for item in (cursor.description or [])]
                raw_rows = cursor.fetchmany(max_rows + 1)
        except Exception as exc:  # noqa: BLE001
            raise QueryExecutionError(str(exc), sql, self.dialect) from exc

        is_truncated = len(raw_rows) > max_rows
        rows = [list(row) for row in raw_rows[:max_rows]]
        column_types = ["str"] * len(column_names)
        for row in rows:
            for index, value in enumerate(row):
                if value is not None and column_types[index] == "str":
                    column_types[index] = type(value).__name__
        return QueryExecutionResult(
            column_names=column_names,
            column_types=column_types,
            rows=rows,
            row_count=len(rows),
            is_truncated=is_truncated,
            execution_time_ms=(time.perf_counter() - started) * 1000,
        )

    def get_raw_schema(self) -> RawSchemaInfo:
        try:
            with self._lock:
                names = [row[0] for row in self._connection.execute("SHOW TABLES").fetchall()]
                tables: list[RawTableInfo] = []
                for table_name in names:
                    quoted = self._quote(str(table_name))
                    rows = self._connection.execute(f"DESCRIBE {quoted}").fetchall()
                    columns = [
                        RawColumnInfo(
                            name=str(row[0]),
                            data_type=str(row[1]),
                            nullable=True,
                            is_primary_key=False,
                            is_foreign_key=False,
                            references_table=None,
                            references_column=None,
                            default_value=str(row[4]) if len(row) > 4 and row[4] else None,
                            comment=None,
                        )
                        for row in rows
                    ]
                    tables.append(
                        RawTableInfo(
                            name=str(table_name),
                            schema_name=None,
                            columns=columns,
                            row_count_estimate=None,
                            comment=None,
                            indexes=[],
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError("DuckDB schema discovery failed.") from exc

        fingerprint = hashlib.sha256(
            "\n".join(sorted(f"{t.name}.{c.name}" for t in tables for c in t.columns)).encode()
        ).hexdigest()
        return RawSchemaInfo(
            dialect=self.dialect,
            database_name=self.database_name,
            tables=tables,
            schema_fingerprint=fingerprint,
        )

    def explain_query(self, sql: str) -> str:
        try:
            with self._lock:
                rows = self._connection.execute(f"EXPLAIN {sql}").fetchall()
            return "\n".join(str(row[0]) for row in rows)
        except Exception as exc:  # noqa: BLE001
            raise QueryExecutionError(str(exc), sql, self.dialect) from exc

    def dispose(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _quote(identifier: str) -> str:
        if not identifier or "\x00" in identifier or '"' in identifier:
            raise ValueError("Invalid database identifier")
        return f'"{identifier}"'
