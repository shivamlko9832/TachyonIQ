"""
Semantic Context Layer — Pydantic Schema
=========================================
The SCL is UADA's primary mechanism for providing semantic grounding.
It is stored as a YAML file, version-controlled, and human-curated.

Structure mirrors Cube data-model conventions (ADR for SCL) so that
migration to Cube Core as a production semantic layer is non-breaking.

The models here define the YAML schema. The loader (scl/loader.py)
parses and validates the YAML against these models.

YAML → SemanticContextLayer (root)
           ├── DatabaseMeta
           ├── list[TableDefinition]
           │     └── list[ColumnDefinition]
           ├── list[MetricDefinition]
           ├── list[JoinDefinition]
           ├── list[GlossaryTerm]
           ├── list[ExampleQuery]
           └── SecurityPolicy
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


# ── Enumerations ──────────────────────────────────────────────────────────────


class SemanticType(str, Enum):
    MEASURE = "measure"
    DIMENSION = "dimension"
    IDENTIFIER = "identifier"
    TEMPORAL = "temporal"
    TEXT = "text"
    BOOLEAN = "boolean"
    UNKNOWN = "unknown"


class JoinCardinality(str, Enum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class MetricAdditivity(str, Enum):
    """Whether grouped metric values may be safely rolled up by summing them."""

    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    NON_ADDITIVE = "non_additive"


class SQLDialect(str, Enum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    TSQL = "tsql"
    DUCKDB = "duckdb"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"


# ── Column and Table definitions ──────────────────────────────────────────────


class ColumnDefinition(BaseModel):
    """Definition of a single database column."""

    name: str = Field(description="Exact column name as it appears in the database.")
    type: str = Field(description="SQL data type string, e.g. 'integer', 'varchar', 'timestamp'.")
    description: str | None = Field(
        default=None,
        description="Human-readable description of what this column contains.",
    )
    semantic_type: SemanticType = Field(default=SemanticType.UNKNOWN)
    is_primary_key: bool = False
    is_foreign_key: bool = False
    references: str | None = Field(
        default=None,
        description="Foreign key target as 'table.column'. E.g. 'customers.customer_id'.",
    )
    is_temporal: bool = Field(
        default=False,
        description="True if this column contains date/time values.",
    )
    default_time_column: bool = Field(
        default=False,
        description="True if this is the primary time column for this table. "
        "At most one column per table should have this set.",
    )
    aggregation: str | None = Field(
        default=None,
        description="Default aggregation for measure columns. E.g. 'SUM', 'AVG', 'COUNT'.",
    )
    enum_values: list[str] | None = Field(
        default=None,
        description="Allowed values for categorical columns. Used for filter validation.",
    )
    excluded: bool = Field(
        default=False,
        description="If True, this column is excluded from all queries and prompts.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names for this column used in business terminology.",
    )


class TableDefinition(BaseModel):
    """Definition of a database table and its analytical semantics."""

    name: str = Field(description="Exact table name as it appears in the database.")
    description: str | None = Field(
        default=None,
        description="Human-readable description of what this table contains.",
    )
    grain: str | None = Field(
        default=None,
        description="What one row in this table represents. E.g. 'order', 'customer', 'day'.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names for this table used in business terminology. "
        "E.g. ['transactions', 'sales'] for an 'orders' table.",
    )
    excluded: bool = Field(
        default=False,
        description="If True, this table is excluded from all queries and prompts. "
        "Use for audit tables, system tables, or sensitive tables.",
    )
    columns: list[ColumnDefinition] = Field(default_factory=list)

    @property
    def included_columns(self) -> list[ColumnDefinition]:
        """Returns only non-excluded columns."""
        return [c for c in self.columns if not c.excluded]

    @property
    def default_time_column(self) -> ColumnDefinition | None:
        """Returns the default time column for this table, if defined."""
        for col in self.columns:
            if col.default_time_column:
                return col
        return None


# ── Metrics ───────────────────────────────────────────────────────────────────


class MetricDefinition(BaseModel):
    """
    A business metric with its SQL formula.

    Metrics are named, reusable measures that encapsulate business logic.
    The Query Planner resolves metric names to their SQL formulas before
    passing the plan to the SQL Generator.
    """

    name: str = Field(description="Metric name used in AnalyticalIntent, e.g. 'revenue'.")
    description: str = Field(
        description="Human-readable description of what this metric measures."
    )
    formula: str = Field(
        description="SQL expression for the metric. May reference table.column paths. "
        "E.g. 'SUM(orders.revenue)' or a complex CASE expression.",
    )
    filters: str | None = Field(
        default=None,
        description="SQL WHERE condition that must be added when computing this metric. "
        "E.g. \"orders.status != 'cancelled'\".",
    )
    requires_time_comparison: bool = Field(
        default=False,
        description="True if this metric is inherently a period-over-period comparison.",
    )
    grain: str | None = Field(
        default=None,
        description="The grain at which this metric is defined. E.g. 'order'.",
    )
    additivity: MetricAdditivity = Field(
        default=MetricAdditivity.ADDITIVE,
        description="Whether values may be summed across result groups. Distinct counts, "
        "ratios, averages and percentages are normally non-additive.",
    )
    unit: str | None = Field(default=None, description="Unit of the metric. E.g. 'USD', '%'.")
    aliases: list[str] = Field(default_factory=list)


# ── Joins ─────────────────────────────────────────────────────────────────────


class JoinDefinition(BaseModel):
    """
    A join relationship between two tables.

    Join definitions allow the Query Planner to resolve multi-table queries
    without the LLM having to infer join conditions from column names alone.
    """

    from_table: str
    to_table: str
    on: str = Field(
        description="SQL join condition. E.g. 'orders.customer_id = customers.customer_id'."
    )
    join_type: str = Field(default="LEFT", description="JOIN type: INNER, LEFT, RIGHT, FULL.")
    cardinality: JoinCardinality = JoinCardinality.MANY_TO_ONE
    description: str | None = None


# ── Glossary ──────────────────────────────────────────────────────────────────


class GlossaryTerm(BaseModel):
    """
    A business term and its SQL interpretation.

    Glossary terms allow the Query Planner to resolve business language
    ("enterprise customers", "active customers") to SQL filter conditions
    without the LLM having to guess.
    """

    term: str = Field(description="The business term as users say it.")
    description: str = Field(description="What this term means in the context of this database.")
    sql_filter: str | None = Field(
        default=None,
        description="Ready-to-use SQL WHERE fragment for this term. "
        "E.g. \"customers.segment = 'Enterprise'\". "
        "None for terms that are descriptive but don't translate to a single filter.",
    )
    applies_to_table: str | None = Field(
        default=None,
        description="The table this filter applies to. Used for validation.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative ways users may express this term.",
    )


# ── Example Queries ───────────────────────────────────────────────────────────


class ExampleQuery(BaseModel):
    """
    A worked example: question, SQL, and intent classification.

    Examples are indexed in the retrieval store and retrieved as
    few-shot examples for the SQL Generator.
    """

    question: str = Field(description="The natural-language question.")
    sql: str = Field(description="The correct SQL for this question against this database.")
    intent_type: str | None = Field(
        default=None,
        description="The QuestionType classification of this example.",
    )
    description: str | None = Field(
        default=None,
        description="Optional notes about why this query is structured as it is.",
    )
    dialect: str | None = Field(
        default=None,
        description="The dialect of the SQL. If None, assumed to match database dialect.",
    )


# ── Security policy ───────────────────────────────────────────────────────────


class ColumnExclusion(BaseModel):
    table: str
    columns: list[str]


class SecurityPolicy(BaseModel):
    """
    Security configuration for this database connection.

    The SQLValidator uses this policy to build its allowlists.
    """

    excluded_tables: list[str] = Field(
        default_factory=list,
        description="Tables that must never appear in generated SQL or prompts. "
        "E.g. ['audit_log', 'user_passwords', 'api_keys'].",
    )
    excluded_columns: list[ColumnExclusion] = Field(
        default_factory=list,
        description="Columns that must never appear in generated SQL or prompts.",
    )
    row_filters: dict[str, str] = Field(
        default_factory=dict,
        description="Per-table row-level filters applied to every query. "
        "For future use — row-level security implementation.",
    )
    max_query_rows: int | None = Field(
        default=None,
        description="Override the global db_max_rows setting for this database.",
    )


# ── Root model ────────────────────────────────────────────────────────────────


class DatabaseMeta(BaseModel):
    """Top-level metadata for the connected database."""

    name: str = Field(description="Logical name for this database (used in UI and traces).")
    description: str | None = None
    dialect: SQLDialect
    timezone: str = Field(
        default="UTC",
        description="Database timezone. Used for time arithmetic.",
    )
    fiscal_year_start_month: Annotated[int, Field(ge=1, le=12)] = Field(
        default=1,
        description="Month number (1=January) when the fiscal year starts.",
    )


class AnalysisProfileDefinition(BaseModel):
    """A governed, reusable analytical dataset profile.

    Profiles contain semantic names only.  They do not contain answers or
    display values: the normal planner resolves their measures and dimensions,
    and every number is still produced by validated SQL and deterministic
    analysis at request time.
    """

    name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time_dimension: str | None = None
    time_bucket: str | None = None
    operations: list[str] = Field(default_factory=list)
    question_type: str | None = None
    order_by: str | None = None
    order_direction: str = "desc"
    limit: int | None = Field(default=None, ge=1, le=10_000)
    excluded_glossary_filters: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_analysis_contract(self) -> "AnalysisProfileDefinition":
        allowed_operations = {
            "descriptive",
            "comparison",
            "trend",
            "distribution",
            "correlation",
            "regression",
            "anomaly",
            "forecast",
            "contribution",
            "driver_analysis",
            "executive_summary",
        }
        unknown_operations = set(self.operations) - allowed_operations
        if unknown_operations:
            raise ValueError(f"Unknown analytical operations: {sorted(unknown_operations)}")
        allowed_question_types = {
            "aggregation",
            "time_series",
            "comparison",
            "ranking",
            "filter",
            "diagnostic",
        }
        if self.question_type and self.question_type not in allowed_question_types:
            raise ValueError(f"Unknown profile question_type '{self.question_type}'.")
        if self.order_direction not in {"asc", "desc"}:
            raise ValueError("order_direction must be 'asc' or 'desc'.")
        if self.order_by and self.order_by not in {*self.measures, *self.dimensions}:
            raise ValueError("order_by must reference a profile measure or dimension.")
        if self.question_type == "ranking" and not self.order_by:
            raise ValueError("A ranking profile requires order_by.")
        return self


class SemanticContextLayer(BaseModel):
    """
    Root model for the UADA Semantic Context Layer.

    This is the full deserialized form of the SCL YAML file.
    Loaded once at startup and used throughout the pipeline.
    """

    version: str = Field(description="SCL schema version. Current: '1.0'.")
    database: DatabaseMeta
    tables: list[TableDefinition] = Field(default_factory=list)
    metrics: list[MetricDefinition] = Field(default_factory=list)
    joins: list[JoinDefinition] = Field(default_factory=list)
    glossary: list[GlossaryTerm] = Field(default_factory=list)
    examples: list[ExampleQuery] = Field(default_factory=list)
    analysis_profiles: list[AnalysisProfileDefinition] = Field(default_factory=list)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)

    @model_validator(mode="after")
    def validate_references(self) -> "SemanticContextLayer":
        """Validate that join table references exist in the table list."""
        table_names = {t.name for t in self.tables}
        for join in self.joins:
            if join.from_table not in table_names:
                raise ValueError(
                    f"Join references unknown table '{join.from_table}'. "
                    f"Available tables: {sorted(table_names)}"
                )
            if join.to_table not in table_names:
                raise ValueError(
                    f"Join references unknown table '{join.to_table}'. "
                    f"Available tables: {sorted(table_names)}"
                )
        metric_names = {metric.name for metric in self.metrics}
        column_names = {
            column.name for table in self.included_tables for column in table.included_columns
        }
        for profile in self.analysis_profiles:
            unknown_measures = set(profile.measures) - metric_names
            if unknown_measures:
                raise ValueError(
                    f"Analysis profile '{profile.name}' references unknown measures: "
                    f"{sorted(unknown_measures)}"
                )
            unknown_dimensions = set(profile.dimensions) - column_names
            if unknown_dimensions:
                raise ValueError(
                    f"Analysis profile '{profile.name}' references unknown dimensions: "
                    f"{sorted(unknown_dimensions)}"
                )
            if profile.time_dimension and profile.time_dimension not in column_names:
                raise ValueError(
                    f"Analysis profile '{profile.name}' references unknown time dimension "
                    f"'{profile.time_dimension}'."
                )
        return self

    @property
    def included_tables(self) -> list[TableDefinition]:
        """Tables not in the security exclusion list."""
        excluded = set(self.security.excluded_tables)
        return [t for t in self.tables if t.name not in excluded and not t.excluded]

    def get_table(self, name: str) -> TableDefinition | None:
        for t in self.tables:
            if t.name == name:
                return t
        return None

    def get_metric(self, name: str) -> MetricDefinition | None:
        for m in self.metrics:
            if m.name == name or name in m.aliases:
                return m
        return None

    def get_glossary_term(self, term: str) -> GlossaryTerm | None:
        term_lower = term.lower()
        for g in self.glossary:
            if g.term.lower() == term_lower or term_lower in [a.lower() for a in g.aliases]:
                return g
        return None

    def get_allowed_tables(self) -> set[str]:
        """Returns the set of table names permitted in SQL queries."""
        return {t.name for t in self.included_tables}
