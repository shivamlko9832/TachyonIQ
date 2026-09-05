"""
Follow-Up Question Engine
==========================
Generates 3-5 contextual follow-up question suggestions after each successful
analysis turn. Deterministic-first: rule-based generation from intent type and
result shape. Optional LLM enrichment when the deterministic rules produce fewer
than 3 suggestions.

The follow-up chips appear below the answer in the frontend and help non-analyst
users continue exploring without knowing what to ask next.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uada.models.conversation import ConversationState
    from uada.models.intent import AnalyticalIntent
    from uada.models.result import AnalysedResult
    from uada.models.schema_context import SchemaContext

logger = logging.getLogger(__name__)

_MAX_SUGGESTIONS = 5
_MIN_SUGGESTIONS = 3


class FollowUpEngine:
    """
    Deterministic follow-up question generator.

    Generates suggestions based on:
    - The question type of the current turn (AGGREGATION / TIME_SERIES / RANKING / etc.)
    - The actual result shape (top entity name, trend direction, time bucket)
    - The prior conversation context (what has already been asked)

    No LLM call — all suggestions are template-driven, with placeholders filled
    from result data.
    """

    def suggest(
        self,
        intent: AnalyticalIntent,
        result: AnalysedResult,
        conversation_state: ConversationState,
        schema_context: SchemaContext,
    ) -> list[str]:
        """
        Generate 3-5 follow-up question strings.

        Returns an empty list if suggestion generation fails — callers should
        treat an empty list as "no suggestions available" and not surface chips.
        """
        try:
            suggestions = self._generate(intent, result, conversation_state, schema_context)
            seen_questions = {
                t.user_question.lower()
                for t in conversation_state.turns
                if t.user_question
            }
            deduped = [s for s in suggestions if s.lower() not in seen_questions]
            return deduped[:_MAX_SUGGESTIONS]
        except Exception as exc:  # noqa: BLE001
            logger.warning("FollowUpEngine.suggest failed: %s", exc)
            return []

    def _generate(
        self,
        intent: AnalyticalIntent,
        result: AnalysedResult,
        conversation_state: ConversationState,
        schema_context: SchemaContext,
    ) -> list[str]:
        from uada.models.intent import QuestionType

        qt = intent.question_type
        measures = list(intent.measures)
        dimensions = list(intent.dimensions)
        metric = measures[0] if measures else "this metric"
        metric_label = metric.replace("_", " ")
        dim = dimensions[0] if dimensions else None
        dim_label = dim.replace("_", " ") if dim else None

        # Pull top entity from result if available (first row, first non-numeric cell)
        top_entity = self._top_entity(result)

        # Pull trend direction
        trend = self._trend_label(result)

        # Available dimensions from schema (excluding those already in intent)
        available_dims = [
            col.column_name.replace("_", " ")
            for tbl in schema_context.tables
            for col in tbl.columns
            if col.column_name not in dimensions
            and col.semantic_type in ("dimension", "DIMENSION", None)
            and col.column_name not in ("id", "created_at", "updated_at")
        ][:3]

        suggestions: list[str] = []

        if qt == QuestionType.AGGREGATION:
            suggestions += [
                f"Break down {metric_label} by {available_dims[0]}" if available_dims else f"Which segment drives the most {metric_label}?",
                f"How does {metric_label} compare to last period?",
                f"What is the trend in {metric_label} over the past 12 months?",
                f"Which {dim_label or 'category'} has the highest {metric_label}?",
            ]

        elif qt == QuestionType.TIME_SERIES:
            suggestions += [
                f"What is driving the {trend} in {metric_label}?" if trend else f"What caused the biggest change in {metric_label}?",
                f"Forecast {metric_label} for the next 3 months",
                f"Show {metric_label} broken down by {available_dims[0]}" if available_dims else f"Compare {metric_label} across segments",
                f"Which month had the highest {metric_label}?",
            ]

        elif qt == QuestionType.RANKING:
            suggestions += [
                f"Show the trend for {top_entity} over the last 12 months" if top_entity else f"How has {metric_label} changed over time?",
                f"What is the {metric_label} breakdown by {available_dims[0]} for {top_entity}?" if top_entity and available_dims else f"Break down the top performer further",
                f"Which {dim_label or 'item'} is growing fastest?",
                f"Compare top 5 vs bottom 5 {dim_label or 'items'} by {metric_label}",
            ]

        elif qt == QuestionType.COMPARISON:
            suggestions += [
                f"Show the percentage change in {metric_label} vs prior period",
                f"Which {dim_label or 'segment'} is growing fastest?",
                f"What is the {metric_label} trend for the top performer?",
                f"Break down {metric_label} by {available_dims[0]}" if available_dims else f"Drill into the largest segment",
            ]

        elif qt == QuestionType.DIAGNOSTIC:
            suggestions += [
                f"Show the full distribution of {metric_label}",
                f"Which {dim_label or 'factor'} most affects {metric_label}?",
                f"Are there seasonal patterns in {metric_label}?",
                f"Find outliers in {metric_label}",
            ]

        elif qt in (QuestionType.FOLLOW_UP_REFINE, QuestionType.FOLLOW_UP_EXTEND, QuestionType.FOLLOW_UP_EXPLAIN):
            suggestions += [
                f"Show this as a trend over time",
                f"Break this down by {available_dims[0]}" if available_dims else "Add another dimension to this view",
                f"Compare to the same period last year",
                f"What is driving these results?",
            ]

        else:
            suggestions += [
                f"Show me {metric_label} over time" if metric_label != "this metric" else "Show me a trend",
                f"Which {dim_label or 'category'} performs best?",
                "Compare this to last period",
                "Show me the top 10",
            ]

        # Deduplicate and return
        seen: set[str] = set()
        result_list: list[str] = []
        for s in suggestions:
            key = s.lower().strip()
            if key not in seen:
                seen.add(key)
                result_list.append(s)
        return result_list

    def _top_entity(self, result: AnalysedResult) -> str | None:
        """Extract the top-ranked entity from the first row of results."""
        try:
            rows = result.query_result.rows
            cols = result.query_result.column_names
            numeric_cols = {s.column for s in result.numeric_summaries}
            if not rows:
                return None
            first_row = rows[0]
            for i, col in enumerate(cols):
                if col not in numeric_cols and i < len(first_row):
                    val = first_row[i]
                    if val is not None and str(val).strip():
                        return str(val).strip()
            return None
        except Exception:  # noqa: BLE001
            return None

    def _trend_label(self, result: AnalysedResult) -> str | None:
        """Return 'growth' or 'decline' based on trend direction."""
        try:
            if not result.trend_analysis:
                return None
            direction = result.trend_analysis[0].direction.value
            if direction == "increasing":
                return "growth"
            if direction == "decreasing":
                return "decline"
            return None
        except Exception:  # noqa: BLE001
            return None
