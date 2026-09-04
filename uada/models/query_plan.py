"""
QueryPlan — Dialect-Agnostic Query Plan
========================================
The QueryPlan is the output of the Query Planner (pipeline step 4).
It is a fully resolved, dialect-agnostic description of the SQL query
that the SQL Generator (step 5) must produce.

Key properties:
- All semantic references are resolved (metric formulas, glossary terms, join paths).
- All time semantics are resolved into a TimeResolution with dialect-specific hints.
- The plan is serializable: it can be logged, traced, diffed, and evaluated.
- The plan is complete: the SQL Generator should not need to consult the SCL again.
- The plan is dialect-aware: it carries the target dialect so the generator
  can apply dialect-specific SQL syntax.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enumerations ──────────────────────────────────────────────────────────────


class JoinType(str, Enum):
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL = "FULL"


class AggregationFunction(str, Enum):
    SUM = "SUM"
    AVG = "AVG"
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    MIN = "MIN"
    MAX = "MAX"
    MEDIAN = "MEDIAN"
    STDDEV = "STDDEV"


class SQLDialect(str, Enum):
    """Supported SQL dialects. Maps to SQLGlot dialect names."""
    POSTGRESQL = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    TSQL = "tsql"        # SQL Server
    DUCKDB = "duckdb"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"


# ── Sub-models ────────────────────────────────────────────────────────────────


class ResolvedTable(BaseModel):
    """A table included in the query, as resolved from the SCL."""

    table_name: str
    alias: str | None = None
    schema_name: str | None = None  # For databases with schemas/namespaces


class ResolvedJoin(BaseModel):
    """A JOIN between two tables, as resolved from the SCL join paths."""

    from_table: str
    to_table: str
    join_type: JoinType = JoinType.LEFT
    condition: str = Field(
        description="The ON condition as a SQL string fragment. "
        "E.g. 'orders.customer_id = customers.customer_id'."
    )


class ResolvedMeasure(BaseModel):
    """
    A metric, fully resolved from the SCL.
    The metric's formula is expanded here so the SQL Generator
    does not need to look up the SCL.
    """

    name: str = Field(description="The semantic name of the metric, e.g. 'revenue'.")
    sql_expression: str = Field(
        description="The SQL expression for this measure. "
        "E.g. 'SUM(orders.revenue)' or a complex formula. "
        "Already expanded from the SCL metric definition."
    )
    aggregation: AggregationFunction | None = None
    output_alias: str = Field(description="The column alias in the SELECT clause.")
    unit: str | None = None


class ResolvedDimension(BaseModel):
    """A grouping dimension, resolved from the SCL."""

    name: str
    sql_expression: str = Field(
        description="The SQL expression for this dimension. "
        "E.g. 'customers.region' or 'DATE_TRUNC(\\\"month\\\", orders.order_date)'."
    )
    output_alias: str


class TimeResolution(BaseModel):
    """
    Fully resolved time filter/grouping, with dialect-specific SQL.
    The Query Planner resolves relative periods to dialect-appropriate SQL fragments.
    """

    time_column: str = Field(description="Fully qualified time column, e.g. 'orders.order_date'.")
    dialect: SQLDialect

    # Filter clause
    filter_sql: str | None = Field(
        default=None,
        description="SQL WHERE fragment for the time filter. "
        "Dialect-specific. E.g. PostgreSQL: "
        "\"order_date >= DATE_TRUNC('quarter', NOW() - INTERVAL '3 months')\""
        " vs SQLite: \"order_date >= date('now', '-3 months')\"",
    )

    # Grouping clause (for TIME_SERIES)
    group_by_sql: str | None = Field(
        default=None,
        description="SQL GROUP BY + SELECT fragment for time bucketing. "
        "E.g. PostgreSQL: \"DATE_TRUNC('month', order_date) AS period\" "
        "vs MySQL: \"DATE_FORMAT(order_date, '%Y-%m') AS period\"",
    )

    # Comparison period (for COMPARISON questions)
    comparison_filter_sql: str | None = Field(
        default=None,
        description="SQL WHERE fragment for the comparison period, if applicable.",
    )
    comparison_label: str | None = None


class ResolvedFilter(BaseModel):
    """A WHERE clause condition, fully resolved from the SCL glossary."""

    sql_fragment: str = Field(
        description="Ready-to-use SQL fragment. "
        "E.g. \"customers.segment = 'Enterprise'\". "
        "Resolved from glossary term or direct filter."
    )
    original_semantic_filter: str = Field(
        description="The original semantic entity/value for tracing. "
        "E.g. 'customer_segment = Enterprise'."
    )


class OrderByClause(BaseModel):
    sql_expression: str
    direction: str = "DESC"  # "ASC" or "DESC"


# ── Primary Model ─────────────────────────────────────────────────────────────


class QueryPlan(BaseModel):
    """
    A fully resolved, dialect-agnostic description of the analytical query.

    The SQL Generator (step 5) receives this plan and produces SQL from it.
    The plan must be self-contained: the generator should not need to
    consult the SCL, the retrieval engine, or any other external resource.

    Serialization: This model is JSON-serializable and is included in
    Langfuse traces for every query.
    """

    # ── Origin ────────────────────────────────────────────────────────────────
    intent_question_type: str = Field(
        description="The QuestionType from the originating AnalyticalIntent."
    )
    original_question: str = Field(
        description="The original user question, for tracing."
    )
    dialect: SQLDialect = Field(
        description="Target SQL dialect. Determines which SQL syntax the generator produces."
    )

    # ── FROM clause ───────────────────────────────────────────────────────────
    primary_table: ResolvedTable
    additional_tables: list[ResolvedTable] = Field(default_factory=list)
    joins: list[ResolvedJoin] = Field(default_factory=list)

    # ── SELECT clause ─────────────────────────────────────────────────────────
    measures: list[ResolvedMeasure] = Field(
        default_factory=list,
        description="Metrics to compute. At least one measure OR dimension required.",
    )
    dimensions: list[ResolvedDimension] = Field(
        default_factory=list,
        description="Grouping dimensions in the SELECT and GROUP BY clauses.",
    )

    # ── Time ──────────────────────────────────────────────────────────────────
    time_resolution: TimeResolution | None = Field(
        default=None,
        description="Fully resolved time filter and grouping. "
        "None if the query has no time dimension.",
    )

    # ── WHERE clause ──────────────────────────────────────────────────────────
    filters: list[ResolvedFilter] = Field(
        default_factory=list,
        description="WHERE clause conditions, fully resolved from SCL glossary.",
    )

    # ── ORDER BY / LIMIT ──────────────────────────────────────────────────────
    order_by: list[OrderByClause] = Field(default_factory=list)
    limit: int | None = None

    # ── Comparison (for COMPARISON question type) ─────────────────────────────
    is_comparison: bool = False
    comparison_cte_name: str | None = Field(
        default=None,
        description="If this is a period-over-period comparison, the CTE name "
        "for the comparison period subquery.",
    )

    # ── Follow-up linkage ─────────────────────────────────────────────────────
    is_follow_up: bool = False
    base_plan_summary: str | None = Field(
        default=None,
        description="Summary of the prior query plan this plan modifies. "
        "Used by the SQL Generator to understand the diff.",
    )

    # ── Metadata ──────────────────────────────────────────────────────────────
    plan_notes: list[str] = Field(
        default_factory=list,
        description="Planner notes for the SQL Generator. "
        "E.g. 'Use fiscal quarter definition from SCL (Q1 starts April).'",
    )
    estimated_complexity: str = Field(
        default="simple",
        description="Planner's estimate: 'simple', 'moderate', 'complex'. "
        "Used to guide SQL Generator prompt selection.",
    )

    def to_generator_context(self) -> dict[str, Any]:
        """
        Returns a condensed dict for injection into the SQL Generator prompt.
        Strips fields that are not useful to the LLM.
        """
        return {
            "dialect": self.dialect.value,
            "primary_table": self.primary_table.table_name,
            "joins": [
                f"{j.join_type.value} JOIN {j.to_table} ON {j.condition}"
                for j in self.joins
            ],
            "select": (
                [m.sql_expression + f" AS {m.output_alias}" for m in self.measures]
                + [d.sql_expression + f" AS {d.output_alias}" for d in self.dimensions]
            ),
            "time_filter": self.time_resolution.filter_sql if self.time_resolution else None,
            "time_group_by": self.time_resolution.group_by_sql if self.time_resolution else None,
            "where": [f.sql_fragment for f in self.filters],
            "order_by": [
                f"{o.sql_expression} {o.direction}" for o in self.order_by
            ],
            "limit": self.limit,
            "notes": self.plan_notes,
        }
