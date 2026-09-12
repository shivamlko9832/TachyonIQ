"""
DatabaseAdapter Interface
==========================
Abstract base class for all database adapters.

Every supported database (PostgreSQL, MySQL, SQLite, SQL Server) is accessed
through this interface. Business logic never imports a database-specific driver.

Design contract:
- All connections use read-only credentials.
- execute_query() enforces timeout and row limits.
- get_raw_schema() returns a dialect-agnostic dict for the SCL reflector.
- The interface is synchronous for the POC. Async can be added behind
  this interface without changing callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RawColumnInfo:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    references_table: str | None
    references_column: str | None
    default_value: str | None
    comment: str | None  # From database column comments if available


@dataclass
class RawTableInfo:
    name: str
    schema_name: str | None
    columns: list[RawColumnInfo]
    row_count_estimate: int | None  # May be None if estimation is too slow
    comment: str | None  # From database table comments if available
    indexes: list[str]   # Index column names


@dataclass
class RawSchemaInfo:
    dialect: str          # SQLGlot dialect name
    database_name: str
    tables: list[RawTableInfo]
    schema_fingerprint: str  # SHA-256 of sorted table+column names — for change detection


@dataclass
class QueryExecutionResult:
    column_names: list[str]
    column_types: list[str]
    rows: list[list[object]]
    row_count: int
    is_truncated: bool
    # Keep a default so lightweight adapters, fixtures, and callers that only
    # need result shape do not have to fabricate timing data. Concrete
    # adapters always populate the measured value.
    execution_time_ms: float = 0.0


class DatabaseAdapter(ABC):
    """
    Abstract interface for database connectivity.

    Implementors: SQLAlchemyAdapter (primary), future adapters as needed.

    All methods may raise DatabaseError on unrecoverable failure.
    """

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Verify the connection is alive and the account is read-only.
        Returns True on success. Raises DatabaseError on failure.
        """
        ...

    @abstractmethod
    def execute_query(
        self,
        sql: str,
        timeout_seconds: int = 30,
        max_rows: int = 1000,
    ) -> QueryExecutionResult:
        """
        Execute a SQL string and return the result.

        Args:
            sql: A validated SQL string. MUST have passed SQLValidator.validate()
                 before being passed here.
            timeout_seconds: Maximum query execution time.
            max_rows: Maximum rows to return. If exceeded, result is truncated
                      and is_truncated=True.

        Returns:
            QueryExecutionResult with rows and metadata.

        Raises:
            QueryTimeoutError: If execution exceeds timeout_seconds.
            QueryExecutionError: If the database returns an error.
            RowLimitExceededError: If max_rows is exceeded (after truncation).
        """
        ...

    @abstractmethod
    def get_raw_schema(self) -> RawSchemaInfo:
        """
        Discover the full database schema using native introspection.

        Returns a RawSchemaInfo suitable for the SCL Reflector.
        Does NOT return excluded tables — caller applies exclusions.

        Uses SQLAlchemy Inspector under the hood.
        """
        ...

    @abstractmethod
    def explain_query(self, sql: str) -> str:
        """
        Return the query execution plan as a string.
        Used for debugging and observability. Never used for security decisions.
        """
        ...

    @property
    @abstractmethod
    def dialect(self) -> str:
        """SQLGlot dialect name for this database. E.g. 'postgres', 'mysql'."""
        ...

    @property
    @abstractmethod
    def database_name(self) -> str:
        """The name of the connected database."""
        ...


# ── Exceptions ────────────────────────────────────────────────────────────────


class DatabaseError(Exception):
    """Base class for all database adapter errors."""


class ConnectionError(DatabaseError):
    """Raised when the database connection cannot be established."""


class QueryTimeoutError(DatabaseError):
    """Raised when a query exceeds its timeout budget."""

    def __init__(self, timeout_seconds: int, sql: str) -> None:
        super().__init__(
            f"Query exceeded timeout of {timeout_seconds}s."
        )
        self.timeout_seconds = timeout_seconds
        self.sql = sql


class QueryExecutionError(DatabaseError):
    """Raised when the database returns an error during query execution."""

    def __init__(self, message: str, sql: str, dialect: str) -> None:
        super().__init__(message)
        self.sql = sql
        self.dialect = dialect
        # Parse the error message to produce a repair hint
        self.repair_hint = self._extract_repair_hint(message)

    def _extract_repair_hint(self, message: str) -> str:
        """
        Extracts a structured hint for the SQL repair loop.
        This is heuristic — improve as real-world errors accumulate.
        """
        msg_lower = message.lower()
        if "does not exist" in msg_lower or "unknown table" in msg_lower:
            return "The referenced table or column may not exist. Check table names."
        if "syntax error" in msg_lower:
            return "SQL syntax error. Check dialect-specific syntax."
        if "ambiguous" in msg_lower:
            return "Column reference is ambiguous. Use fully qualified table.column names."
        if "aggregate" in msg_lower and "group by" in msg_lower:
            return "Aggregation error. Check GROUP BY includes all non-aggregated columns."
        # Do not pass raw driver text (which can contain literals, schema
        # names, or provider metadata) into a repair prompt or API response.
        return "Database rejected the query; verify the referenced fields and dialect syntax."


class SecurityViolation(Exception):
    """
    Raised when SQLValidator rejects a query.
    This exception propagates to the pipeline orchestrator which
    STOPS execution and logs the incident. Never retry after this.
    """

    def __init__(self, violation_type: str, detail: str) -> None:
        super().__init__(f"Security violation [{violation_type}]: {detail}")
        self.violation_type = violation_type
        self.detail = detail
