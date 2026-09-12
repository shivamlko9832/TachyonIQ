"""
SQLAlchemy Database Adapter
============================
Concrete implementation of DatabaseAdapter using SQLAlchemy Core.

Supports PostgreSQL, MySQL, SQLite, and SQL Server through SQLAlchemy's
dialect abstraction. Business logic never imports a database-specific
driver directly -- only this module does, via SQLAlchemy's dialect plugins.

Never logs SQL text, row values, or the connection string. Only query
metadata (row counts, timings, dialect) is logged.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

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
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import Engine, Inspector

    from uada.config import Settings

logger = logging.getLogger(__name__)

# SQLAlchemy dialect name -> SQLGlot dialect name.
_DIALECT_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "mssql": "tsql",
}

def _is_sqlite_memory_url(connection_string: str) -> bool:
    """Whether `connection_string` is a non-persistent (in-memory) SQLite URL."""
    return connection_string in ("sqlite://", "sqlite:///:memory:") or (
        ":memory:" in connection_string
    )


# Python value type -> UADA type label, used both for ad-hoc query results
# and (via TypeEngine.python_type) for reflected column types.
_PYTHON_TYPE_LABELS: dict[type, str] = {
    bool: "bool",
    int: "int",
    float: "float",
    Decimal: "float",
    str: "str",
    bytes: "str",
    datetime: "datetime",
    date: "datetime",
}


class SQLAlchemyAdapter(DatabaseAdapter):
    """
    DatabaseAdapter implementation backed by SQLAlchemy Core.

    One adapter instance owns one Engine (and connection pool) for the
    lifetime of the process.
    """

    def __init__(self, connection_string: str, settings: Settings) -> None:
        """
        Create the engine for `connection_string`.

        SQLite's default pool classes (SingletonThreadPool / NullPool) do
        not accept `pool_size`/`pool_recycle`, and its DBAPI has no
        `connect_timeout` parameter, so both are skipped for that dialect.

        An in-memory SQLite URL additionally needs `StaticPool` +
        `check_same_thread=False`: SQLAlchemy's default pool for
        `sqlite:///:memory:` (SingletonThreadPool) hands each *thread* its
        own separate in-memory database, invisible to every other thread
        -- including a concurrent request handled on a different thread
        by a real ASGI server. StaticPool shares one single connection
        across every thread instead.
        """
        self._settings = settings
        engine_kwargs: dict[str, Any] = {}
        connect_args: dict[str, Any] = {}

        if not connection_string.startswith("sqlite"):
            engine_kwargs["pool_size"] = settings.db_pool_size
            engine_kwargs["pool_recycle"] = settings.db_pool_recycle_seconds
            connect_args["connect_timeout"] = 10
        elif _is_sqlite_memory_url(connection_string):
            engine_kwargs["poolclass"] = StaticPool
            connect_args["check_same_thread"] = False

        try:
            self._engine: Engine = create_engine(
                connection_string,
                connect_args=connect_args,
                **engine_kwargs,
            )
        except SQLAlchemyError as exc:
            raise ConnectionError(f"Failed to create database engine: {exc}") from exc

        raw_dialect = self._engine.dialect.name
        self._dialect = _DIALECT_MAP.get(raw_dialect, raw_dialect)
        self._database_name = self._engine.url.database or raw_dialect

    @property
    def dialect(self) -> str:
        """SQLGlot dialect name for this database. E.g. 'postgres', 'mysql'."""
        return self._dialect

    @property
    def database_name(self) -> str:
        """The name of the connected database."""
        return self._database_name

    def test_connection(self) -> bool:
        """
        Verify connectivity and (best-effort) that the account is read-only.

        Returns:
            True on success.

        Raises:
            ConnectionError: If a basic `SELECT 1` fails.
        """
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise ConnectionError(f"Database connection check failed: {exc}") from exc

        self._probe_read_only()
        logger.info("Database connection verified for dialect '%s'.", self._dialect)
        return True

    def _probe_read_only(self) -> None:
        """
        Best-effort check that the credentials are read-only.

        Use a temporary table inside a rolled-back transaction. Probing a
        deliberately missing table is not meaningful: a writable account
        fails that probe for the same reason as a read-only account.
        """
        statement = (
            "CREATE TABLE #__uada_readonly_probe__ (x INTEGER)"
            if self._dialect == "tsql"
            else "CREATE TEMPORARY TABLE __uada_readonly_probe__ (x INTEGER)"
        )
        try:
            with self._engine.connect() as conn:
                transaction = conn.begin()
                try:
                    conn.execute(text(statement))
                except SQLAlchemyError:
                    transaction.rollback()
                    return
                transaction.rollback()
                logger.warning(
                    "Temporary table creation succeeded -- "
                    "the database account may not be read-only."
                )
        except SQLAlchemyError:
            logger.debug("Read-only probe could not be completed.", exc_info=True)

    def execute_query(
        self,
        sql: str,
        timeout_seconds: int = 30,
        max_rows: int = 1000,
    ) -> QueryExecutionResult:
        """
        Execute a validated, read-only SQL string.

        Args:
            sql: SQL that has already passed SQLValidator.validate().
            timeout_seconds: Maximum execution time before QueryTimeoutError.
            max_rows: Maximum rows returned; excess rows are truncated.

        Returns:
            QueryExecutionResult with rows, column metadata, and timing.

        Raises:
            QueryTimeoutError: Execution exceeded timeout_seconds.
            QueryExecutionError: The database rejected or failed the query.
        """
        start = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                conn = conn.execution_options(timeout=timeout_seconds)
                remove_guard = self._install_timeout_guard(conn, timeout_seconds)
                try:
                    result = conn.execute(text(sql))
                    column_names = list(result.keys())
                    rows = result.fetchmany(max_rows + 1)
                finally:
                    remove_guard()
        except SQLAlchemyError as exc:
            if self._dialect == "sqlite" and "interrupted" in str(exc).lower():
                raise QueryTimeoutError(timeout_seconds, sql) from exc
            raise QueryExecutionError(str(exc), sql, self._dialect) from exc

        is_truncated = len(rows) > max_rows
        if is_truncated:
            rows = rows[:max_rows]

        column_types = self._infer_column_types(len(column_names), rows)
        execution_time_ms = (time.perf_counter() - start) * 1000

        logger.info(
            "Query executed on dialect '%s': %d row(s), truncated=%s, %.2fms.",
            self._dialect,
            len(rows),
            is_truncated,
            execution_time_ms,
        )

        return QueryExecutionResult(
            column_names=column_names,
            column_types=column_types,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            is_truncated=is_truncated,
            execution_time_ms=execution_time_ms,
        )

    def _install_timeout_guard(self, conn: Any, timeout_seconds: int) -> Callable[[], None]:
        """
        Install a real, per-statement timeout for dialects that support one
        at the driver level.

        SQLite has no server-side statement timeout, but sqlite3's
        `set_progress_handler` calls back periodically during execution and
        aborts the statement if the callback returns non-zero -- the
        standard, thread-safe way to bound SQLite query time. For other
        dialects, real enforcement depends on server-side configuration
        (e.g. PostgreSQL `statement_timeout`, MySQL `MAX_EXECUTION_TIME`);
        this adapter does not attempt driver-specific session tweaks for
        those in the POC, so `timeout_seconds` is advisory only there.

        Returns:
            A zero-argument callable that removes the guard.
        """
        if self._dialect != "sqlite":
            return lambda: None

        raw_conn = conn.connection.dbapi_connection
        deadline = time.monotonic() + timeout_seconds

        def _progress_handler() -> int:
            return 1 if time.monotonic() > deadline else 0

        raw_conn.set_progress_handler(_progress_handler, 1000)

        def _remove() -> None:
            raw_conn.set_progress_handler(None, 0)

        return _remove

    def _infer_column_types(
        self, column_count: int, rows: Sequence[Any]
    ) -> list[str]:
        """
        Infer a UADA type label per column from the fetched values.

        Ad-hoc generated SQL (aggregates, expressions, aliases) has no
        fixed SQLAlchemy Column type to consult, so types are inferred
        from the actual returned Python values instead.
        """
        types = ["str"] * column_count
        resolved = [False] * column_count
        for row in rows:
            for i, value in enumerate(row):
                if resolved[i] or value is None:
                    continue
                types[i] = _PYTHON_TYPE_LABELS.get(type(value), "str")
                resolved[i] = True
            if all(resolved):
                break
        return types

    def get_raw_schema(self) -> RawSchemaInfo:
        """
        Reflect the full database schema via SQLAlchemy's Inspector.

        Returns:
            RawSchemaInfo covering every table visible to the connected
            account, with a SHA-256 fingerprint over all "table.column"
            pairs for downstream change detection.
        """
        inspector = inspect(self._engine)
        tables: list[RawTableInfo] = []
        fingerprint_parts: list[str] = []

        for table_name in inspector.get_table_names():
            columns = self._reflect_columns(inspector, table_name)
            try:
                table_comment = inspector.get_table_comment(table_name).get("text")
            except NotImplementedError:
                table_comment = None
            indexes = self._reflect_index_columns(inspector, table_name)

            tables.append(
                RawTableInfo(
                    name=table_name,
                    schema_name=None,
                    columns=columns,
                    row_count_estimate=None,
                    comment=table_comment,
                    indexes=indexes,
                )
            )
            fingerprint_parts.extend(f"{table_name}.{c.name}" for c in columns)

        fingerprint = hashlib.sha256(
            "\n".join(sorted(fingerprint_parts)).encode("utf-8")
        ).hexdigest()

        return RawSchemaInfo(
            dialect=self._dialect,
            database_name=self._database_name,
            tables=tables,
            schema_fingerprint=fingerprint,
        )

    def _reflect_columns(self, inspector: Inspector, table_name: str) -> list[RawColumnInfo]:
        """Build RawColumnInfo for every column of `table_name`."""
        raw_columns = inspector.get_columns(table_name)
        pk_constraint = inspector.get_pk_constraint(table_name)
        pk_columns: set[str] = set(pk_constraint.get("constrained_columns") or [])

        fk_map: dict[str, tuple[str | None, str | None]] = {}
        for fk in inspector.get_foreign_keys(table_name):
            referred_table = fk.get("referred_table")
            referred_columns = fk.get("referred_columns") or []
            constrained_columns = fk.get("constrained_columns") or []
            for local_col, remote_col in zip(
                constrained_columns, referred_columns, strict=False
            ):
                fk_map[local_col] = (referred_table, remote_col)

        columns: list[RawColumnInfo] = []
        for col in raw_columns:
            name = col["name"]
            ref_table, ref_column = fk_map.get(name, (None, None))
            default = col.get("default")
            columns.append(
                RawColumnInfo(
                    name=name,
                    data_type=self._map_column_type(col.get("type")),
                    nullable=bool(col.get("nullable", True)),
                    is_primary_key=name in pk_columns,
                    is_foreign_key=name in fk_map,
                    references_table=ref_table,
                    references_column=ref_column,
                    default_value=str(default) if default is not None else None,
                    comment=col.get("comment"),
                )
            )
        return columns

    def _reflect_index_columns(self, inspector: Inspector, table_name: str) -> list[str]:
        """Return the sorted set of column names covered by any index."""
        columns: set[str] = set()
        for index in inspector.get_indexes(table_name):
            for col in index.get("column_names") or []:
                if col:
                    columns.add(col)
        return sorted(columns)

    def _map_column_type(self, sa_type: Any) -> str:
        """Map a SQLAlchemy column TypeEngine to a UADA type label."""
        try:
            python_type = sa_type.python_type
        except (NotImplementedError, AttributeError):
            return "str"
        return _PYTHON_TYPE_LABELS.get(python_type, "str")

    def explain_query(self, sql: str) -> str:
        """
        Return the query execution plan as text.

        Used for debugging and observability only -- never for security
        decisions.
        """
        try:
            with self._engine.connect() as conn:
                if self._dialect in ("postgres", "mysql"):
                    result = conn.execute(text(f"EXPLAIN {sql}"))
                elif self._dialect == "sqlite":
                    result = conn.execute(text(f"EXPLAIN QUERY PLAN {sql}"))
                elif self._dialect == "tsql":
                    conn.exec_driver_sql("SET SHOWPLAN_TEXT ON")
                    result = conn.execute(text(sql))
                else:
                    raise QueryExecutionError(
                        f"EXPLAIN not supported for dialect '{self._dialect}'.",
                        sql,
                        self._dialect,
                    )
                return "\n".join(str(tuple(row)) for row in result.fetchall())
        except SQLAlchemyError as exc:
            raise QueryExecutionError(str(exc), sql, self._dialect) from exc
