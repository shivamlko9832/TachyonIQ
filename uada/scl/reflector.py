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
    from uada.pipeline.data_profiler import DataProfiler, SchemaProfile

logger = logging.getLogger(__name__)

# DatabaseAdapter.dialect returns a SQLGlot dialect name; SQLDialect uses
# SQLAlchemy-style names for everything except tsql. Only entries that
# differ need mapping -- anything else is passed through unchanged.
_SQLGLOT_TO_SCL_DIALECT: dict[str, str] = {
    "postgres": "postgresql",
}


class SCLReflector:
    """Builds a candidate SemanticContextLayer from database introspection."""

    def from_adapter(
        self,
        adapter: DatabaseAdapter,
        profiler: DataProfiler | None = None,
        allowed_tables: frozenset[str] | None = None,
    ) -> SemanticContextLayer:
        """
        Reflect `adapter`'s schema into a candidate SemanticContextLayer.

        Tables and columns are populated with names, types, and PK/FK
        relationships. Foreign keys become LEFT joins. Descriptions,
        grains, metrics, glossary, and examples are left empty for a human
        to author -- this method never guesses business meaning.

        Args:
            adapter:        Live database adapter.
            profiler:       Optional DataProfiler instance. When supplied,
                            column null-% and distinct-count statistics are
                            attached as structured hints in the column
                            description so that downstream SCL enrichment
                            scripts can prioritise which columns to annotate.
                            Sample values are NEVER propagated here -- only
                            null_pct and distinct_count.
            allowed_tables: When provided, profiling is limited to this set
                            of table names (e.g. from a SecurityPolicy).
                            Ignored if `profiler` is None.

        Raises:
            ValueError: The adapter's dialect has no SQLDialect equivalent.
        """
        raw_schema = adapter.get_raw_schema()

        # Run profiling before table construction so stats are available
        # when building ColumnDefinitions.
        schema_profile: SchemaProfile | None = None
        if profiler is not None:
            profiling_tables = allowed_tables if allowed_tables is not None else frozenset(
                t.name for t in raw_schema.tables
            )
            try:
                schema_profile = profiler.profile(adapter, profiling_tables)
                logger.info(
                    "DataProfiler completed for '%s': %d table(s) profiled.",
                    adapter.database_name,
                    len(schema_profile.tables),
                )
            except Exception as exc:  # pragma: no cover
                # Profiling is best-effort -- a failure must never block
                # schema reflection, which is the primary deliverable.
                logger.warning("DataProfiler failed (continuing without stats): %s", exc)
                schema_profile = None
        table_names = {t.name for t in raw_schema.tables}

        tables: list[TableDefinition] = []
        joins: list[JoinDefinition] = []
        seen_joins: set[tuple[str, str]] = set()

        for raw_table in raw_schema.tables:
            table_prof = (
                schema_profile.tables.get(raw_table.name)
                if schema_profile is not None
                else None
            )
            tables.append(
                TableDefinition(
                    name=raw_table.name,
                    description=None,
                    grain=None,
                    columns=[
                        self._build_column(c, table_prof)
                        for c in raw_table.columns
                    ],
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

    def _build_column(
        self,
        raw_col: RawColumnInfo,
        table_profile: object | None = None,
    ) -> ColumnDefinition:
        """Map a RawColumnInfo to a candidate ColumnDefinition.

        When *table_profile* is supplied (a TableProfile from DataProfiler),
        null_pct and distinct_count are embedded in the description as
        structured hints.  Sample values are intentionally excluded -- they
        must never propagate into SCL YAML, LLM prompts, or logs.
        """
        references = None
        if raw_col.is_foreign_key and raw_col.references_table and raw_col.references_column:
            references = f"{raw_col.references_table}.{raw_col.references_column}"

        description: str | None = None
        if table_profile is not None:
            col_prof = next(
                (cp for cp in table_profile.columns if cp.column_name == raw_col.name),
                None,
            )
            if col_prof is not None:
                parts: list[str] = []
                if col_prof.null_pct is not None:
                    parts.append(f"null_pct={col_prof.null_pct:.1f}%")
                if col_prof.distinct_count is not None:
                    parts.append(f"distinct={col_prof.distinct_count}")
                if parts:
                    description = "[profiled] " + ", ".join(parts)

        return ColumnDefinition(
            name=raw_col.name,
            type=raw_col.data_type,
            is_primary_key=raw_col.is_primary_key,
            is_foreign_key=raw_col.is_foreign_key,
            references=references,
            description=description,
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
