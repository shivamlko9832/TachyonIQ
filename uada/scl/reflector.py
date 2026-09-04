"""
Semantic Context Layer — Schema Reflector
===========================================
Generates a *candidate* SemanticContextLayer directly from a live database
via `DatabaseAdapter.get_raw_schema()`. The output is a starting point:
table/column descriptions, grains, metrics, glossary terms, and example
queries are left empty for a human to fill in afterward.

Used by `scripts/onboard_db.py` to bootstrap a new database's SCL YAML.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uada.scl.schema import (
    ColumnDefinition,
    DatabaseMeta,
    JoinDefinition,
    SecurityPolicy,
    SemanticContextLayer,
    SQLDialect,
    TableDefinition,
)

if TYPE_CHECKING:
    from uada.db.interface import DatabaseAdapter, RawColumnInfo

logger = logging.getLogger(__name__)

# DatabaseAdapter.dialect returns a SQLGlot dialect name; SQLDialect uses
# SQLAlchemy-style names for everything except tsql. Only entries that
# differ need mapping -- anything else is passed through unchanged.
_SQLGLOT_TO_SCL_DIALECT: dict[str, str] = {
    "postgres": "postgresql",
}


class SCLReflector:
    """Builds a candidate SemanticContextLayer from database introspection."""

    def from_adapter(self, adapter: DatabaseAdapter) -> SemanticContextLayer:
        """
        Reflect `adapter`'s schema into a candidate SemanticContextLayer.

        Tables and columns are populated with names, types, and PK/FK
        relationships. Foreign keys become LEFT joins. Descriptions,
        grains, metrics, glossary, and examples are left empty for a human
        to author -- this method never guesses business meaning.

        Raises:
            ValueError: The adapter's dialect has no SQLDialect equivalent.
        """
        raw_schema = adapter.get_raw_schema()
        table_names = {t.name for t in raw_schema.tables}

        tables: list[TableDefinition] = []
        joins: list[JoinDefinition] = []
        seen_joins: set[tuple[str, str]] = set()

        for raw_table in raw_schema.tables:
            tables.append(
                TableDefinition(
                    name=raw_table.name,
                    description=None,
                    grain=None,
                    columns=[self._build_column(c) for c in raw_table.columns],
                )
            )

            for raw_col in raw_table.columns:
                join = self._build_join(raw_table.name, raw_col, table_names)
                if join is None:
                    continue
                key = (join.from_table, join.to_table)
                if key in seen_joins:
                    continue
                seen_joins.add(key)
                joins.append(join)

        scl = SemanticContextLayer(
            version="1.0",
            database=DatabaseMeta(
                name=adapter.database_name,
                dialect=self._map_dialect(raw_schema.dialect),
            ),
            tables=tables,
            metrics=[],
            joins=joins,
            glossary=[],
            examples=[],
            security=SecurityPolicy(),
        )
        logger.info(
            "Reflected candidate SCL for '%s': %d table(s), %d join(s).",
            adapter.database_name,
            len(tables),
            len(joins),
        )
        return scl

    def _build_column(self, raw_col: RawColumnInfo) -> ColumnDefinition:
        """Map a RawColumnInfo to a candidate ColumnDefinition."""
        references = None
        if raw_col.is_foreign_key and raw_col.references_table and raw_col.references_column:
            references = f"{raw_col.references_table}.{raw_col.references_column}"
        return ColumnDefinition(
            name=raw_col.name,
            type=raw_col.data_type,
            is_primary_key=raw_col.is_primary_key,
            is_foreign_key=raw_col.is_foreign_key,
            references=references,
        )

    def _build_join(
        self,
        from_table: str,
        raw_col: RawColumnInfo,
        known_tables: set[str],
    ) -> JoinDefinition | None:
        """
        Build a LEFT join from a foreign-key column, or None if the column
        isn't a resolvable foreign key (e.g. it references a table outside
        the reflected schema, which would fail SemanticContextLayer's own
        join-reference validation).
        """
        if not raw_col.is_foreign_key or not raw_col.references_table:
            return None
        if not raw_col.references_column:
            return None
        if raw_col.references_table not in known_tables:
            return None
        return JoinDefinition(
            from_table=from_table,
            to_table=raw_col.references_table,
            on=(
                f"{from_table}.{raw_col.name} = "
                f"{raw_col.references_table}.{raw_col.references_column}"
            ),
            join_type="LEFT",
        )

    def _map_dialect(self, sqlglot_dialect: str) -> SQLDialect:
        """Map a SQLGlot dialect name to its SQLDialect equivalent."""
        mapped = _SQLGLOT_TO_SCL_DIALECT.get(sqlglot_dialect, sqlglot_dialect)
        try:
            return SQLDialect(mapped)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported dialect '{sqlglot_dialect}' for SCL generation. "
                f"Supported: {[d.value for d in SQLDialect]}"
            ) from exc
