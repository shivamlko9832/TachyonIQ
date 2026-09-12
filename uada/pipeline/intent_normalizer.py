"""Deterministic normalization of the LLM's analytical intent.

The LLM interprets language; this module enforces executable semantics.  It
does not produce SQL, statistics, answers, or values.  Its output remains the
typed ``AnalyticalIntent`` consumed by the normal query planner.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from uada.models.intent import (
    AnalysisOperation,
    AnalyticalIntent,
    OrderClause,
    QuestionType,
    SortDirection,
    TimeBucket,
    TimeRange,
    TimeRangeType,
)

if TYPE_CHECKING:
    from uada.models.conversation import ActiveContext
    from uada.models.schema_context import SchemaContext
    from uada.scl.manager import SCLManager


_SMALL_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "ninety": 90,
}


def _number_value(text: str) -> int | None:
    text = text.lower().replace("-", " ").strip()
    if text.isdigit():
        return int(text)
    parts = text.split()
    if not parts or any(part not in _SMALL_NUMBERS for part in parts):
        return None
    return sum(_SMALL_NUMBERS[part] for part in parts)


def _forecast_horizon(question: str) -> int:
    """Extract the requested forecast horizon, including number words."""
    pattern = re.compile(
        r"\bnext\s+((?:\d{1,3})|(?:[a-z]+(?:[-\s][a-z]+)?))\s+"
        r"(?:day|week|month|quarter|year)s?\b",
        re.IGNORECASE,
    )
    match = pattern.search(question)
    parsed = _number_value(match.group(1)) if match else None
    return max(1, min(parsed or 4, 120))


class IntentNormalizer:
    """Enrich an extracted intent with governed profiles and analysis operations."""

    def __init__(self, scl_manager: SCLManager) -> None:
        self._scl_manager = scl_manager

    def normalise(
        self,
        intent: AnalyticalIntent,
        question: str,
        schema_context: SchemaContext,
        active_context: ActiveContext | None = None,
    ) -> AnalyticalIntent:
        lowered = question.lower()
        update: dict[str, object] = {}
        operations = list(intent.analysis_operations)

        profile = self._scl_manager.match_analysis_profile(question)
        if profile is not None:
            profile_filters = [
                item
                for item in intent.filters
                if self._filter_value_is_explicit(item, lowered)
            ]
            profile_operations = [AnalysisOperation(value) for value in profile.operations]
            operations = self._dedupe([*operations, *profile_operations])
            update.update(
                {
                    "analysis_profile": profile.name,
                    "measures": list(profile.measures),
                    "dimensions": list(profile.dimensions),
                    "time_dimension": profile.time_dimension,
                    # A matched governed profile is a complete analysis
                    # contract.  Do not allow speculative extraction fields
                    # to leak into the deterministic query plan.
                    "time_comparison": None,
                    "order_by": None,
                    "limit": profile.limit,
                    "references_prior_turn": False,
                    "prior_turn_id": None,
                    "follow_up_description": None,
                    "question_type": (
                        QuestionType(profile.question_type)
                        if profile.question_type
                        else (
                            QuestionType.TIME_SERIES
                            if profile.time_bucket
                            else QuestionType.AGGREGATION
                        )
                    ),
                    "confidence": max(intent.confidence, 0.95),
                    "clarification_question": None,
                    "filters": profile_filters,
                }
            )
            if profile.order_by:
                update["order_by"] = [
                    OrderClause(
                        measure_or_dimension=profile.order_by,
                        direction=SortDirection(profile.order_direction),
                    )
                ]
            if profile.excluded_glossary_filters:
                excluded_terms = {
                    term.lower() for term in profile.excluded_glossary_filters
                }
                explicit_threshold = bool(
                    re.search(
                        r"\b(?:above|over|at least|greater than|less than|below|under)\s+\d+",
                        lowered,
                    )
                )
                update["filters"] = [
                    item
                    for item in profile_filters
                    if explicit_threshold
                    or (item.glossary_term or "").lower() not in excluded_terms
                ]
            if profile.time_bucket:
                update["time_range"] = TimeRange(
                    range_type=TimeRangeType.ALL,
                    bucket=TimeBucket(profile.time_bucket),
                )

        is_forecast = bool(
            re.search(r"\b(forecast|predict|projection|projected|project)\b", lowered)
        )
        if is_forecast:
            operations = self._dedupe(
                [
                    *operations,
                    AnalysisOperation.DESCRIPTIVE,
                    AnalysisOperation.TREND,
                    AnalysisOperation.FORECAST,
                ]
            )
            measures = self._resolve_measures(
                question,
                intent,
                active_context,
                profile_defaults=list(profile.measures) if profile is not None else None,
            )
            dimensions = self._resolve_dimensions(question, intent, schema_context)
            update.update(
                {
                    "question_type": QuestionType.TIME_SERIES,
                    "measures": measures,
                    "dimensions": dimensions,
                    "time_dimension": None,
                    "time_range": TimeRange(
                        range_type=TimeRangeType.ALL,
                        bucket=TimeBucket.MONTH,
                    ),
                    "forecast_horizon": _forecast_horizon(question),
                    "confidence": max(intent.confidence, 0.95),
                    "clarification_question": None,
                }
            )

        detected = self._detect_operations(lowered, intent.question_type)
        operations = self._dedupe([*operations, *detected])
        if operations:
            update["analysis_operations"] = operations

        confidence_match = re.search(r"\b(8[0-9]|9[0-9])\s*%\s*(?:confidence|interval)", lowered)
        if confidence_match:
            update["confidence_level"] = int(confidence_match.group(1)) / 100.0

        return intent.model_copy(update=update)

    def _filter_value_is_explicit(self, item: object, lowered: str) -> bool:
        """Keep profile filters only when their value is visible in the question."""
        value = getattr(item, "value", None)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            rendered = str(candidate).lower().strip("% ")
            if rendered and re.search(rf"\b{re.escape(rendered)}\b", lowered):
                return True

        glossary_term = getattr(item, "glossary_term", None)
        if glossary_term:
            term = self._scl_manager.resolve_glossary_term(glossary_term)
            labels = [glossary_term, *(term.aliases if term is not None else [])]
            if any(
                re.search(rf"\b{re.escape(label.lower())}\b", lowered)
                for label in labels
            ):
                return True
        return False

    def _resolve_measures(
        self,
        question: str,
        intent: AnalyticalIntent,
        active_context: ActiveContext | None,
        profile_defaults: list[str] | None = None,
    ) -> list[str]:
        lowered = question.lower()
        candidates: list[tuple[int, str]] = []
        for metric in self._scl_manager.scl.metrics:
            for label in [metric.name.replace("_", " "), *metric.aliases]:
                if re.search(rf"\b{re.escape(label.lower())}\b", lowered):
                    candidates.append((len(label), metric.name))
        if candidates:
            return [max(candidates)[1]]

        if profile_defaults:
            return list(profile_defaults)

        resolved = []
        for name in intent.measures:
            metric = self._scl_manager.resolve_metric(name)
            if metric is not None and metric.name not in resolved:
                resolved.append(metric.name)
        if resolved:
            return resolved

        if active_context is not None:
            inherited = [
                metric.name
                for name in active_context.current_measures
                if (metric := self._scl_manager.resolve_metric(name)) is not None
            ]
            if inherited:
                return list(dict.fromkeys(inherited))
        return []

    def _resolve_dimensions(
        self,
        question: str,
        intent: AnalyticalIntent,
        schema_context: SchemaContext,
    ) -> list[str]:
        dimensions = list(intent.dimensions)
        lowered = question.lower()
        explicit_breakdown = re.search(r"\b(?:by|across|per|broken down by)\s+([a-z_ ]+)", lowered)
        if explicit_breakdown:
            tail = explicit_breakdown.group(1)
            all_tables = [*schema_context.tables]
            known_names: set[str] = set()
            for table in all_tables:
                for column in table.columns:
                    if column.semantic_type == "dimension":
                        known_names.add(column.column_name)
            for table in self._scl_manager.scl.tables:
                for column in table.columns:
                    if getattr(column.semantic_type, "value", column.semantic_type) == "dimension":
                        known_names.add(column.name)
            for name in sorted(known_names, key=len, reverse=True):
                if re.search(rf"\b{re.escape(name.replace('_', ' '))}\b", tail):
                    if name not in dimensions:
                        dimensions.append(name)
                    break
        return dimensions

    @staticmethod
    def _detect_operations(
        lowered: str, question_type: QuestionType
    ) -> list[AnalysisOperation]:
        operations: list[AnalysisOperation] = []
        if question_type == QuestionType.COMPARISON:
            operations.append(AnalysisOperation.COMPARISON)
        patterns = {
            AnalysisOperation.CORRELATION: r"\b(correlat|relationship|association)\w*\b",
            AnalysisOperation.REGRESSION: r"\b(regression|effect size|coefficient)\b",
            AnalysisOperation.ANOMALY: r"\b(anomal|outlier|unusual)\w*\b",
            AnalysisOperation.DISTRIBUTION: r"\b(distribution|percentile|quartile|spread)\b",
            AnalysisOperation.DRIVER_ANALYSIS: r"\b(driver|root cause|why|explain)\w*\b",
            AnalysisOperation.CONTRIBUTION: r"\b(contribution|contributor|share of)\w*\b",
            AnalysisOperation.TREND: r"\b(trend|over time|growth|decline|increase)\w*\b",
        }
        for operation, pattern in patterns.items():
            if re.search(pattern, lowered):
                operations.append(operation)
        return operations

    @staticmethod
    def _dedupe(values: list[AnalysisOperation]) -> list[AnalysisOperation]:
        return list(dict.fromkeys(values))


__all__ = ["IntentNormalizer"]
