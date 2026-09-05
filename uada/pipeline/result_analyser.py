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
    OlapInsights,
    Outlier,
    TrendAnalysis,
    TrendDirection,
)

# P3 analytics modules — lazy imports inside methods so app starts without them


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

        # DuckDB OLAP enrichment (P2-4) — best-effort, never blocks the response
        olap_insights: OlapInsights | None = None
        if len(numeric_columns) >= 1 and result.row_count >= 3:
            olap_insights = self._run_duckdb_olap(df, numeric_columns, intent)

        # P3-1: Correlation Analysis — best-effort, never blocks the response
        correlation_result = None
        if result.row_count >= 10 and len(numeric_columns) >= 2:
            try:
                from uada.analytics.correlation import CorrelationAnalyser
                correlation_result = CorrelationAnalyser().analyse(df, numeric_columns)
            except Exception as _exc:
                logger.debug("CorrelationAnalyser skipped: %s", _exc)

        # P3-2: Anomaly Detection — best-effort, never blocks the response
        anomaly_result = None
        if result.row_count >= 10 and len(numeric_columns) >= 1:
            try:
                from uada.analytics.anomaly import AnomalyDetector
                anomaly_result = AnomalyDetector().detect(df, numeric_columns)
            except Exception as _exc:
                logger.debug("AnomalyDetector skipped: %s", _exc)

        # Richer deterministic narration (P2-2 — bullets, drivers, anomaly descriptions)
        key_findings_bullets = self._build_findings_bullets(
            intent, df, numeric_summaries, trend_analysis, outliers
        )
        drivers = self._build_drivers(intent, df, numeric_summaries, trend_analysis)
        anomaly_descriptions = [o.description for o in outliers[:5]]

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
            key_findings_bullets=key_findings_bullets,
            drivers=drivers,
            anomaly_descriptions=anomaly_descriptions,
            olap_insights=olap_insights,
            correlation_result=correlation_result,
            anomaly_result=anomaly_result,
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
        summary = self._select_fallback_summary(intent, numeric_summaries)
        if summary is not None and summary.sum is not None:
            return f"{summary.column}: {_format_number(summary.sum)}"  # type: ignore[arg-type]
        return None

    def _select_fallback_summary(
        self,
        intent: AnalyticalIntent,
        numeric_summaries: list[NumericSummary],
    ) -> NumericSummary | None:
        """
        Pick the numeric column the key finding should summarise, without
        ever guessing at an unrelated one.

        `ColumnMeta` carries no primary/foreign-key flag (it's one of the
        fully-specified models), so a raw listing query's first numeric
        column is just as likely to be an id column as an actual measure
        -- e.g. a bare "SELECT * FROM customers" once summed its `id`
        column into a nonsensical "id: 10,450.00" key finding. Preferring
        a column intent.measures actually names, and otherwise only
        trusting a single unambiguous numeric column, avoids that without
        needing schema access this stage doesn't have.
        """
        if not numeric_summaries:
            return None
        named_measures = {m.lower() for m in intent.measures}
        for summary in numeric_summaries:
            if summary.column.lower() in named_measures:
                return summary
        if len(numeric_summaries) == 1:
            return numeric_summaries[0]
        return None



    # ── DuckDB OLAP (P2-4) ───────────────────────────────────────────────────

    def _run_duckdb_olap(
        self,
        df: pd.DataFrame,
        numeric_columns: list[str],
        intent: AnalyticalIntent,
    ) -> OlapInsights | None:
        """
        Run in-process OLAP queries against the result DataFrame via DuckDB.
        Returns None if DuckDB is not installed or any error occurs — callers
        must treat this as best-effort enrichment only.

        Safety: operates on the already-fetched QueryResult DataFrame in memory.
        No new database connections are opened; no SQL is sent to the user's DB.
        """
        try:
            import duckdb  # noqa: PLC0415 — lazy import; not always installed

            con = duckdb.connect(database=":memory:")
            con.register("result_df", df)

            correlations: dict[str, float] = {}
            if len(numeric_columns) >= 2:
                for i, col_a in enumerate(numeric_columns):
                    for col_b in numeric_columns[i + 1:]:
                        try:
                            row = con.execute(
                                f'SELECT CORR("{col_a}", "{col_b}") FROM result_df'
                            ).fetchone()
                            if row and row[0] is not None:
                                correlations[f"{col_a}:{col_b}"] = round(float(row[0]), 4)
                        except Exception:  # noqa: BLE001
                            pass

            percentiles: dict[str, dict[str, float]] = {}
            for col in numeric_columns[:3]:  # cap to avoid large payloads
                try:
                    row = con.execute(f"""
                        SELECT
                            PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY "{col}"),
                            PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY "{col}"),
                            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY "{col}"),
                            PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY "{col}"),
                            PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY "{col}"),
                            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY "{col}")
                        FROM result_df
                    """).fetchone()
                    if row:
                        labels = ["p10", "p25", "p50", "p75", "p90", "p99"]
                        percentiles[col] = {
                            label: round(float(v), 4)
                            for label, v in zip(labels, row)
                            if v is not None
                        }
                except Exception:  # noqa: BLE001
                    pass

            # Top contributors: first dimension × first measure
            top_contributors: list[dict[str, object]] = []
            if intent.dimensions and intent.measures:
                dim = intent.dimensions[0]
                measure = intent.measures[0]
                if dim in df.columns and measure in df.columns:
                    try:
                        rows = con.execute(f"""
                            SELECT "{dim}", SUM("{measure}") AS total
                            FROM result_df
                            WHERE "{dim}" IS NOT NULL
                            GROUP BY "{dim}"
                            ORDER BY total DESC
                            LIMIT 3
                        """).fetchall()
                        top_contributors = [
                            {"dimension": r[0], "value": round(float(r[1]), 2)}
                            for r in rows
                        ]
                    except Exception:  # noqa: BLE001
                        pass

            con.close()
            return OlapInsights(
                correlations=correlations,
                percentiles=percentiles,
                top_contributors=top_contributors,
            )

        except ImportError:
            logger.debug("DuckDB not installed — skipping in-process OLAP enrichment.")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("DuckDB OLAP enrichment failed (non-fatal): %s", exc)
            return None

    # ── Richer narration (P2-2) ──────────────────────────────────────────────

    def _build_findings_bullets(
        self,
        intent: AnalyticalIntent,
        df: pd.DataFrame,
        numeric_summaries: list[NumericSummary],
        trend_analysis: list[TrendAnalysis],
        outliers: list[Outlier],
    ) -> list[str]:
        """Generate 3-5 factual bullet points from statistical summaries."""
        bullets: list[str] = []

        # Primary metric summary
        for summary in numeric_summaries[:2]:
            if summary.sum is not None:
                bullets.append(
                    f"{summary.column.replace('_', ' ').title()}: "
                    f"total {_format_number(summary.sum)}, "
                    f"avg {_format_number(summary.mean or 0)}, "
                    f"range {_format_number(summary.min or 0)}–{_format_number(summary.max or 0)}."
                )

        # Trend bullets
        for trend in trend_analysis[:2]:
            if trend.change_pct is not None:
                direction = "increased" if trend.change_pct > 0 else "decreased"
                bullets.append(
                    f"{trend.column.replace('_', ' ').title()} {direction} "
                    f"{abs(trend.change_pct):.1f}% from start to end of period."
                )

        # Outlier bullet
        if outliers:
            top_outlier = max(outliers, key=lambda o: abs(o.z_score or 0))
            bullets.append(
                f"Notable outlier: {top_outlier.column} = {_format_number(top_outlier.value)} "
                f"({abs(top_outlier.z_score or 0):.1f}σ from mean)."
            )

        # Row-count finding
        if len(df) > 0:
            bullets.append(f"Query returned {len(df):,} data point(s).")

        return bullets[:5]

    def _build_drivers(
        self,
        intent: AnalyticalIntent,
        df: pd.DataFrame,
        numeric_summaries: list[NumericSummary],
        trend_analysis: list[TrendAnalysis],
    ) -> list[str]:
        """Generate plausible driver statements from intent + trend data."""
        drivers: list[str] = []

        for trend in trend_analysis:
            if trend.direction.value in ("increasing", "decreasing"):
                col = trend.column.replace("_", " ")
                drivers.append(
                    f"{col.title()} shows a {trend.direction.value} trend "
                    f"({abs(trend.change_pct or 0):.1f}% change). "
                    "Consider segmenting by dimension to identify the primary driver."
                )

        if intent.dimensions:
            dim = intent.dimensions[0].replace("_", " ")
            drivers.append(
                f"Breakdown by '{dim}' may reveal which segment is driving overall performance."
            )

        if not drivers:
            drivers.append(
                "Insufficient trend data to identify specific drivers. "
                "A time-series breakdown may surface contributing factors."
            )

        return drivers[:3]

_TREND_VERBS: dict[TrendDirection, str] = {
    TrendDirection.INCREASING: "increased",
    TrendDirection.DECREASING: "decreased",
    TrendDirection.STABLE: "stayed roughly stable",
    TrendDirection.VOLATILE: "was volatile",
    TrendDirection.INSUFFICIENT_DATA: "could not be determined",
}


def _format_number(value: float) -> str:
    return f"{value:,.2f}"
