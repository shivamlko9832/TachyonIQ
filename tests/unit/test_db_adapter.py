"""
Tests for SQLAlchemyAdapter (uada/db/adapter.py).

All tests run against SQLite in-memory ("sqlite:///:memory:") -- no real
client database required. Each test gets its own adapter/engine instance
so schemas never leak between tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from uada.config import Settings
from uada.db.adapter import SQLAlchemyAdapter
from uada.db.interface import QueryExecutionError, QueryTimeoutError

pytestmark = pytest.mark.unit

# A recursive CTE with a large bound -- enough SQLite VM steps for the
# progress-handler timeout guard to have a chance to fire.
_HEAVY_QUERY = """
    WITH RECURSIVE cnt(x) AS (
        SELECT 1
        UNION ALL
        SELECT x + 1 FROM cnt WHERE x < 5000000
    )
    SELECT count(*) FROM cnt
"""


@pytest.fixture
def settings() -> Settings:
    return Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]


@pytest.fixture
def adapter(settings: Settings) -> SQLAlchemyAdapter:
    adapter = SQLAlchemyAdapter("sqlite:///:memory:", settings)
    with adapter._engine.connect() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE customers (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    region TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    revenue REAL NOT NULL,
                    order_date TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_orders_customer_id ON orders(customer_id)"))
        conn.execute(
            text("INSERT INTO customers (id, name, region) VALUES (1, 'Acme', 'US')")
        )
        conn.execute(
            text("INSERT INTO customers (id, name, region) VALUES (2, 'Globex', 'EU')")
        )
        conn.execute(
            text(
                "INSERT INTO orders (id, customer_id, revenue, order_date) "
                "VALUES (1, 1, 100.5, '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO orders (id, customer_id, revenue, order_date) "
                "VALUES (2, 1, 200.0, '2026-02-01')"
            )
        )
        conn.commit()
    return adapter


class TestConnection:
    def test_test_connection_succeeds(self, adapter: SQLAlchemyAdapter) -> None:
        assert adapter.test_connection() is True

    def test_dialect_is_sqlite(self, adapter: SQLAlchemyAdapter) -> None:
        assert adapter.dialect == "sqlite"

    def test_database_name_is_set(self, adapter: SQLAlchemyAdapter) -> None:
        assert isinstance(adapter.database_name, str)
        assert adapter.database_name


class TestExecuteQuery:
    def test_returns_rows_and_columns(self, adapter: SQLAlchemyAdapter) -> None:
        result = adapter.execute_query("SELECT id, name, region FROM customers ORDER BY id")
        assert result.column_names == ["id", "name", "region"]
        assert result.column_types == ["int", "str", "str"]
        assert result.rows == [[1, "Acme", "US"], [2, "Globex", "EU"]]
        assert result.row_count == 2
        assert result.is_truncated is False
        assert result.execution_time_ms >= 0

    def test_infers_float_type(self, adapter: SQLAlchemyAdapter) -> None:
        result = adapter.execute_query("SELECT revenue FROM orders ORDER BY id")
        assert result.column_types == ["float"]

    def test_row_limit_truncation(self, adapter: SQLAlchemyAdapter) -> None:
        result = adapter.execute_query(
            "SELECT id FROM orders ORDER BY id", max_rows=1
        )
        assert result.row_count == 1
        assert result.is_truncated is True
        assert result.rows == [[1]]

    def test_row_limit_not_exceeded_is_not_truncated(
        self, adapter: SQLAlchemyAdapter
    ) -> None:
        result = adapter.execute_query(
            "SELECT id FROM orders ORDER BY id", max_rows=10
        )
        assert result.row_count == 2
        assert result.is_truncated is False

    def test_execution_error_on_missing_table(self, adapter: SQLAlchemyAdapter) -> None:
        with pytest.raises(QueryExecutionError) as exc_info:
            adapter.execute_query("SELECT * FROM does_not_exist")
        assert exc_info.value.sql == "SELECT * FROM does_not_exist"
        assert exc_info.value.dialect == "sqlite"

    def test_timeout_raised_on_slow_query(self, adapter: SQLAlchemyAdapter) -> None:
        with pytest.raises(QueryTimeoutError) as exc_info:
            adapter.execute_query(_HEAVY_QUERY, timeout_seconds=0)
        assert exc_info.value.timeout_seconds == 0

    def test_generous_timeout_does_not_interrupt_normal_query(
        self, adapter: SQLAlchemyAdapter
    ) -> None:
        result = adapter.execute_query(
            "SELECT id FROM customers ORDER BY id", timeout_seconds=30
        )
        assert result.row_count == 2


class TestSchemaReflection:
    def test_discovers_all_tables(self, adapter: SQLAlchemyAdapter) -> None:
        schema = adapter.get_raw_schema()
        assert schema.dialect == "sqlite"
        table_names = {t.name for t in schema.tables}
        assert {"customers", "orders"} <= table_names

    def test_primary_key_flagged(self, adapter: SQLAlchemyAdapter) -> None:
        schema = adapter.get_raw_schema()
        customers = next(t for t in schema.tables if t.name == "customers")
        id_col = next(c for c in customers.columns if c.name == "id")
        assert id_col.is_primary_key is True
        assert id_col.data_type == "int"

    def test_foreign_key_resolved(self, adapter: SQLAlchemyAdapter) -> None:
        schema = adapter.get_raw_schema()
        orders = next(t for t in schema.tables if t.name == "orders")
        customer_id_col = next(c for c in orders.columns if c.name == "customer_id")
        assert customer_id_col.is_foreign_key is True
        assert customer_id_col.references_table == "customers"
        assert customer_id_col.references_column == "id"

    def test_index_columns_present(self, adapter: SQLAlchemyAdapter) -> None:
        schema = adapter.get_raw_schema()
        orders = next(t for t in schema.tables if t.name == "orders")
        assert "customer_id" in orders.indexes

    def test_fingerprint_is_deterministic(self, adapter: SQLAlchemyAdapter) -> None:
        first = adapter.get_raw_schema().schema_fingerprint
        second = adapter.get_raw_schema().schema_fingerprint
        assert first == second
        assert len(first) == 64  # SHA-256 hex digest

    def test_fingerprint_changes_with_schema(
        self, adapter: SQLAlchemyAdapter, settings: Settings
    ) -> None:
        before = adapter.get_raw_schema().schema_fingerprint
        with adapter._engine.connect() as conn:
            conn.execute(text("ALTER TABLE customers ADD COLUMN tier TEXT"))
            conn.commit()
        after = adapter.get_raw_schema().schema_fingerprint
        assert before != after


class TestExplainQuery:
    def test_returns_nonempty_plan(self, adapter: SQLAlchemyAdapter) -> None:
        plan = adapter.explain_query("SELECT * FROM customers")
        assert isinstance(plan, str)
        assert plan.strip()
