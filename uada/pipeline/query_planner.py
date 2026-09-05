"""
Query Planner (pipeline step 4)
==================================
Deterministic. Resolves an `AnalyticalIntent` (semantic, dialect-agnostic)
into a `QueryPlan` (fully resolved, dialect-aware, self-contained) using
the SCL. The SQL Generator (step 5) should never need to consult the SCL,
the schema linker, or the retrieval engine -- everything it needs is
already expanded into the plan.

No LLM calls here. Time-range resolution is the bulk of the module: each
`RelativePeriod` needs a dialect-specific SQL fragment, since PostgreSQL,
MySQL, SQLite, and SQL Server have no common date-truncation syntax.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from uada.models.intent import FilterOperator, RelativePeriod, TimeBucket, TimeRangeType
from uada.models.query_plan import (
    JoinType,
    OrderByClause,
    QueryPlan,
    ResolvedDimension,
    ResolvedFilter,
    ResolvedJoin,
    ResolvedMeasure,
    ResolvedTable,
    SQLDialect,
    TimeResolution,
)

if TYPE_CHECKING:
    from uada.models.intent import AnalyticalIntent, SemanticFilter, TimeComparison, TimeRange
    from uada.models.schema_context import SchemaContext
    from uada.scl.manager import SCLManager

logger = logging.getLogger(__name__)

# SchemaContext.dialect comes from the SCL's SQLDialect (SQLAlchemy-style
# names: "postgresql", "mysql", "sqlite", "tsql", ...). QueryPlan.dialect
# uses SQLGlot-style names ("postgres" for Postgres, everything else the
# same). Only the Postgres spelling differs.
_SCHEMA_DIALECT_TO_QUERY_PLAN_DIALECT: dict[str, str] = {"postgresql": "postgres"}

_TABLE_COLUMN_PATTERN = re.compile(r"\b(\w+)\.\w+")


def _first_table_reference(sql_expression: str) -> str | None:
    """Extract the first 'table.column'-shaped reference's table name, if any."""
    match = _TABLE_COLUMN_PATTERN.search(sql_expression)
    return match.group(1) if match else None


def _sql_literal(value: Any) -> str:
    """Render a Python value as a SQL literal (quoted string, bare number, NULL)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


# ── Per-dialect relative-period filters ────────────────────────────────────────
# Each returns a WHERE-clause fragment for `column` given a RelativePeriod.
# `count` is only used by LAST_N_MONTHS / LAST_N_DAYS.


def _postgres_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= NOW() - INTERVAL '{count} months'"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= NOW() - INTERVAL '{count} days'"

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: (
            f"{column} >= CURRENT_DATE AND {column} < CURRENT_DATE + INTERVAL '1 day'"
        ),
        RelativePeriod.YESTERDAY: (
            f"{column} >= CURRENT_DATE - INTERVAL '1 day' AND {column} < CURRENT_DATE"
        ),
        RelativePeriod.THIS_WEEK: f"{column} >= DATE_TRUNC('week', NOW())",
        RelativePeriod.LAST_WEEK: (
            f"{column} >= DATE_TRUNC('week', NOW() - INTERVAL '1 week') "
            f"AND {column} < DATE_TRUNC('week', NOW())"
        ),
        RelativePeriod.THIS_MONTH: f"{column} >= DATE_TRUNC('month', NOW())",
        RelativePeriod.LAST_MONTH: (
            f"{column} >= DATE_TRUNC('month', NOW() - INTERVAL '1 month') "
            f"AND {column} < DATE_TRUNC('month', NOW())"
        ),
        RelativePeriod.THIS_QUARTER: f"{column} >= DATE_TRUNC('quarter', NOW())",
        RelativePeriod.LAST_QUARTER: (
            f"{column} >= DATE_TRUNC('quarter', NOW() - INTERVAL '3 months') "
            f"AND {column} < DATE_TRUNC('quarter', NOW())"
        ),
        RelativePeriod.THIS_YEAR: f"{column} >= DATE_TRUNC('year', NOW())",
        RelativePeriod.LAST_YEAR: (
            f"{column} >= DATE_TRUNC('year', NOW() - INTERVAL '1 year') "
            f"AND {column} < DATE_TRUNC('year', NOW())"
        ),
        RelativePeriod.LAST_7_DAYS: f"{column} >= NOW() - INTERVAL '7 days'",
        RelativePeriod.LAST_30_DAYS: f"{column} >= NOW() - INTERVAL '30 days'",
        RelativePeriod.LAST_90_DAYS: f"{column} >= NOW() - INTERVAL '90 days'",
        RelativePeriod.LAST_12_MONTHS: f"{column} >= NOW() - INTERVAL '12 months'",
    }
    return templates[period]


def _mysql_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= DATE_SUB(CURDATE(), INTERVAL {count} MONTH)"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= DATE_SUB(CURDATE(), INTERVAL {count} DAY)"

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: f"{column} >= CURDATE()",
        RelativePeriod.YESTERDAY: (
            f"{column} >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND {column} < CURDATE()"
        ),
        RelativePeriod.THIS_WEEK: (
            f"{column} >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)"
        ),
        RelativePeriod.LAST_WEEK: (
            f"{column} >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE())+7 DAY) "
            f"AND {column} < DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)"
        ),
        RelativePeriod.THIS_MONTH: f"{column} >= DATE_FORMAT(CURDATE(), '%Y-%m-01')",
        RelativePeriod.LAST_MONTH: (
            f"{column} >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') "
            f"AND {column} < DATE_FORMAT(CURDATE(), '%Y-%m-01')"
        ),
        RelativePeriod.THIS_QUARTER: (
            f"{column} >= MAKEDATE(YEAR(CURDATE()), 1) "
            f"+ INTERVAL (QUARTER(CURDATE()) - 1) * 3 MONTH"
        ),
        # Given directly by the phase spec: a rolling 3-month window, not a
        # calendar-quarter truncation.
        RelativePeriod.LAST_QUARTER: f"{column} >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH)",
        RelativePeriod.THIS_YEAR: f"{column} >= MAKEDATE(YEAR(CURDATE()), 1)",
        RelativePeriod.LAST_YEAR: (
            f"{column} >= MAKEDATE(YEAR(CURDATE()) - 1, 1) "
            f"AND {column} < MAKEDATE(YEAR(CURDATE()), 1)"
        ),
        RelativePeriod.LAST_7_DAYS: f"{column} >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)",
        RelativePeriod.LAST_30_DAYS: f"{column} >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)",
        RelativePeriod.LAST_90_DAYS: f"{column} >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)",
        RelativePeriod.LAST_12_MONTHS: f"{column} >= DATE_SUB(CURDATE(), INTERVAL 12 MONTH)",
    }
    return templates[period]


def _sqlite_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= date('now', '-{count} months')"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= date('now', '-{count} days')"

    # Days since the most recent Monday: strftime('%w') is 0=Sunday..6=Saturday.
    week_start = "date('now', '-' || ((strftime('%w','now') + 6) % 7) || ' days')"
    last_week_start = "date('now', '-' || ((strftime('%w','now') + 6) % 7 + 7) || ' days')"
    quarter_start = (
        "date(date('now','start of month'), "
        "'-' || ((strftime('%m','now') - 1) % 3) || ' months')"
    )

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: f"{column} >= date('now', 'start of day')",
        RelativePeriod.YESTERDAY: (
            f"{column} >= date('now', 'start of day', '-1 day') "
            f"AND {column} < date('now', 'start of day')"
        ),
        RelativePeriod.THIS_WEEK: f"{column} >= {week_start}",
        RelativePeriod.LAST_WEEK: (
            f"{column} >= {last_week_start} AND {column} < {week_start}"
        ),
        RelativePeriod.THIS_MONTH: f"{column} >= date('now', 'start of month')",
        RelativePeriod.LAST_MONTH: (
            f"{column} >= date('now', 'start of month', '-1 month') "
            f"AND {column} < date('now', 'start of month')"
        ),
        RelativePeriod.THIS_QUARTER: f"{column} >= {quarter_start}",
        # Given directly by the phase spec: a rolling 3-month window.
        RelativePeriod.LAST_QUARTER: f"{column} >= date('now', '-3 months')",
        RelativePeriod.THIS_YEAR: f"{column} >= date('now', 'start of year')",
        RelativePeriod.LAST_YEAR: (
            f"{column} >= date('now', 'start of year', '-1 year') "
            f"AND {column} < date('now', 'start of year')"
        ),
        RelativePeriod.LAST_7_DAYS: f"{column} >= date('now', '-7 days')",
        RelativePeriod.LAST_30_DAYS: f"{column} >= date('now', '-30 days')",
        RelativePeriod.LAST_90_DAYS: f"{column} >= date('now', '-90 days')",
        RelativePeriod.LAST_12_MONTHS: f"{column} >= date('now', '-12 months')",
    }
    return templates[period]


def _tsql_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= DATEADD(month, -{count}, GETDATE())"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= DATEADD(day, -{count}, GETDATE())"

    today = "CAST(GETDATE() AS DATE)"
    week_start = "DATEADD(week, DATEDIFF(week, 0, GETDATE()), 0)"
    month_start = "DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)"
    quarter_start = "DATEADD(quarter, DATEDIFF(quarter, 0, GETDATE()), 0)"
    year_start = "DATEFROMPARTS(YEAR(GETDATE()), 1, 1)"

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: f"{column} >= {today}",
        RelativePeriod.YESTERDAY: (
            f"{column} >= DATEADD(day, -1, {today}) AND {column} < {today}"
        ),
        RelativePeriod.THIS_WEEK: f"{column} >= {week_start}",
        RelativePeriod.LAST_WEEK: (
            f"{column} >= DATEADD(week, -1, {week_start}) AND {column} < {week_start}"
        ),
        RelativePeriod.THIS_MONTH: f"{column} >= {month_start}",
        RelativePeriod.LAST_MONTH: (
            f"{column} >= DATEADD(month, -1, {month_start}) AND {column} < {month_start}"
        ),
        RelativePeriod.THIS_QUARTER: f"{column} >= {quarter_start}",
        # Given directly by the phase spec: a rolling 3-month window, not a
        # calendar-quarter truncation.
        RelativePeriod.LAST_QUARTER: f"{column} >= DATEADD(quarter, -1, GETDATE())",
        RelativePeriod.THIS_YEAR: f"{column} >= {year_start}",
        RelativePeriod.LAST_YEAR: (
            f"{column} >= DATEADD(year, -1, {year_start}) AND {column} < {year_start}"
        ),
        RelativePeriod.LAST_7_DAYS: f"{column} >= DATEADD(day, -7, GETDATE())",
        RelativePeriod.LAST_30_DAYS: f"{column} >= DATEADD(day, -30, GETDATE())",
        RelativePeriod.LAST_90_DAYS: f"{column} >= DATEADD(day, -90, GETDATE())",
        RelativePeriod.LAST_12_MONTHS: f"{column} >= DATEADD(month, -12, GETDATE())",
    }
    return templates[period]



def _duckdb_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    # DuckDB follows PostgreSQL syntax almost exactly; DATE_TRUNC + INTERVAL work the same.
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= NOW() - INTERVAL '{count} months'"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= NOW() - INTERVAL '{count} days'"

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: (
            f"{column} >= CURRENT_DATE AND {column} < CURRENT_DATE + INTERVAL '1 day'"
        ),
        RelativePeriod.YESTERDAY: (
            f"{column} >= CURRENT_DATE - INTERVAL '1 day' AND {column} < CURRENT_DATE"
        ),
        RelativePeriod.THIS_WEEK: f"{column} >= DATE_TRUNC('week', NOW())",
        RelativePeriod.LAST_WEEK: (
            f"{column} >= DATE_TRUNC('week', NOW() - INTERVAL '1 week') "
            f"AND {column} < DATE_TRUNC('week', NOW())"
        ),
        RelativePeriod.THIS_MONTH: f"{column} >= DATE_TRUNC('month', NOW())",
        RelativePeriod.LAST_MONTH: (
            f"{column} >= DATE_TRUNC('month', NOW() - INTERVAL '1 month') "
            f"AND {column} < DATE_TRUNC('month', NOW())"
        ),
        RelativePeriod.THIS_QUARTER: f"{column} >= DATE_TRUNC('quarter', NOW())",
        RelativePeriod.LAST_QUARTER: (
            f"{column} >= DATE_TRUNC('quarter', NOW() - INTERVAL '3 months') "
            f"AND {column} < DATE_TRUNC('quarter', NOW())"
        ),
        RelativePeriod.THIS_YEAR: f"{column} >= DATE_TRUNC('year', NOW())",
        RelativePeriod.LAST_YEAR: (
            f"{column} >= DATE_TRUNC('year', NOW() - INTERVAL '1 year') "
            f"AND {column} < DATE_TRUNC('year', NOW())"
        ),
        RelativePeriod.LAST_7_DAYS: f"{column} >= NOW() - INTERVAL '7 days'",
        RelativePeriod.LAST_30_DAYS: f"{column} >= NOW() - INTERVAL '30 days'",
        RelativePeriod.LAST_90_DAYS: f"{column} >= NOW() - INTERVAL '90 days'",
        RelativePeriod.LAST_12_MONTHS: f"{column} >= NOW() - INTERVAL '12 months'",
    }
    return templates[period]


def _snowflake_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    # Snowflake uses DATEADD / DATE_TRUNC with unit as first string arg.
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= DATEADD(month, -{count}, CURRENT_TIMESTAMP())"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= DATEADD(day, -{count}, CURRENT_TIMESTAMP())"

    week_start = "DATE_TRUNC('week', CURRENT_DATE())"
    month_start = "DATE_TRUNC('month', CURRENT_DATE())"
    quarter_start = "DATE_TRUNC('quarter', CURRENT_DATE())"
    year_start = "DATE_TRUNC('year', CURRENT_DATE())"

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: f"{column} >= CURRENT_DATE()",
        RelativePeriod.YESTERDAY: (
            f"{column} >= DATEADD(day, -1, CURRENT_DATE()) "
            f"AND {column} < CURRENT_DATE()"
        ),
        RelativePeriod.THIS_WEEK: f"{column} >= {week_start}",
        RelativePeriod.LAST_WEEK: (
            f"{column} >= DATEADD(week, -1, {week_start}) "
            f"AND {column} < {week_start}"
        ),
        RelativePeriod.THIS_MONTH: f"{column} >= {month_start}",
        RelativePeriod.LAST_MONTH: (
            f"{column} >= DATEADD(month, -1, {month_start}) "
            f"AND {column} < {month_start}"
        ),
        RelativePeriod.THIS_QUARTER: f"{column} >= {quarter_start}",
        RelativePeriod.LAST_QUARTER: (
            f"{column} >= DATEADD(month, -3, {quarter_start}) "
            f"AND {column} < {quarter_start}"
        ),
        RelativePeriod.THIS_YEAR: f"{column} >= {year_start}",
        RelativePeriod.LAST_YEAR: (
            f"{column} >= DATEADD(year, -1, {year_start}) "
            f"AND {column} < {year_start}"
        ),
        RelativePeriod.LAST_7_DAYS: f"{column} >= DATEADD(day, -7, CURRENT_TIMESTAMP())",
        RelativePeriod.LAST_30_DAYS: f"{column} >= DATEADD(day, -30, CURRENT_TIMESTAMP())",
        RelativePeriod.LAST_90_DAYS: f"{column} >= DATEADD(day, -90, CURRENT_TIMESTAMP())",
        RelativePeriod.LAST_12_MONTHS: f"{column} >= DATEADD(month, -12, CURRENT_TIMESTAMP())",
    }
    return templates[period]


def _bigquery_period_filter(column: str, period: RelativePeriod, count: int | None) -> str:
    # BigQuery uses DATE_TRUNC(date_expr, granularity) and DATE_SUB / TIMESTAMP_SUB.
    # We emit DATE_SUB on CURRENT_DATE() for rolling windows and DATE_TRUNC for
    # calendar-aligned boundaries.  BigQuery is case-sensitive on function names
    # (they are all UPPERCASE per convention).
    if period == RelativePeriod.LAST_N_MONTHS:
        return f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL {count} MONTH)"
    if period == RelativePeriod.LAST_N_DAYS:
        return f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL {count} DAY)"

    templates: dict[RelativePeriod, str] = {
        RelativePeriod.TODAY: f"{column} >= CURRENT_DATE()",
        RelativePeriod.YESTERDAY: (
            f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) "
            f"AND {column} < CURRENT_DATE()"
        ),
        RelativePeriod.THIS_WEEK: (
            f"{column} >= DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))"
        ),
        RelativePeriod.LAST_WEEK: (
            f"{column} >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)), INTERVAL 1 WEEK) "
            f"AND {column} < DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))"
        ),
        RelativePeriod.THIS_MONTH: (
            f"{column} >= DATE_TRUNC(CURRENT_DATE(), MONTH)"
        ),
        RelativePeriod.LAST_MONTH: (
            f"{column} >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH) "
            f"AND {column} < DATE_TRUNC(CURRENT_DATE(), MONTH)"
        ),
        RelativePeriod.THIS_QUARTER: (
            f"{column} >= DATE_TRUNC(CURRENT_DATE(), QUARTER)"
        ),
        RelativePeriod.LAST_QUARTER: (
            f"{column} >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 QUARTER), QUARTER) "
            f"AND {column} < DATE_TRUNC(CURRENT_DATE(), QUARTER)"
        ),
        RelativePeriod.THIS_YEAR: (
            f"{column} >= DATE_TRUNC(CURRENT_DATE(), YEAR)"
        ),
        RelativePeriod.LAST_YEAR: (
            f"{column} >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 YEAR), YEAR) "
            f"AND {column} < DATE_TRUNC(CURRENT_DATE(), YEAR)"
        ),
        RelativePeriod.LAST_7_DAYS: (
            f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"
        ),
        RelativePeriod.LAST_30_DAYS: (
            f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"
        ),
        RelativePeriod.LAST_90_DAYS: (
            f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)"
        ),
        RelativePeriod.LAST_12_MONTHS: (
            f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)"
        ),
    }
    return templates[period]


_PERIOD_FILTER_FUNCS = {
    SQLDialect.POSTGRESQL: _postgres_period_filter,
    SQLDialect.MYSQL: _mysql_period_filter,
    SQLDialect.SQLITE: _sqlite_period_filter,
    SQLDialect.TSQL: _tsql_period_filter,
    SQLDialect.DUCKDB: _duckdb_period_filter,
    SQLDialect.SNOWFLAKE: _snowflake_period_filter,
    SQLDialect.BIGQUERY: _bigquery_period_filter,
}


# ── Per-dialect time-bucket grouping ────────────────────────────────────────────


def _postgres_bucket(column: str, bucket: TimeBucket) -> str:
    return f"DATE_TRUNC('{bucket.value}', {column}) AS period"


def _mysql_bucket(column: str, bucket: TimeBucket) -> str:
    formats: dict[TimeBucket, str] = {
        TimeBucket.HOUR: f"DATE_FORMAT({column}, '%Y-%m-%d %H:00:00')",
        TimeBucket.DAY: f"DATE_FORMAT({column}, '%Y-%m-%d')",
        TimeBucket.WEEK: (
            f"DATE_FORMAT(DATE_SUB({column}, INTERVAL WEEKDAY({column}) DAY), '%Y-%m-%d')"
        ),
        TimeBucket.MONTH: f"DATE_FORMAT({column}, '%Y-%m')",
        TimeBucket.QUARTER: f"CONCAT(YEAR({column}), '-Q', QUARTER({column}))",
        TimeBucket.YEAR: f"DATE_FORMAT({column}, '%Y')",
    }
    return f"{formats[bucket]} AS period"


def _sqlite_bucket(column: str, bucket: TimeBucket) -> str:
    formats: dict[TimeBucket, str] = {
        TimeBucket.HOUR: f"strftime('%Y-%m-%d %H:00:00', {column})",
        TimeBucket.DAY: f"strftime('%Y-%m-%d', {column})",
        TimeBucket.WEEK: f"strftime('%Y-%W', {column})",
        TimeBucket.MONTH: f"strftime('%Y-%m', {column})",
        TimeBucket.QUARTER: (
            f"(strftime('%Y', {column}) || '-Q' || "
            f"((CAST(strftime('%m', {column}) AS INTEGER) - 1) / 3 + 1))"
        ),
        TimeBucket.YEAR: f"strftime('%Y', {column})",
    }
    return f"{formats[bucket]} AS period"


def _tsql_bucket(column: str, bucket: TimeBucket) -> str:
    formats: dict[TimeBucket, str] = {
        TimeBucket.HOUR: f"DATEADD(hour, DATEDIFF(hour, 0, {column}), 0)",
        TimeBucket.DAY: f"CAST({column} AS DATE)",
        TimeBucket.WEEK: f"DATEADD(week, DATEDIFF(week, 0, {column}), 0)",
        TimeBucket.MONTH: f"DATEFROMPARTS(YEAR({column}), MONTH({column}), 1)",
        TimeBucket.QUARTER: f"DATEADD(quarter, DATEDIFF(quarter, 0, {column}), 0)",
        TimeBucket.YEAR: f"DATEFROMPARTS(YEAR({column}), 1, 1)",
    }
    return f"{formats[bucket]} AS period"




def _duckdb_bucket(column: str, bucket: TimeBucket) -> str:
    # DuckDB DATE_TRUNC uses the same unit strings as PostgreSQL.
    return f"DATE_TRUNC('{bucket.value}', {column}) AS period"


def _snowflake_bucket(column: str, bucket: TimeBucket) -> str:
    # Snowflake DATE_TRUNC: DATE_TRUNC('unit', expr).
    # QUARTER is supported; HOUR requires TIMESTAMP input.
    return f"DATE_TRUNC('{bucket.value}', {column}) AS period"


def _bigquery_bucket(column: str, bucket: TimeBucket) -> str:
    # BigQuery DATE_TRUNC(date_expr, granularity) — granularity is an unquoted keyword.
    # WEEK → WEEK(MONDAY) for ISO-week alignment.
    bq_unit: dict[TimeBucket, str] = {
        TimeBucket.HOUR: "HOUR",      # requires TIMESTAMP_TRUNC for TIMESTAMP cols
        TimeBucket.DAY: "DAY",
        TimeBucket.WEEK: "WEEK(MONDAY)",
        TimeBucket.MONTH: "MONTH",
        TimeBucket.QUARTER: "QUARTER",
        TimeBucket.YEAR: "YEAR",
    }
    unit = bq_unit[bucket]
    if bucket == TimeBucket.HOUR:
        # TIMESTAMP_TRUNC is correct for TIMESTAMP columns; DATE_TRUNC for DATE.
        # We emit TIMESTAMP_TRUNC as the safer choice for analytics columns.
        return f"TIMESTAMP_TRUNC({column}, {unit}) AS period"
    return f"DATE_TRUNC({column}, {unit}) AS period"

_BUCKET_FUNCS = {
    SQLDialect.POSTGRESQL: _postgres_bucket,
    SQLDialect.MYSQL: _mysql_bucket,
    SQLDialect.SQLITE: _sqlite_bucket,
    SQLDialect.TSQL: _tsql_bucket,
    SQLDialect.DUCKDB: _duckdb_bucket,
    SQLDialect.SNOWFLAKE: _snowflake_bucket,
    SQLDialect.BIGQUERY: _bigquery_bucket,
}


class QueryPlanner:
    """Pipeline step 4: AnalyticalIntent -> QueryPlan. Pure deterministic logic."""

    def __init__(self, scl_manager: SCLManager) -> None:
        self._scl_manager = scl_manager

    def plan(self, intent: AnalyticalIntent, schema_context: SchemaContext) -> QueryPlan:
        """Resolve `intent` into a fully self-contained QueryPlan."""
        dialect = self._resolve_dialect(schema_context.dialect)

        measures = [self._resolve_measure(name, schema_context) for name in intent.measures]
        dimensions = [
            self._resolve_dimension(name, schema_context) for name in intent.dimensions
        ]

        primary_table_name = self._resolve_primary_table(measures, dimensions, schema_context)

        filters = [
            self._resolve_filter(f, primary_table_name, schema_context) for f in intent.filters
        ]

        additional_tables, joins = self._resolve_joins(
            primary_table_name, measures, dimensions, filters
        )

        time_resolution = None
        if intent.time_range is not None:
            time_column = intent.time_dimension or self._find_default_time_column(
                schema_context, primary_table_name
            )
            if time_column is None:
                logger.warning("Time range given but no time column could be resolved.")
            else:
                qualified_column = (
                    time_column if "." in time_column else f"{primary_table_name}.{time_column}"
                )
                time_resolution = self._resolve_time(
                    intent.time_range, qualified_column, dialect, intent.time_comparison
                )

        order_by = [
            OrderByClause(
                sql_expression=self._resolve_order_expression(
                    o.measure_or_dimension, measures, dimensions
                ),
                direction=o.direction.value.upper(),
            )
            for o in (intent.order_by or [])
        ]

        is_comparison = intent.question_type.value == "comparison"

        plan = QueryPlan(
            intent_question_type=intent.question_type.value,
            original_question=intent.raw_question,
            dialect=dialect,
            primary_table=ResolvedTable(table_name=primary_table_name),
            additional_tables=additional_tables,
            joins=joins,
            measures=measures,
            dimensions=dimensions,
            time_resolution=time_resolution,
            filters=filters,
            order_by=order_by,
            limit=intent.limit,
            is_comparison=is_comparison,
            comparison_cte_name="comparison_period" if is_comparison else None,
            is_follow_up=intent.references_prior_turn,
            base_plan_summary=intent.follow_up_description,
            estimated_complexity=self._estimate_complexity(joins, time_resolution, is_comparison),
        )
        logger.info(
            "Query planned: dialect=%s, primary_table=%s, %d measure(s), %d dimension(s), "
            "%d join(s).",
            dialect.value,
            primary_table_name,
            len(measures),
            len(dimensions),
            len(joins),
        )
        return plan

    # ── Dialect ──────────────────────────────────────────────────────────────

    def _resolve_dialect(self, schema_dialect: str) -> SQLDialect:
        mapped = _SCHEMA_DIALECT_TO_QUERY_PLAN_DIALECT.get(schema_dialect, schema_dialect)
        try:
            return SQLDialect(mapped)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported dialect '{schema_dialect}' for query planning. "
                f"Supported: {[d.value for d in SQLDialect]}"
            ) from exc

    # ── Primary table ────────────────────────────────────────────────────────

    def _resolve_primary_table(
        self,
        measures: list[ResolvedMeasure],
        dimensions: list[ResolvedDimension],
        schema_context: SchemaContext,
    ) -> str:
        for measure in measures:
            table = _first_table_reference(measure.sql_expression)
            if table is not None:
                return table
        for dimension in dimensions:
            table = _first_table_reference(dimension.sql_expression)
            if table is not None:
                return table
        if schema_context.tables:
            return schema_context.tables[0].table_name
        raise ValueError("Cannot plan a query: no tables available in the schema context.")

    def _find_default_time_column(
        self, schema_context: SchemaContext, primary_table: str
    ) -> str | None:
        """
        Resolve a bare default time column name when the intent didn't
        specify `time_dimension` explicitly.

        Prefers the *primary* table's own configured default time column
        (since that's the table the measures/dimensions actually come
        from -- it may differ from the first retrieved table), then any
        other retrieved table's, then finally SchemaContext's own
        schema-wide default (set by the Schema Linker from the highest-
        scored retrieved table, which may not be the primary table here).
        """
        for table in schema_context.tables:
            if table.table_name != primary_table:
                continue
            for column in table.columns:
                if column.is_default_time_column:
                    return column.column_name
        for table in schema_context.tables:
            for column in table.columns:
                if column.is_default_time_column:
                    return column.column_name
        return schema_context.default_time_column

    # ── Measures ─────────────────────────────────────────────────────────────

    def _resolve_measure(self, name: str, schema_context: SchemaContext) -> ResolvedMeasure:
        metric = self._scl_manager.resolve_metric(name)
        if metric is not None:
            return ResolvedMeasure(
                name=metric.name,
                sql_expression=metric.formula,
                output_alias=metric.name,
                unit=metric.unit,
            )

        fallback_table = schema_context.tables[0].table_name if schema_context.tables else name
        logger.warning(
            "Measure '%s' not found in SCL; falling back to SUM(%s.%s).",
            name,
            fallback_table,
            name,
        )
        return ResolvedMeasure(
            name=name,
            sql_expression=f"SUM({fallback_table}.{name})",
            output_alias=name,
        )

    # ── Dimensions ───────────────────────────────────────────────────────────

    def _resolve_dimension(self, name: str, schema_context: SchemaContext) -> ResolvedDimension:
        for table in schema_context.tables:
            for column in table.columns:
                if column.column_name == name:
                    return ResolvedDimension(
                        name=name,
                        sql_expression=f"{table.table_name}.{column.column_name}",
                        output_alias=name,
                    )
        fallback_table = schema_context.tables[0].table_name if schema_context.tables else name
        logger.warning(
            "Dimension '%s' not found in any retrieved table; falling back to %s.%s.",
            name,
            fallback_table,
            name,
        )
        return ResolvedDimension(
            name=name, sql_expression=f"{fallback_table}.{name}", output_alias=name
        )

    def _resolve_order_expression(
        self,
        name: str,
        measures: list[ResolvedMeasure],
        dimensions: list[ResolvedDimension],
    ) -> str:
        for measure in measures:
            if measure.name == name:
                return measure.output_alias
        for dimension in dimensions:
            if dimension.name == name:
                return dimension.output_alias
        return name

    # ── Joins ────────────────────────────────────────────────────────────────

    def _resolve_joins(
        self,
        primary_table: str,
        measures: list[ResolvedMeasure],
        dimensions: list[ResolvedDimension],
        filters: list[ResolvedFilter],
    ) -> tuple[list[ResolvedTable], list[ResolvedJoin]]:
        """
        Determine which additional tables must be joined to the primary
        table, and the join for each.

        A table is "referenced" if a measure, dimension, *or filter*
        qualifies a column with it -- a WHERE-clause filter resolved from
        a glossary term (e.g. 'customers.segment = ...' while the primary
        table is 'orders') needs its table joined in exactly as much as a
        SELECTed measure or dimension would.
        """
        referenced_tables: set[str] = set()
        for measure in measures:
            table = _first_table_reference(measure.sql_expression)
            if table is not None:
                referenced_tables.add(table)
        for dimension in dimensions:
            table = _first_table_reference(dimension.sql_expression)
            if table is not None:
                referenced_tables.add(table)
        for filt in filters:
            referenced_tables.update(_TABLE_COLUMN_PATTERN.findall(filt.sql_fragment))
        referenced_tables.discard(primary_table)

        additional_tables: list[ResolvedTable] = []
        joins: list[ResolvedJoin] = []
        for other_table in sorted(referenced_tables):
            join_def = self._scl_manager.get_join_path(primary_table, other_table)
            if join_def is None:
                logger.warning(
                    "No SCL join path between '%s' and '%s'; table referenced without a join.",
                    primary_table,
                    other_table,
                )
                continue
            additional_tables.append(ResolvedTable(table_name=other_table))
            joins.append(
                ResolvedJoin(
                    from_table=join_def.from_table,
                    to_table=join_def.to_table,
                    join_type=JoinType(join_def.join_type.upper()),
                    condition=join_def.on,
                )
            )
        return additional_tables, joins

    # ── Time ─────────────────────────────────────────────────────────────────

    def _resolve_time(
        self,
        time_range: TimeRange,
        column: str,
        dialect: SQLDialect,
        time_comparison: TimeComparison | None = None,
    ) -> TimeResolution:
        """Resolve a TimeRange (and optional TimeComparison) into dialect-specific SQL."""
        filter_sql: str | None = None
        if time_range.range_type == TimeRangeType.RELATIVE:
            assert time_range.relative_period is not None  # enforced by TimeRange's own validator
            filter_sql = self._period_filter(
                dialect, column, time_range.relative_period, time_range.period_count
            )
        elif time_range.range_type == TimeRangeType.ABSOLUTE:
            filter_sql = (
                f"{column} >= '{time_range.start_date}' AND {column} < '{time_range.end_date}'"
            )
        else:
            logger.warning("TimeRangeType.FISCAL is not yet supported; skipping time filter.")

        group_by_sql = None
        if time_range.bucket is not None:
            group_by_sql = _BUCKET_FUNCS[dialect](column, time_range.bucket)

        comparison_filter_sql = None
        comparison_label = None
        if time_comparison is not None:
            comparison_filter_sql = self._period_filter(
                dialect, column, time_comparison.comparison_period, time_comparison.period_count
            )
            comparison_label = time_comparison.comparison_label

        return TimeResolution(
            time_column=column,
            dialect=dialect,
            filter_sql=filter_sql,
            group_by_sql=group_by_sql,
            comparison_filter_sql=comparison_filter_sql,
            comparison_label=comparison_label,
        )

    def _period_filter(
        self,
        dialect: SQLDialect,
        column: str,
        period: RelativePeriod,
        period_count: int | None,
    ) -> str:
        return _PERIOD_FILTER_FUNCS[dialect](column, period, period_count)

    # ── Filters ──────────────────────────────────────────────────────────────

    def _resolve_filter(
        self,
        filt: SemanticFilter,
        primary_table: str,
        schema_context: SchemaContext,
    ) -> ResolvedFilter:
        original = f"{filt.entity} {filt.operator.value} {filt.value}"

        if filt.glossary_term:
            term = self._scl_manager.resolve_glossary_term(filt.glossary_term)
            if term is not None and term.sql_filter:
                return ResolvedFilter(
                    sql_fragment=term.sql_filter, original_semantic_filter=original
                )
            logger.warning(
                "Glossary term '%s' not found or has no sql_filter; "
                "falling back to a direct filter.",
                filt.glossary_term,
            )

        column_expr = self._resolve_dimension(filt.entity, schema_context).sql_expression
        return ResolvedFilter(
            sql_fragment=self._build_filter_fragment(column_expr, filt),
            original_semantic_filter=original,
        )

    def _build_filter_fragment(self, column_expr: str, filt: SemanticFilter) -> str:
        if filt.operator == FilterOperator.IS_NULL:
            return f"{column_expr} IS NULL"
        if filt.operator == FilterOperator.IS_NOT_NULL:
            return f"{column_expr} IS NOT NULL"
        if filt.operator in (FilterOperator.IN, FilterOperator.NOT_IN):
            values = filt.value if isinstance(filt.value, list) else [filt.value]
            rendered = ", ".join(_sql_literal(v) for v in values)
            keyword = "IN" if filt.operator == FilterOperator.IN else "NOT IN"
            return f"{column_expr} {keyword} ({rendered})"
        if filt.operator == FilterOperator.LIKE:
            return f"{column_expr} LIKE {_sql_literal(filt.value)}"
        # EQUALS/NOT_EQUALS/comparisons: the enum's own value is the SQL operator.
        return f"{column_expr} {filt.operator.value} {_sql_literal(filt.value)}"

    # ── Complexity heuristic ─────────────────────────────────────────────────

    def _estimate_complexity(
        self,
        joins: list[ResolvedJoin],
        time_resolution: TimeResolution | None,
        is_comparison: bool,
    ) -> str:
        if is_comparison or len(joins) >= 2:
            return "complex"
        if joins or time_resolution is not None:
            return "moderate"
        return "simple"
