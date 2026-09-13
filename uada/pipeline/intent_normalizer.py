"""Deterministic normalization of the LLM's analytical intent.

The LLM interprets language; this module enforces executable semantics.  It
does not produce SQL, statistics, answers, or values.  Its output remains the
typed ``AnalyticalIntent`` consumed by the normal query planner.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from uada.models.intent import (
    AnalysisOperation,
    AnalyticalIntent,
    FilterOperator,
    OrderClause,
    QuestionType,
    RelativePeriod,
    SemanticFilter,
    SortDirection,
    TimeBucket,
    TimeComparison,
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
                bucket = TimeBucket(profile.time_bucket)
                update["time_range"] = (
                    intent.time_range.model_copy(update={"bucket": bucket})
                    if intent.time_range is not None
                    else TimeRange(range_type=TimeRangeType.ALL, bucket=bucket)
                )
            elif (
                intent.time_range is not None
                and intent.time_range.bucket is not None
                and not re.search(
                    r"\b(hourly|daily|weekly|monthly|quarterly|yearly|annual|"
                    r"trend|over time|by (?:day|week|month|quarter|year))\b",
                    lowered,
                )
            ):
                # Time-window phrases such as "in 2026" constrain an aggregate;
                # they do not request one row per month. Discard a model-added
                # bucket unless the user or governed profile explicitly asks
                # for a time series.
                update["time_range"] = intent.time_range.model_copy(
                    update={"bucket": None}
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

        if not is_forecast:
            effective_time_range = update.get("time_range", intent.time_range)
            if isinstance(effective_time_range, TimeRange):
                complete_period = self._cap_current_year_to_complete_months(
                    effective_time_range,
                    question,
                )
                if complete_period != effective_time_range:
                    update["time_range"] = complete_period

        return intent.model_copy(update=update)

    @staticmethod
    def _cap_current_year_to_complete_months(
        time_range: TimeRange,
        question: str,
    ) -> TimeRange:
        """Keep current-year reporting honest when only complete months are available."""
        if time_range.range_type != TimeRangeType.ABSOLUTE:
            return time_range
        if not time_range.start_date or not time_range.end_date:
            return time_range
        explicit_year = re.search(r"\b(?:in|for|during)\s+(20\d{2})\b", question.casefold())
        if explicit_year is None:
            return time_range

        today = date.today()
        requested_year = int(explicit_year.group(1))
        if requested_year != today.year:
            return time_range

        start = date.fromisoformat(time_range.start_date[:10])
        end = date.fromisoformat(time_range.end_date[:10])
        complete_month_end = today.replace(day=1)
        if start != date(today.year, 1, 1) or end <= complete_month_end:
            return time_range
        return time_range.model_copy(update={"end_date": complete_month_end.isoformat()})

    def fallback(
        self,
        question: str,
        schema_context: SchemaContext,
        active_context: ActiveContext | None = None,
    ) -> AnalyticalIntent:
        """Build a conservative governed intent when the intent model is unavailable.

        This fallback resolves only exact semantic-profile aliases, metric aliases,
        declared categorical values, explicit time phrases, and typed conversation
        state.  It never creates SQL, values, metric formulas, or business facts.
        Unsupported language is returned as ``OUT_OF_SCOPE`` or ``AMBIGUOUS`` so a
        provider failure cannot be converted into a plausible analytical answer.
        """
        lowered = question.casefold()
        profile = self._scl_manager.match_analysis_profile(question)
        references_prior = bool(
            active_context
            and active_context.current_measures
            and re.search(
                r"\b(that|this|those|these|it|only|now|same|previous|prior|"
                r"break (?:it|that|this) down|compare (?:it|that|this))\b",
                lowered,
            )
        )

        seed = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=[],
            dimensions=[],
            filters=[],
            references_prior_turn=references_prior,
            follow_up_description=(
                "Conservative deterministic continuation of the active governed context."
                if references_prior
                else None
            ),
            confidence=0.72,
            raw_question=question,
        )

        measures = self._resolve_measures(
            question,
            seed,
            active_context if references_prior else None,
            profile_defaults=list(profile.measures) if profile is not None else None,
        )
        dimensions = self._resolve_dimensions(question, seed, schema_context)
        if references_prior and not dimensions:
            dimensions = list(active_context.current_dimensions)  # type: ignore[union-attr]
        filters = self._resolve_explicit_filters(question)
        time_range = self._resolve_time_range(question)

        if profile is None and not measures:
            if references_prior:
                measures = list(active_context.current_measures)  # type: ignore[union-attr]
            else:
                return AnalyticalIntent(
                    question_type=QuestionType.OUT_OF_SCOPE,
                    measures=[],
                    confidence=0.0,
                    raw_question=question,
                )

        question_type = QuestionType.AGGREGATION
        order_by: list[OrderClause] | None = None
        limit: int | None = None
        time_comparison: TimeComparison | None = None

        if re.search(r"\b(forecast|predict|projection|projected|project)\b", lowered):
            question_type = QuestionType.TIME_SERIES
            time_range = TimeRange(range_type=TimeRangeType.ALL, bucket=TimeBucket.MONTH)
        elif re.search(r"\b(why|what (?:caused|contributed|drove)|driver|root cause)\b", lowered):
            question_type = QuestionType.DIAGNOSTIC
        elif re.search(r"\b(compare|comparison|versus|vs\.?|against)\b", lowered):
            question_type = QuestionType.COMPARISON
            comparison_base = (
                active_context.time_range
                if references_prior and active_context is not None and active_context.time_range
                else time_range
            )
            time_range, time_comparison = self._resolve_comparison_time(
                question, comparison_base
            )
        elif re.search(r"\b(top|bottom|highest|lowest|least|most)\b", lowered) and dimensions:
            question_type = QuestionType.RANKING
            ascending = bool(re.search(r"\b(bottom|lowest|least)\b", lowered))
            order_by = [
                OrderClause(
                    measure_or_dimension=measures[0],
                    direction=SortDirection.ASC if ascending else SortDirection.DESC,
                )
            ]
            number = re.search(r"\b(?:top|bottom)\s+(\d{1,3})\b", lowered)
            limit = min(int(number.group(1)), 100) if number else 10
        elif re.search(r"\b(monthly|weekly|quarterly|yearly|trend|over time)\b", lowered):
            question_type = QuestionType.TIME_SERIES
            bucket = self._time_bucket(lowered)
            if time_range is None:
                time_range = TimeRange(range_type=TimeRangeType.ALL, bucket=bucket)
            else:
                time_range = time_range.model_copy(update={"bucket": bucket})

        intent = AnalyticalIntent(
            question_type=question_type,
            measures=measures,
            dimensions=dimensions,
            filters=filters,
            time_range=time_range,
            time_comparison=time_comparison,
            order_by=order_by,
            limit=limit,
            references_prior_turn=references_prior,
            follow_up_description=seed.follow_up_description,
            analysis_operations=[AnalysisOperation.DESCRIPTIVE],
            forecast_horizon=(
                _forecast_horizon(question)
                if question_type == QuestionType.TIME_SERIES
                and re.search(r"\b(forecast|predict|projection|projected|project)\b", lowered)
                else None
            ),
            confidence=0.72,
            raw_question=question,
        )
        return self.normalise(intent, question, schema_context, active_context)

    @staticmethod
    def _time_bucket(lowered: str) -> TimeBucket:
        if "weekly" in lowered:
            return TimeBucket.WEEK
        if "quarterly" in lowered:
            return TimeBucket.QUARTER
        if "yearly" in lowered or "annual" in lowered:
            return TimeBucket.YEAR
        return TimeBucket.MONTH

    def _resolve_time_range(self, question: str) -> TimeRange | None:
        lowered = question.casefold()
        year = re.search(r"\b(?:in|for|during)\s+(20\d{2})\b", lowered)
        if year:
            value = int(year.group(1))
            return TimeRange(
                range_type=TimeRangeType.ABSOLUTE,
                start_date=f"{value:04d}-01-01",
                end_date=f"{value + 1:04d}-01-01",
            )

        relative_patterns = (
            (r"\bthis year\b", RelativePeriod.THIS_YEAR),
            (r"\blast year\b", RelativePeriod.LAST_YEAR),
            (r"\bthis quarter\b", RelativePeriod.THIS_QUARTER),
            (r"\blast quarter\b", RelativePeriod.LAST_QUARTER),
            (r"\bthis month\b", RelativePeriod.THIS_MONTH),
            (r"\blast month\b", RelativePeriod.LAST_MONTH),
            (r"\blast 12 months?\b", RelativePeriod.LAST_12_MONTHS),
        )
        for pattern, period in relative_patterns:
            if re.search(pattern, lowered):
                return TimeRange(range_type=TimeRangeType.RELATIVE, relative_period=period)

        counted = re.search(r"\blast\s+(\d{1,3})\s+months?\b", lowered)
        if counted:
            return TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_N_MONTHS,
                period_count=min(int(counted.group(1)), 120),
            )
        return None

    @staticmethod
    def _resolve_comparison_time(
        question: str, current: TimeRange | None
    ) -> tuple[TimeRange | None, TimeComparison | None]:
        lowered = question.casefold()
        if "this year" in lowered and "last year" in lowered:
            today = date.today()
            primary_end = date(today.year, today.month, 1)
            comparison_end = date(today.year - 1, today.month, 1)
            return (
                TimeRange(
                    range_type=TimeRangeType.ABSOLUTE,
                    start_date=f"{today.year}-01-01",
                    end_date=primary_end.isoformat(),
                ),
                TimeComparison(
                    comparison_period=RelativePeriod.LAST_YEAR,
                    comparison_label="Same complete months last year",
                    comparison_start_date=f"{today.year - 1}-01-01",
                    comparison_end_date=comparison_end.isoformat(),
                ),
            )
        explicit_years = [int(value) for value in re.findall(r"\b(20\d{2})\b", lowered)]
        if len(explicit_years) >= 2:
            primary, comparison = explicit_years[0], explicit_years[1]
            return (
                TimeRange(
                    range_type=TimeRangeType.ABSOLUTE,
                    start_date=f"{primary}-01-01",
                    end_date=f"{primary + 1}-01-01",
                ),
                TimeComparison(
                    comparison_period=RelativePeriod.LAST_YEAR,
                    comparison_label=str(comparison),
                    comparison_start_date=f"{comparison}-01-01",
                    comparison_end_date=f"{comparison + 1}-01-01",
                ),
            )
        if "last year" in lowered and current is not None:
            if current.range_type == TimeRangeType.ABSOLUTE:
                assert current.start_date is not None and current.end_date is not None
                primary_start = date.fromisoformat(current.start_date[:10])
                primary_end = date.fromisoformat(current.end_date[:10])
                complete_month = date.today().replace(day=1)
                if primary_end > complete_month:
                    primary_end = complete_month
                comparison_start = primary_start.replace(year=primary_start.year - 1)
                comparison_end = primary_end.replace(year=primary_end.year - 1)
                primary = current.model_copy(update={"end_date": primary_end.isoformat()})
                return (
                    primary,
                    TimeComparison(
                        comparison_period=RelativePeriod.LAST_YEAR,
                        comparison_label="Same complete months last year",
                        comparison_start_date=comparison_start.isoformat(),
                        comparison_end_date=comparison_end.isoformat(),
                    ),
                )
            if current.relative_period == RelativePeriod.THIS_YEAR:
                today = date.today()
                month_start = today.replace(day=1)
                return (
                    TimeRange(
                        range_type=TimeRangeType.ABSOLUTE,
                        start_date=f"{today.year}-01-01",
                        end_date=month_start.isoformat(),
                    ),
                    TimeComparison(
                        comparison_period=RelativePeriod.LAST_YEAR,
                        comparison_label="Same complete months last year",
                        comparison_start_date=f"{today.year - 1}-01-01",
                        comparison_end_date=month_start.replace(
                            year=today.year - 1
                        ).isoformat(),
                    ),
                )
        return current, None

    def _resolve_explicit_filters(self, question: str) -> list[SemanticFilter]:
        lowered = question.casefold()
        filters: list[SemanticFilter] = []
        seen: set[tuple[str, str]] = set()
        for table in self._scl_manager.scl.tables:
            for column in table.columns:
                for value in column.enum_values or []:
                    rendered = str(value)
                    if re.search(rf"\b{re.escape(rendered.casefold())}\b", lowered):
                        key = (column.name, rendered)
                        if key not in seen:
                            filters.append(
                                SemanticFilter(
                                    entity=column.name,
                                    operator=FilterOperator.EQUALS,
                                    value=rendered,
                                )
                            )
                            seen.add(key)
        return filters

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
        candidates: list[tuple[int, int, int, str]] = []
        for metric_index, metric in enumerate(self._scl_manager.scl.metrics):
            for label in [metric.name.replace("_", " "), *metric.aliases]:
                for match in re.finditer(rf"\b{re.escape(label.lower())}\b", lowered):
                    candidates.append((match.start(), match.end(), metric_index, metric.name))
        if candidates:
            # Prefer longer non-overlapping labels ("waste revenue" over
            # "revenue"), then preserve the governed metric declaration order.
            selected: list[tuple[int, int, int, str]] = []
            for candidate in sorted(
                candidates,
                key=lambda item: (-(item[1] - item[0]), item[2], item[0]),
            ):
                if any(candidate[0] < item[1] and item[0] < candidate[1] for item in selected):
                    continue
                if candidate[3] not in {item[3] for item in selected}:
                    selected.append(candidate)
            return [item[3] for item in sorted(selected, key=lambda item: item[2])]

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
