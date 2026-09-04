"""
Tests for the SCL loader and reflector (uada/scl/loader.py, uada/scl/reflector.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from uada.config import Settings
from uada.db.adapter import SQLAlchemyAdapter
from uada.scl.loader import SCLLoader, SCLLoadError
from uada.scl.reflector import SCLReflector
from uada.scl.schema import JoinDefinition, SemanticContextLayer, SQLDialect

pytestmark = pytest.mark.unit

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "semantic_context.yaml"


class TestSCLLoader:
    def test_loads_example_config_cleanly(self) -> None:
        scl = SCLLoader.load(CONFIG_PATH)
        assert scl.version == "1.0"
        assert scl.database.name == "acme_analytics"
        assert {"orders", "customers"} <= {t.name for t in scl.tables}

    def test_missing_file_raises_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(SCLLoadError):
            SCLLoader.load(tmp_path / "does_not_exist.yaml")

    def test_invalid_yaml_raises_clear_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: '1.0'\ndatabase: [unbalanced\n", encoding="utf-8")
        with pytest.raises(SCLLoadError, match="line"):
            SCLLoader.load(bad)

    def test_schema_validation_failure_raises_clear_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad_schema.yaml"
        bad.write_text("version: '1.0'\n", encoding="utf-8")  # missing required `database`
        with pytest.raises(SCLLoadError, match="database"):
            SCLLoader.load(bad)

    def test_round_trip_save_and_load(self, tmp_path: Path) -> None:
        original = SCLLoader.load(CONFIG_PATH)
        out_path = tmp_path / "roundtrip.yaml"
        SCLLoader.save(original, out_path)
        reloaded = SCLLoader.load(out_path)
        assert reloaded.version == original.version
        assert {t.name for t in reloaded.tables} == {t.name for t in original.tables}
        assert reloaded.database.dialect == original.database.dialect

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        original = SCLLoader.load(CONFIG_PATH)
        out_path = tmp_path / "nested" / "dir" / "scl.yaml"
        SCLLoader.save(original, out_path)
        assert out_path.exists()


class TestJoinReferenceValidation:
    def test_join_referencing_unknown_table_fails(self) -> None:
        with pytest.raises(ValidationError):
            SemanticContextLayer(
                version="1.0",
                database={"name": "db", "dialect": "sqlite"},  # type: ignore[arg-type]
                tables=[{"name": "orders", "columns": []}],  # type: ignore[list-item]
                joins=[
                    JoinDefinition(
                        from_table="orders",
                        to_table="does_not_exist",
                        on="orders.x = does_not_exist.y",
                    )
                ],
            )

    def test_join_between_known_tables_succeeds(self) -> None:
        scl = SemanticContextLayer(
            version="1.0",
            database={"name": "db", "dialect": "sqlite"},  # type: ignore[arg-type]
            tables=[
                {"name": "orders", "columns": []},  # type: ignore[list-item]
                {"name": "customers", "columns": []},  # type: ignore[list-item]
            ],
            joins=[
                JoinDefinition(
                    from_table="orders",
                    to_table="customers",
                    on="orders.customer_id = customers.id",
                )
            ],
        )
        assert len(scl.joins) == 1


class TestSecurityExclusion:
    def test_get_allowed_tables_excludes_excluded_tables(self) -> None:
        scl = SCLLoader.load(CONFIG_PATH)
        allowed = scl.get_allowed_tables()
        assert "audit_log" not in allowed
        assert "user_sessions" not in allowed
        assert "api_keys" not in allowed
        assert "orders" in allowed
        assert "customers" in allowed


class TestSCLReflector:
    @pytest.fixture
    def adapter(self) -> SQLAlchemyAdapter:
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        adapter = SQLAlchemyAdapter("sqlite:///:memory:", settings)
        with adapter._engine.connect() as conn:
            conn.execute(
                text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            )
            conn.execute(
                text(
                    "CREATE TABLE orders ("
                    "id INTEGER PRIMARY KEY, "
                    "customer_id INTEGER NOT NULL REFERENCES customers(id), "
                    "revenue REAL NOT NULL)"
                )
            )
            conn.commit()
        return adapter

    def test_reflects_tables_and_columns(self, adapter: SQLAlchemyAdapter) -> None:
        scl = SCLReflector().from_adapter(adapter)
        assert scl.version == "1.0"
        assert scl.database.dialect == SQLDialect.SQLITE
        table_names = {t.name for t in scl.tables}
        assert {"customers", "orders"} <= table_names

        orders = next(t for t in scl.tables if t.name == "orders")
        customer_id_col = next(c for c in orders.columns if c.name == "customer_id")
        assert customer_id_col.is_foreign_key is True
        assert customer_id_col.references == "customers.id"
        assert customer_id_col.type == "int"

    def test_reflects_join_from_foreign_key(self, adapter: SQLAlchemyAdapter) -> None:
        scl = SCLReflector().from_adapter(adapter)
        assert len(scl.joins) == 1
        join = scl.joins[0]
        assert join.from_table == "orders"
        assert join.to_table == "customers"
        assert join.join_type == "LEFT"

    def test_descriptions_and_grain_left_for_human(self, adapter: SQLAlchemyAdapter) -> None:
        scl = SCLReflector().from_adapter(adapter)
        for table in scl.tables:
            assert table.description is None
            assert table.grain is None

    def test_metrics_glossary_examples_left_empty(self, adapter: SQLAlchemyAdapter) -> None:
        scl = SCLReflector().from_adapter(adapter)
        assert scl.metrics == []
        assert scl.glossary == []
        assert scl.examples == []

    def test_output_passes_its_own_validation(self, adapter: SQLAlchemyAdapter) -> None:
        # from_adapter's return value is a SemanticContextLayer -- pydantic
        # already ran the join-reference validator during construction.
        scl = SCLReflector().from_adapter(adapter)
        assert isinstance(scl, SemanticContextLayer)
