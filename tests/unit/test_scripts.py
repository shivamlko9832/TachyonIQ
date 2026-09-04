"""
Tests for the CLI scripts (scripts/onboard_db.py, scripts/build_index.py).

onboard_db.py is exercised against a real SQLite file (no network, no
LLM). build_index.py uses the real Embedder (consistent with how
tests/unit/test_retrieval.py and test_schema_linker.py already test it --
no LLM is involved, so it isn't mocked), pointed at a tmp_path via
monkeypatching the settings singleton's chroma_path/collection_name.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from scripts import build_index, onboard_db
from uada.scl.loader import SCLLoader

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _seed_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
        "customer_id INTEGER REFERENCES customers(id), revenue REAL NOT NULL)"
    )
    conn.commit()
    conn.close()


class TestOnboardDB:
    def test_writes_valid_scl_from_reflected_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_sqlite(db_path)
        output_path = tmp_path / "scl.yaml"

        exit_code = onboard_db.main(
            ["--db-url", f"sqlite:///{db_path}", "--output", str(output_path)]
        )

        assert exit_code == 0
        assert output_path.exists()

        scl = SCLLoader.load(output_path)
        assert {t.name for t in scl.tables} == {"customers", "orders"}
        assert len(scl.joins) == 1
        assert scl.joins[0].from_table == "orders"
        assert scl.joins[0].to_table == "customers"

    def test_output_defaults_to_config_semantic_context_yaml(self) -> None:
        args = onboard_db.parse_args(["--db-url", "sqlite:///:memory:"])
        assert args.output == "config/semantic_context.yaml"

    def test_connection_failure_returns_error_and_writes_nothing(self, tmp_path: Path) -> None:
        # Parent directory doesn't exist -- sqlite3 fails to open it
        # quickly, with no network involved (unlike an unreachable host).
        bad_db_path = tmp_path / "no_such_directory" / "test.db"
        output_path = tmp_path / "scl.yaml"

        exit_code = onboard_db.main(
            ["--db-url", f"sqlite:///{bad_db_path}", "--output", str(output_path)]
        )

        assert exit_code == 1
        assert not output_path.exists()


class TestBuildIndex:
    def test_indexes_documents_from_scl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from uada.config import settings as real_settings
        from uada.scl.schema import (
            ColumnDefinition,
            DatabaseMeta,
            MetricDefinition,
            SecurityPolicy,
            SemanticContextLayer,
            TableDefinition,
        )

        scl = SemanticContextLayer(
            version="1.0",
            database=DatabaseMeta(name="test_db", dialect="sqlite"),
            tables=[
                TableDefinition(
                    name="orders",
                    description="Orders.",
                    columns=[ColumnDefinition(name="revenue", type="float")],
                )
            ],
            metrics=[
                MetricDefinition(
                    name="revenue", description="Total revenue.", formula="SUM(orders.revenue)"
                )
            ],
            security=SecurityPolicy(),
        )
        scl_path = tmp_path / "scl.yaml"
        SCLLoader.save(scl, scl_path)

        monkeypatch.setattr(real_settings, "chroma_path", tmp_path / "chroma")
        monkeypatch.setattr(real_settings, "chroma_collection_name", "test_build_index_collection")

        exit_code = build_index.main(["--scl-path", str(scl_path)])

        assert exit_code == 0

    def test_invalid_scl_returns_error_code(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "bad.yaml"
        bad_path.write_text("version: '1.0'\n", encoding="utf-8")  # missing required `database`

        exit_code = build_index.main(["--scl-path", str(bad_path)])

        assert exit_code == 1

    def test_scl_path_defaults_to_none(self) -> None:
        args = build_index.parse_args([])
        assert args.scl_path is None
