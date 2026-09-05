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

        for table_info in raw_schema.tables:
            tname = table_info.name
            if tname not in allowed_tables:
                continue
            try:
                tp = self._profile_table(adapter, table_info, timeout_s)
                schema_profile.tables[tname] = tp
            except Exception as exc:
                logger.warning("DataProfiler: skipping table %s — %s", tname, exc)

        return schema_profile

    def _profile_table(self, adapter: DatabaseAdapter, table_info, timeout_s: int) -> TableProfile:
        from uada.db.interface import QueryExecutionError, QueryTimeoutError

        tname = table_info.name

        # Row count
        row_count: int | None = None
        try:
            rc = adapter.execute_query(
                f"SELECT COUNT(*) AS __n FROM {tname}",
                timeout_seconds=timeout_s,
                max_rows=1,
            )
            if rc.rows:
                row_count = int(rc.rows[0].get("__n", 0))
        except (QueryExecutionError, QueryTimeoutError) as e:
            logger.debug("Row count failed for %s: %s", tname, e)

        col_profiles: list[ColumnProfile] = []
        for col in table_info.columns:
            cp = self._profile_column(adapter, tname, col, timeout_s)
            col_profiles.append(cp)

        return TableProfile(table_name=tname, row_count=row_count, columns=col_profiles)

    def _profile_column(self, adapter: DatabaseAdapter, tname: str, col, timeout_s: int) -> ColumnProfile:
        from uada.db.interface import QueryExecutionError, QueryTimeoutError

        cname = col.name
        dtype = col.data_type or "unknown"
        is_temporal = any(t in dtype.lower() for t in ("date", "time", "timestamp"))

        cp = ColumnProfile(table_name=tname, column_name=cname, data_type=dtype, is_temporal=is_temporal)

        # Null% + distinct count
        try:
            sql = (
                f"SELECT "
                f"  COUNT(*) AS __total, "
                f"  COUNT({cname}) AS __non_null, "
                f"  COUNT(DISTINCT {cname}) AS __distinct "
                f"FROM {tname}"
            )
            r = adapter.execute_query(sql, timeout_seconds=timeout_s, max_rows=1)
            if r.rows:
                row = r.rows[0]
                total = int(row.get("__total") or 0)
                non_null = int(row.get("__non_null") or 0)
                cp.null_pct = round(1 - non_null / total, 4) if total else None
                cp.distinct_count = int(row.get("__distinct") or 0)
        except (QueryExecutionError, QueryTimeoutError, Exception) as e:
            logger.debug("Null/distinct failed for %s.%s: %s", tname, cname, e)

        # Min / max (skip for non-comparable types)
        if dtype.lower() not in ("json", "jsonb", "bytea", "blob", "text", "clob"):
            try:
                r = adapter.execute_query(
                    f"SELECT MIN({cname}) AS __min, MAX({cname}) AS __max FROM {tname}",
                    timeout_seconds=timeout_s,
                    max_rows=1,
                )
                if r.rows:
                    cp.min_value = str(r.rows[0].get("__min", "")) or None
                    cp.max_value = str(r.rows[0].get("__max", "")) or None
            except Exception as e:
                logger.debug("Min/max failed for %s.%s: %s", tname, cname, e)

        # Sample values — held in memory only, never in prompts/logs
        try:
            r = adapter.execute_query(
                f"SELECT DISTINCT {cname} AS __v FROM {tname} WHERE {cname} IS NOT NULL LIMIT {_SAMPLE_LIMIT}",
                timeout_seconds=timeout_s,
                max_rows=_SAMPLE_LIMIT,
            )
            cp.sample_values = [str(row.get("__v", "")) for row in r.rows if row.get("__v") is not None]
        except Exception as e:
            logger.debug("Samples failed for %s.%s: %s", tname, cname, e)

        return cp
