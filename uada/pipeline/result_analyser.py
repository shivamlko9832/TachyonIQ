"""
Result Analyser (pipeline step 8)
====================================
Deterministic. Converts a raw `QueryResult` into an `AnalysedResult`:
statistical summaries, trend detection for time series, outlier
detection, and a short natural-language narrative -- all via Pandas, no
LLM call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from uada.models.intent import QuestionType
from uada.models.result import (
    AnalysedResult,
    NumericSummary,
    Outlier,
    TrendAnalysis,
    TrendDirection,
)

if TYPE_CHECKING:
    from uada.models.intent import AnalyticalIntent
    from uada.models.result import QueryResult

logger = logging.getLogger(__name__)

_TREND_INCREASE_THRESHOLD_PCT = 5.0
_TREND_DECREASE_THRESHOLD_PCT = -5.0
_VOLATILITY_STEP_THRESHOLD = 0.15
_OUTLIER_Z_SCORE_THRESHOLD = 3.0


class ResultAnalyser:
    """Pipeline step 8: QueryResult -> AnalysedResult. Pure Pandas, no LLM."""

    def analyse(self, result: QueryResult, intent: AnalyticalIntent) -> AnalysedResult:
        """Post-process `result` into statistical summaries and a narrative."""
        df = pd.DataFrame(result.rows, columns=result.column_names)

        numeric_columns = self._numeric_columns(result, df)
        numeric_summaries = [self._summarize_column(df, col) for col in numeric_columns]

        time_column = self._resolve_time_column(result, intent)
        has_time_dimension = time_column is not None and time_column in df.columns

        trend_analysis = (
            [self._analyse_trend(df, time_column, col) for col in numeric_columns]
            if has_time_dimension and time_column is not None
            else []
        )

        outliers = self._detect_outliers(df, numeric_columns)

        narrative_insight = self._build_narrative(intent, df, numeric_summaries, trend_analysis)
        key_finding = self._build_key_finding(intent, df, numeric_summaries, trend_analysis)

        analysed = AnalysedResult(
            query_result=result,
            numeric_summaries=numeric_summaries,
            trend_analysis=trend_analysis,
            outliers=outliers,
            has_time_dimension=has_time_dimension,
            time_column=time_column if has_time_dimension else None,
            has_comparison=intent.question_type == QuestionType.COMPARISON,
            narrative_insight=narrative_insight,
            key_finding=key_finding,
        )
        logger.info(
            "Result analysed: %d numeric column(s), %d outlier(s), has_time_dimension=%s.",
            len(numeric_summaries),
            len(outliers),
            has_time_dimension,
        )
        return analysed

    # ── Numeric summaries ────────────────────────────────────────────────────

    def _numeric_columns(self, result: QueryResult, df: pd.DataFrame) -> list[str]:
        """
        Numeric columns per the declared ColumnMeta type -- not per
        pandas' inferred dtype, which defaults an empty (0-row) column to
        `object` regardless of its declared type, incorrectly excluding
        it from summarization for an empty result set.
        """
        return [
            c.name
            for c in result.columns
            if c.data_type in ("int", "float") and c.name in df.columns
        ]

    def _summarize_column(self, df: pd.DataFrame, column: str) -> NumericSummary:
        series = df[column]
        non_null = series.dropna()
        null_count = int(series.isna().sum())
        if non_null.empty:
            return NumericSummary(column=column, null_count=null_count)
        return NumericSummary(
            column=column,
            min=float(non_null.min()),
            max=float(non_null.max()),
            mean=float(non_null.mean()),
            median=float(non_null.median()),
            std=float(non_null.std()) if len(non_null) > 1 else 0.0,
            sum=float(non_null.sum()),
            null_count=null_count,
        )

    # ── Time dimension / trend ───────────────────────────────────────────────

    def _resolve_time_column(self, result: QueryResult, intent: AnalyticalIntent) -> str | None:
        """
        Best-effort resolution of which result column is the time axis.

        QueryPlanner always aliases a TIME_SERIES bucket expression as
        'period' (see query_planner.py's per-dialect bucket functions),
        and the SQL Generator's system prompt requires using plan aliases
        exactly -- so 'period' is checked first. Falls back to
        intent.time_dimension matching a column, then to any column whose
        declared type is datetime/date.
        """
        column_names = set(result.column_names)
        if "period" in column_names:
            return "period"
        if intent.time_dimension:
            bare_name = intent.time_dimension.rsplit(".", 1)[-1]
            if bare_name in column_names:
                return bare_name
        for col in result.columns:
            if col.data_type in ("datetime", "date"):
                return col.name
        return None

    def _analyse_trend(
        self, df: pd.DataFrame, time_column: str, value_column: str
    ) -> TrendAnalysis:
        sorted_pairs = df[[time_column, value_column]].dropna().sort_values(time_column)
        series = sorted_pairs[value_column].astype(float)

        if len(series) < 2:
            return TrendAnalysis(
                column=value_column,
                direction=TrendDirection.INSUFFICIENT_DATA,
                note="Fewer than two data points with a value.",
            )

        first, last = float(series.iloc[0]), float(series.iloc[-1])
        change_pct = ((last - first) / abs(first) * 100) if first != 0 else None

        if change_pct is not None and change_pct > _TREND_INCREASE_THRESHOLD_PCT:
            direction = TrendDirection.INCREASING
        elif change_pct is not None and change_pct < _TREND_DECREASE_THRESHOLD_PCT:
            direction = TrendDirection.DECREASING
        else:
            direction = (
                TrendDirection.VOLATILE if self._is_volatile(series) else TrendDirection.STABLE
            )

        note = None
        if change_pct is not None:
            direction_word = "Up" if change_pct >= 0 else "Down"
            note = f"{direction_word} {abs(change_pct):.1f}% from first to last."

        return TrendAnalysis(
            column=value_column, direction=direction, change_pct=change_pct, note=note
        )

    def _is_volatile(self, series: pd.Series) -> bool:
        """
        A series can have a small net first-to-last change but still swing
        a lot in between. Flag VOLATILE (rather than STABLE) if any
        consecutive step moves more than 15% relative to the series' own
        mean magnitude.
        """
        mean_magnitude = series.abs().mean()
        if not mean_magnitude:
            return False
        step_changes = series.diff().dropna().abs()
        return bool((step_changes / mean_magnitude > _VOLATILITY_STEP_THRESHOLD).any())

    # ── Outliers ─────────────────────────────────────────────────────────────

    def _detect_outliers(self, df: pd.DataFrame, numeric_columns: list[str]) -> list[Outlier]:
        outliers: list[Outlier] = []
        for column in numeric_columns:
            series = df[column].dropna()
            if len(series) < 2:
                continue
            std = series.std()
            if not std:
                continue
            mean = series.mean()
            z_scores = (series - mean) / std
            for index, z in z_scores.items():
                if abs(z) > _OUTLIER_Z_SCORE_THRESHOLD:
                    value = float(series.loc[index])
                    outliers.append(
                        Outlier(
                            column=column,
                            row_index=int(index),
                            value=value,
                            z_score=float(z),
                            description=(
                                f"{column} value {value:.2f} is {abs(z):.1f} "
                                "std devs from the mean."
                            ),
                        )
                    )
        return outliers

    # ── Narrative ────────────────────────────────────────────────────────────

    def _build_narrative(
        self,
        intent: AnalyticalIntent,
        df: pd.DataFrame,
        numeric_summaries: list[NumericSummary],
        trend_analysis: list[TrendAnalysis],
    ) -> str | None:
        if intent.question_type == QuestionType.AGGREGATION:
            return self._aggregation_narrative(intent, numeric_summaries)
        if intent.question_type == QuestionType.TIME_SERIES:
            return self._time_series_narrative(intent, trend_analysis)
        if intent.question_type == QuestionType.RANKING:
            return self._ranking_narrative(intent, df)
        return None

    def _aggregation_narrative(
        self, intent: AnalyticalIntent, numeric_summaries: list[NumericSummary]
    ) -> str | None:
        if not intent.measures or not numeric_summaries:
            return None
        measure_name = intent.measures[0]
        summary = next(
            (s for s in numeric_summaries if s.column == measure_name), numeric_summaries[0]
        )
        if summary.sum is None:
            return None
        return f"Total {measure_name} was {_format_number(summary.sum)}."

    def _time_series_narrative(
        self, intent: AnalyticalIntent, trend_analysis: list[TrendAnalysis]
    ) -> str | None:
        if not trend_analysis:
            return None
        measure_name = intent.measures[0] if intent.measures else trend_analysis[0].column
        trend = next((t for t in trend_analysis if t.column == measure_name), trend_analysis[0])
        if trend.change_pct is None:
            return f"{measure_name.capitalize()} trend over the period was {trend.direction.value}."
        verb = _TREND_VERBS[trend.direction]
        return f"{measure_name.capitalize()} {verb} {abs(trend.change_pct):.1f}% over the period."

    def _ranking_narrative(self, intent: AnalyticalIntent, df: pd.DataFrame) -> str | None:
        if not intent.dimensions or not intent.measures or df.empty:
            return None
        dimension, measure = intent.dimensions[0], intent.measures[0]
        if dimension not in df.columns or measure not in df.columns:
            return None
        top_row = df.iloc[0]
        return (
            f"Top {dimension}: {top_row[dimension]} "
            f"with {measure}={_format_number(float(top_row[measure]))}."
        )

    def _build_key_finding(
        self,
        intent: AnalyticalIntent,
        df: pd.DataFrame,
        numeric_summaries: list[NumericSummary],
        trend_analysis: list[TrendAnalysis],
    ) -> str | None:
        if intent.question_type == QuestionType.TIME_SERIES and trend_analysis:
            trend = trend_analysis[0]
            if trend.change_pct is not None:
                return f"{trend.column} {trend.direction.value} {abs(trend.change_pct):.1f}%."
        if (
            intent.question_type == QuestionType.RANKING
            and not df.empty
            and intent.dimensions
            and intent.measures
        ):
            dimension, measure = intent.dimensions[0], intent.measures[0]
            if dimension in df.columns and measure in df.columns:
                top_row = df.iloc[0]
                return f"{top_row[dimension]}: {_format_number(float(top_row[measure]))}"
        if numeric_summaries and numeric_summaries[0].sum is not None:
            summary = numeric_summaries[0]
            return f"{summary.column}: {_format_number(summary.sum)}"  # type: ignore[arg-type]
        return None


_TREND_VERBS: dict[TrendDirection, str] = {
    TrendDirection.INCREASING: "increased",
    TrendDirection.DECREASING: "decreased",
    TrendDirection.STABLE: "stayed roughly stable",
    TrendDirection.VOLATILE: "was volatile",
    TrendDirection.INSUFFICIENT_DATA: "could not be determined",
}


def _format_number(value: float) -> str:
    return f"{value:,.2f}"
