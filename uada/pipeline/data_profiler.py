"""
Data Profiler (P1-1)
=====================
Computes per-column statistics for every table in a database:
  - null%          — fraction of NULL values
  - distinct count — approximate cardinality
  - min / max      — numeric and temporal range
  - sample values  — up to 5 representative values (never logged)
  - temporal range — earliest / latest timestamps for DATE/TIMESTAMP cols

The profiler works against any DatabaseAdapter-compatible connection and
produces a `SchemaProfile` object that the SCLReflector can embed into
generated SCL YAML to improve schema-linking quality.

Security note: sample values are held ONLY in the returned dataclass and
are NEVER injected into LLM prompts or written to logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uada.db.interface import DatabaseAdapter

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT = 5
_PROFILE_TIMEOUT_S = 20


@dataclass
class ColumnProfile:
    table_name: str
    column_name: str
    data_type: str
    null_pct: float | None = None       # 0.0–1.0
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    sample_values: list[str] = field(default_factory=list)  # NEVER in prompts / logs
    is_temporal: bool = False


@dataclass
class TableProfile:
    table_name: str
    row_count: int | None = None
    columns: list[ColumnProfile] = field(default_factory=list)


@dataclass
class SchemaProfile:
    tables: dict[str, TableProfile] = field(default_factory=dict)  # table_name → profile

    def get_column(self, table: str, column: str) -> ColumnProfile | None:
        tp = self.tables.get(table)
        if tp is None:
            return None
        return next((c for c in tp.columns if c.column_name == column), None)


class DataProfiler:
    """
    Profiles all allowed tables in a database using lightweight SQL queries.
    One COUNT(*) + per-column aggregate query per table; read-only.
    """

    def profile(
        self,
        adapter: DatabaseAdapter,
        allowed_tables: frozenset[str],
        *,
        timeout_s: int = _PROFILE_TIMEOUT_S,
    ) -> SchemaProfile:
        """
        Run profiling across all tables in `allowed_tables`.
        Tables/columns that fail to profile are skipped with a warning.
        """
        schema_profile = SchemaProfile()
        raw_schema = adapter.get_raw_schema()
        # Profiling emits SQL from reflected identifiers.  It still goes
        # through the same AST boundary as user-generated SQL; introspection
        # is trusted metadata, not permission to bypass the execution gate.
        from uada.pipeline.sql_validator import SQLValidator
        validator = SQLValidator(
            allowed_tables=set(allowed_tables),
            inject_limit=True,
            default_limit=_SAMPLE_LIMIT,
        )

        for table_info in raw_schema.tables:
            tname = table_info.name
            if tname not in allowed_tables:
                continue
            try:
                tp = self._profile_table(adapter, table_info, timeout_s, validator)
                schema_profile.tables[tname] = tp
            except Exception as exc:
                logger.warning("DataProfiler: skipping table %s — %s", tname, exc)

        return schema_profile

    def _profile_table(self, adapter: DatabaseAdapter, table_info, timeout_s: int, validator) -> TableProfile:
        from uada.db.interface import QueryExecutionError, QueryTimeoutError

        tname = table_info.name

        # Row count
        row_count: int | None = None
        try:
            rc = self._execute(adapter, validator, f"SELECT COUNT(*) AS __n FROM {self._quote(tname)}", timeout_s, 1)
            if rc.rows:
                row_count = int(rc.rows[0][0] or 0)
        except (QueryExecutionError, QueryTimeoutError) as e:
            logger.debug("Row count failed for %s: %s", tname, e)

        col_profiles: list[ColumnProfile] = []
        for col in table_info.columns:
            cp = self._profile_column(adapter, tname, col, timeout_s, validator)
            col_profiles.append(cp)

        return TableProfile(table_name=tname, row_count=row_count, columns=col_profiles)

    def _profile_column(self, adapter: DatabaseAdapter, tname: str, col, timeout_s: int, validator) -> ColumnProfile:
        from uada.db.interface import QueryExecutionError, QueryTimeoutError

        cname = col.name
        qtable = self._quote(tname)
        qcolumn = self._quote(cname)
        dtype = col.data_type or "unknown"
        is_temporal = any(t in dtype.lower() for t in ("date", "time", "timestamp"))

        cp = ColumnProfile(table_name=tname, column_name=cname, data_type=dtype, is_temporal=is_temporal)

        # Null% + distinct count
        try:
            sql = (
                f"SELECT "
                f"  COUNT(*) AS __total, "
                f"  COUNT({qcolumn}) AS __non_null, "
                f"  COUNT(DISTINCT {qcolumn}) AS __distinct "
                f"FROM {qtable}"
            )
            r = self._execute(adapter, validator, sql, timeout_s, 1)
            if r.rows:
                row = r.rows[0]
                total = int(row[0] or 0)
                non_null = int(row[1] or 0)
                cp.null_pct = round(1 - non_null / total, 4) if total else None
                cp.distinct_count = int(row[2] or 0)
        except Exception as e:
            logger.debug("Null/distinct failed for %s.%s: %s", tname, cname, e)

        # Min / max (skip for non-comparable types)
        if dtype.lower() not in ("json", "jsonb", "bytea", "blob", "text", "clob"):
            try:
                sql = (
                    f"SELECT MIN({qcolumn}) AS __min, MAX({qcolumn}) AS __max FROM {qtable}"
                )
                r = self._execute(adapter, validator, sql, timeout_s, 1)
                if r.rows:
                    cp.min_value = str(r.rows[0][0]) if r.rows[0][0] is not None else None
                    cp.max_value = str(r.rows[0][1]) if r.rows[0][1] is not None else None
            except Exception as e:
                logger.debug("Min/max failed for %s.%s: %s", tname, cname, e)

        # Sample values — held in memory only, never in prompts/logs
        try:
            sql = (
                f"SELECT DISTINCT {qcolumn} AS __v FROM {qtable} WHERE {qcolumn} IS NOT NULL"
            )
            r = self._execute(adapter, validator, sql, timeout_s, _SAMPLE_LIMIT)
            cp.sample_values = [str(row[0]) for row in r.rows if row and row[0] is not None]
        except Exception as e:
            logger.debug("Samples failed for %s.%s: %s", tname, cname, e)

        return cp

    @staticmethod
    def _quote(identifier: str) -> str:
        """Quote an introspected identifier; reject malformed names early."""
        if not identifier or "\x00" in identifier or '"' in identifier:
            raise ValueError("Invalid database identifier")
        return '"' + identifier.replace('"', '""') + '"'

    @staticmethod
    def _execute(adapter, validator, sql: str, timeout_s: int, max_rows: int):
        validation = validator.validate(sql, dialect=adapter.dialect)
        if not validation.is_safe:
            raise PermissionError("Profiler query failed SQL security validation")
        return adapter.execute_query(
            validation.normalised_sql or sql,
            timeout_seconds=timeout_s,
            max_rows=max_rows,
        )
