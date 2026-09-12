"""
AnalyticalIntent — Intermediate Representation
===============================================
The AnalyticalIntent is UADA's central intermediate representation.
It sits between the user's natural language question and SQL generation.

Design rationale (ADR-004):
- Enables dialect-agnostic query planning (same intent → different SQL per dialect)
- Enables explicit follow-up resolution instead of implicit prompt injection
- Enables independent evaluation of intent accuracy vs SQL accuracy
- Enables ambiguity detection before SQL generation begins
- Makes the reasoning path inspectable and traceable

This model is the output of the Analytical Intent Extractor (pipeline step 3)
and the input to the Query Planner (pipeline step 4).

LLM contract: The intent extractor LLM must produce JSON that validates
against this model. PydanticAI enforces this with output_type=AnalyticalIntent
and auto-retries on validation failure.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


# ── Enumerations ──────────────────────────────────────────────────────────────


class QuestionType(str, Enum):
    """
    Classification of the user's analytical intent.
    Used by the Query Planner to select the appropriate query shape.
    """

    AGGREGATION = "aggregation"
    """Single metric, single time point. "What was total revenue last quarter?" """

    TIME_SERIES = "time_series"
    """Metric over time buckets. "Show monthly revenue for the last 12 months." """

    COMPARISON = "comparison"
    """Two or more periods or segments side by side. "Compare this year vs last year." """

    RANKING = "ranking"
    """Ordered top-N or bottom-N. "Top 10 regions by revenue." """

    FILTER = "filter"
    """Apply criteria to a prior or new query. "Only enterprise customers." """

    DIAGNOSTIC = "diagnostic"
    """Why/explain question. "Why did revenue decline in March?" """

    FOLLOW_UP_REFINE = "follow_up_refine"
    """Narrows the previous query. Adds filters or constraints."""

    FOLLOW_UP_EXTEND = "follow_up_extend"
    """Extends the previous query. Adds time comparison, new dimension, etc."""

    FOLLOW_UP_EXPLAIN = "follow_up_explain"
    """Asks for explanation of previous result. Triggers diagnostic path."""

    OUT_OF_SCOPE = "out_of_scope"
    """Question cannot be answered from the connected database."""

    AMBIGUOUS = "ambiguous"
    """Question is unclear. Triggers a clarification request to the user."""


class AnalysisOperation(str, Enum):
    """Deterministic analytical work requested after the governed query executes.

    These values are deliberately separate from ``QuestionType``.  A question can
    have one query shape (for example, a monthly time series) and request several
    analytical operations (trend, anomaly detection, and forecasting).  Keeping
    that distinction explicit prevents UI phrases from selecting an ad-hoc
    response builder.
    """

    DESCRIPTIVE = "descriptive"
    COMPARISON = "comparison"
    TREND = "trend"
    DISTRIBUTION = "distribution"
    CORRELATION = "correlation"
    REGRESSION = "regression"
    ANOMALY = "anomaly"
    FORECAST = "forecast"
    CONTRIBUTION = "contribution"
    DRIVER_ANALYSIS = "driver_analysis"
    EXECUTIVE_SUMMARY = "executive_summary"


class TimeRangeType(str, Enum):
    ALL = "all"
    """Use the full available history while still allowing a time bucket."""

    RELATIVE = "relative"
    """Relative to current date/time. "last quarter", "last 12 months"."""

    ABSOLUTE = "absolute"
    """Explicit date range. "January 2025 to March 2025"."""

    FISCAL = "fiscal"
    """Relative to fiscal calendar defined in SCL. "last fiscal year"."""


class RelativePeriod(str, Enum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    THIS_QUARTER = "this_quarter"
    LAST_QUARTER = "last_quarter"
    THIS_YEAR = "this_year"
    LAST_YEAR = "last_year"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    LAST_90_DAYS = "last_90_days"
    LAST_12_MONTHS = "last_12_months"
    LAST_N_MONTHS = "last_n_months"  # requires period_count
    LAST_N_DAYS = "last_n_days"  # requires period_count


class TimeBucket(str, Enum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class FilterOperator(str, Enum):
    EQUALS = "="
    NOT_EQUALS = "!="
    GREATER_THAN = ">"
    GREATER_THAN_OR_EQUAL = ">="
    LESS_THAN = "<"
    LESS_THAN_OR_EQUAL = "<="
    IN = "in"
    NOT_IN = "not_in"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


# ── Sub-models ────────────────────────────────────────────────────────────────


class TimeRange(BaseModel):
    """Specifies the time window for the query."""

    range_type: TimeRangeType
    relative_period: RelativePeriod | None = None
    period_count: int | None = Field(
        default=None,
        ge=1,
        description="Used with LAST_N_MONTHS / LAST_N_DAYS.",
    )
    start_date: str | None = Field(
        default=None,
        description="ISO 8601 date string. Used with ABSOLUTE range type.",
    )
    end_date: str | None = Field(
        default=None,
        description="ISO 8601 date string. Used with ABSOLUTE range type.",
    )
    bucket: TimeBucket | None = Field(
        default=None,
        description="Time grouping bucket for TIME_SERIES questions.",
    )

    @model_validator(mode="after")
    def validate_range_consistency(self) -> "TimeRange":
        if self.range_type == TimeRangeType.RELATIVE and self.relative_period is None:
            raise ValueError("relative_period required when range_type is RELATIVE")
        if self.range_type == TimeRangeType.ABSOLUTE:
            if self.start_date is None or self.end_date is None:
                raise ValueError("start_date and end_date required when range_type is ABSOLUTE")
            from datetime import date, datetime

            def _parse_boundary(value: str) -> datetime:
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    try:
                        return datetime.combine(date.fromisoformat(value), datetime.min.time())
                    except ValueError as exc:
                        raise ValueError(
                            "Absolute time boundaries must be ISO-8601 dates or timestamps"
                        ) from exc

            start = _parse_boundary(self.start_date)
            end = _parse_boundary(self.end_date)
            if (start.tzinfo is None) != (end.tzinfo is None):
                from datetime import UTC

                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
            if end <= start:
                raise ValueError("end_date must be later than start_date")
        return self


class TimeComparison(BaseModel):
    """Describes a comparison against a prior period."""

    comparison_period: RelativePeriod
    period_count: int | None = None
    comparison_label: str = Field(
        description="Human-readable label for the comparison period, e.g. 'Last Year'."
    )
    comparison_start_date: str | None = Field(
        default=None,
        description="Optional absolute comparison start boundary for explicit windows.",
    )
    comparison_end_date: str | None = Field(
        default=None,
        description="Optional absolute comparison end boundary for explicit windows.",
    )


class SemanticFilter(BaseModel):
    """
    A single filter condition expressed in semantic (business) terms.
    The Query Planner resolves these to SQL WHERE clauses using the SCL glossary.
    """

    entity: str = Field(
        description="Semantic entity name, e.g. 'customer_segment', 'region', 'status'."
    )
    operator: FilterOperator
    value: str | int | float | list[str] | None = Field(
        default=None,
        description="Filter value. None for IS_NULL / IS_NOT_NULL operators.",
    )
    glossary_term: str | None = Field(
        default=None,
        description="If this filter was resolved from a glossary term (e.g. 'enterprise customer'), "
        "record the original term here for traceability.",
    )


class OrderClause(BaseModel):
    """Specifies sort order for ranking queries."""

    measure_or_dimension: str
    direction: SortDirection = SortDirection.DESC


class Ambiguity(BaseModel):
    """Records a detected ambiguity and how it was resolved (or not)."""

    description: str = Field(description="Human-readable description of the ambiguity.")
    resolution: str | None = Field(
        default=None,
        description="How the ambiguity was resolved, if at all. "
        "E.g. 'Used 90-day definition of active customer per SCL glossary.'",
    )
    requires_clarification: bool = Field(
        default=False,
        description="True if UADA cannot resolve this and needs to ask the user.",
    )


# ── Primary Model ─────────────────────────────────────────────────────────────


class AnalyticalIntent(BaseModel):
    """
    Structured representation of what the user wants to compute.

    This is the output of pipeline step 3 (Analytical Intent Extractor)
    and the input to pipeline step 4 (Query Planner).

    LLM contract: PydanticAI maps LLM output to this model.
    Validation failures trigger ModelRetry (max settings.llm_max_retries).
    """

    # ── Classification ────────────────────────────────────────────────────────
    question_type: QuestionType = Field(
        description="Primary classification of the analytical question."
    )

    # ── What is being measured ────────────────────────────────────────────────
    measures: list[str] = Field(
        default_factory=list,
        description="Metric names as defined in the SCL, e.g. ['revenue', 'order_count']. "
        "Empty for DIAGNOSTIC or OUT_OF_SCOPE.",
    )

    # ── How it is grouped ─────────────────────────────────────────────────────
    dimensions: list[str] = Field(
        default_factory=list,
        description="Dimension names as defined in the SCL, e.g. ['region', 'customer_segment']. "
        "Empty for simple aggregations.",
    )

    # ── Time context ──────────────────────────────────────────────────────────
    time_dimension: str | None = Field(
        default=None,
        description="The column or SCL time dimension to use for filtering/grouping. "
        "E.g. 'order_date'. Resolved from SCL default_time_column if not explicit.",
    )
    time_range: TimeRange | None = Field(
        default=None,
        description="The time window for the query. None means no time filtering.",
    )
    time_comparison: TimeComparison | None = Field(
        default=None,
        description="If the question involves comparing two periods, describes the comparison period.",
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    filters: list[SemanticFilter] = Field(
        default_factory=list,
        description="Semantic filters to apply. Resolved to SQL WHERE clauses by the Query Planner.",
    )

    # ── Ordering and limits ───────────────────────────────────────────────────
    order_by: list[OrderClause] | None = Field(
        default=None,
        description="Sort specification. Populated for RANKING questions.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
        description="Result row limit. Populated for RANKING questions (e.g. 'top 10').",
    )

    # ── Multi-turn context ────────────────────────────────────────────────────
    references_prior_turn: bool = Field(
        default=False,
        description="True if this question refers to or modifies a previous query in this session.",
    )
    prior_turn_id: int | None = Field(
        default=None,
        description="The turn_id of the ConversationTurn being referenced or modified.",
    )
    follow_up_description: str | None = Field(
        default=None,
        description="Human-readable description of how this turn modifies the prior turn. "
        "E.g. 'Adds enterprise customer filter to previous revenue by region query.'",
    )

    # ── Ambiguity and confidence ──────────────────────────────────────────────
    ambiguities: list[Ambiguity] = Field(
        default_factory=list,
        description="Detected ambiguities and their resolutions.",
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=1.0,
        description="Confidence score for the extracted intent. "
        "Below 0.5: triggers clarification. Below 0.3: returns out_of_scope.",
    )
    clarification_question: str | None = Field(
        default=None,
        description="If confidence is low, the question to ask the user for clarification.",
    )

    # ── Deterministic analytical work ────────────────────────────────────────
    analysis_operations: list[AnalysisOperation] = Field(
        default_factory=list,
        description="Statistical operations to execute over the validated query result.",
    )
    forecast_horizon: int | None = Field(
        default=None,
        ge=1,
        le=120,
        description="Number of time buckets to forecast when FORECAST is requested.",
    )
    confidence_level: Annotated[float, Field(gt=0.5, lt=1.0)] = Field(
        default=0.95,
        description="Confidence level for intervals and inferential tests.",
    )
    analysis_profile: str | None = Field(
        default=None,
        description="Optional governed semantic analysis profile selected for the question.",
    )

    # ── Raw input (for tracing) ───────────────────────────────────────────────
    raw_question: str = Field(
        description="The original user question, preserved for tracing and evaluation."
    )

    @model_validator(mode="after")
    def validate_intent_consistency(self) -> "AnalyticalIntent":
        """Cross-field validation rules."""
        # OUT_OF_SCOPE and AMBIGUOUS should not have measures
        if self.question_type in (QuestionType.OUT_OF_SCOPE, QuestionType.AMBIGUOUS):
            if self.measures:
                raise ValueError(f"question_type={self.question_type} should not have measures.")

        # RANKING should have order_by
        if self.question_type == QuestionType.RANKING and self.order_by is None:
            raise ValueError("RANKING question_type requires order_by.")

        # Follow-up types should reference a prior turn
        follow_up_types = {
            QuestionType.FOLLOW_UP_REFINE,
            QuestionType.FOLLOW_UP_EXTEND,
            QuestionType.FOLLOW_UP_EXPLAIN,
        }
        if self.question_type in follow_up_types and not self.references_prior_turn:
            raise ValueError(
                f"question_type={self.question_type} requires references_prior_turn=True."
            )

        # TIME_SERIES requires a bucket
        if self.question_type == QuestionType.TIME_SERIES:
            if self.time_range is None or self.time_range.bucket is None:
                raise ValueError("TIME_SERIES question_type requires time_range with a bucket.")

        if AnalysisOperation.FORECAST in self.analysis_operations and self.forecast_horizon is None:
            raise ValueError("FORECAST analysis operation requires forecast_horizon.")

        return self
