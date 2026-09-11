"""
Complexity Tier Models
======================
Classifies an analytical question into one of four tiers, used by the
ComplexityRouter to adapt SQL generation strategy and decide whether to
engage the Investigation Agent state machine (Step 3).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ComplexityTier(str, Enum):
    """
    Four-tier classification for incoming analytical questions.

    SIMPLE
        Point lookup or direct filter — single table, no aggregation, no joins.
        Example: "What was revenue for product X in January 2024?"

    ANALYTICAL
        Standard aggregations (SUM/AVG/COUNT) with GROUP BY / ORDER BY.
        One primary table; at most one straightforward join.
        Example: "Total revenue by region for Q3."

    COMPLEX
        Multi-table joins (2+), OR SQL window functions (RANK, LAG, LEAD, etc.),
        OR CTEs required, OR multi-metric comparisons in a single query.
        Example: "Month-over-month growth rate by product category."

    VERY_COMPLEX
        Multi-hop analysis, correlated subqueries, self-joins, complex CTE
        chains, year-over-year same-period comparisons, rolling aggregates,
        or cohort analysis.
        Example: "Which products had above-average growth in Q4 vs prior year Q4?"
    """

    SIMPLE = "simple"
    ANALYTICAL = "analytical"
    COMPLEX = "complex"
    VERY_COMPLEX = "very_complex"


class ComplexityDecision(BaseModel):
    """Output schema for the ComplexityRouter PydanticAI agent."""

    tier: ComplexityTier = Field(
        description="The complexity tier assigned to this question.",
    )
    rationale: str = Field(
        max_length=250,
        description="One-sentence justification for the tier choice.",
    )
    key_signals: list[str] = Field(
        min_length=1,
        max_length=3,
        description="1-3 specific words or phrases from the question that drove the classification.",
    )
    estimated_join_count: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Estimated number of SQL JOINs required (0 for SIMPLE).",
    )
    requires_window_function: bool = Field(
        default=False,
        description="True when the question likely requires a SQL window function.",
    )
