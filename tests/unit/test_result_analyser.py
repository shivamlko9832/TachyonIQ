"""
Tests for ResultAnalyser (uada/pipeline/result_analyser.py).

Pure Pandas logic against synthetic QueryResults -- no LLM, no database.
"""

from __future__ import annotations

import pytest

from uada.models.intent import (
    AnalyticalIntent,
    OrderClause,
    QuestionType,
    RelativePeriod,
    SortDirection,
    TimeBucket,
    TimeRange,
    TimeRangeType,
)
from uada.models.result import ColumnMeta, QueryResult, TrendDirection
from uada.pipeline.result_analyser import ResultAnalyser

pytestmark = pytest.mark.unit


def _result(columns: list[ColumnMeta], rows: list[list[object]]) -> QueryResult:
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        executed_sql="SELECT ...",
        execution_time_ms=1.0,
        database_dialect="sqlite",
    )


def _intent(question_type: QuestionType, **kwargs: object) -> AnalyticalIntent:
    defaults: dict[str, object] = {"raw_question": "q"}
    # AnalyticalIntent's own validators require these for TIME_SERIES/RANKING;
    # fill in an unremarkable default unless the test overrides it.
    if question_type == QuestionType.TIME_SERIES and "time_range" not in kwargs:
        defaults["time_range"] = TimeRange(
            range_type=TimeRangeType.RELATIVE,
            relative_period=RelativePeriod.LAST_12_MONTHS,
            bucket=TimeBucket.MONTH,
        )
    if question_type == QuestionType.RANKING and "order_by" not in kwargs:
        defaults["order_by"] = [
            OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)
        ]
    defaults.update(kwargs)
    return AnalyticalIntent(question_type=question_type, **defaults)  # type: ignore[arg-type]


@pytest.fixture
def analyser() -> ResultAnalyser:
    return ResultAnalyser()


class TestNumericSummaries:
    def test_summarizes_declared_numeric_columns(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="revenue", data_type="float"),
                ColumnMeta(name="region", data_type="str"),
            ],
            rows=[[100.0, "West"], [200.0, "East"], [300.0, "North"]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.numeric_summaries) == 1
        summary = analysed.numeric_summaries[0]
        assert summary.column == "revenue"
        assert summary.min == 100.0
        assert summary.max == 300.0
        assert summary.sum == 600.0
        assert summary.mean == 200.0
        assert summary.null_count == 0

    def test_empty_result_has_no_values(self, analyser: ResultAnalyser) -> None:
        result = _result(columns=[ColumnMeta(name="revenue", data_type="float")], rows=[])
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.numeric_summaries) == 1
        assert analysed.numeric_summaries[0].sum is None
        assert analysed.narrative_insight is None

    def test_null_values_counted_and_excluded(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0], [None], [300.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        summary = analysed.numeric_summaries[0]
        assert summary.null_count == 1
        assert summary.sum == 400.0


class TestAggregationNarrative:
    def test_narrative_and_key_finding(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")], rows=[[123456.789]]
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert analysed.narrative_insight == "Total revenue was 123,456.79."
        assert analysed.key_finding == "revenue: 123,456.79"


class TestTimeSeries:
    def test_increasing_trend(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 100.0], ["2026-02", 110.0], ["2026-03", 150.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert analysed.has_time_dimension is True
        assert analysed.time_column == "period"
        trend = analysed.trend_analysis[0]
        assert trend.direction == TrendDirection.INCREASING
        assert trend.change_pct == pytest.approx(50.0)
        assert "increased 50.0%" in (analysed.narrative_insight or "")

    def test_decreasing_trend(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 200.0], ["2026-02", 150.0], ["2026-03", 100.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        trend = analysed.trend_analysis[0]
        assert trend.direction == TrendDirection.DECREASING
        assert trend.change_pct == pytest.approx(-50.0)

    def test_stable_trend(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 100.0], ["2026-02", 101.0], ["2026-03", 102.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.trend_analysis[0].direction == TrendDirection.STABLE

    def test_volatile_trend_despite_small_net_change(self, analyser: ResultAnalyser) -> None:
        # Net change from 100 to 102 is small (<5%), but it swings to 300
        # in between -- that's volatility, not stability.
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 100.0], ["2026-02", 300.0], ["2026-03", 102.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.trend_analysis[0].direction == TrendDirection.VOLATILE

    def test_insufficient_data(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 100.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.trend_analysis[0].direction == TrendDirection.INSUFFICIENT_DATA

    def test_time_dimension_resolved_from_intent_when_no_period_column(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="order_date", data_type="datetime"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01-01", 100.0], ["2026-02-01", 200.0]],
        )
        intent = _intent(
            QuestionType.TIME_SERIES, measures=["revenue"], time_dimension="orders.order_date"
        )
        analysed = analyser.analyse(result, intent)
        assert analysed.time_column == "order_date"
        assert analysed.has_time_dimension is True


class TestRanking:
    def test_narrative_and_key_finding(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["West", 500.0], ["East", 300.0]],
        )
        intent = _intent(
            QuestionType.RANKING, measures=["revenue"], dimensions=["region"], limit=10
        )
        analysed = analyser.analyse(result, intent)

        assert analysed.narrative_insight == "Top region: West with revenue=500.00."
        assert analysed.key_finding == "West: 500.00"


class TestOutliers:
    def test_detects_extreme_value(self, analyser: ResultAnalyser) -> None:
        # A single huge outlier inflates its own std, which can mask it
        # from a z>3 threshold with too few samples -- use enough tightly
        # clustered "normal" points to dilute that effect.
        normal_rows: list[list[object]] = [[100.0] for _ in range(20)]
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[*normal_rows, [100000.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.outliers) == 1
        outlier = analysed.outliers[0]
        assert outlier.column == "revenue"
        assert outlier.value == 100000.0
        assert outlier.z_score is not None
        assert abs(outlier.z_score) > 3.0

    def test_no_outliers_in_uniform_data(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0], [101.0], [99.0], [100.5]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.outliers == []


class TestComparisonFlag:
    def test_has_comparison_true_for_comparison_question(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")], rows=[[100.0]]
        )
        intent = _intent(QuestionType.COMPARISON, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.has_comparison is True

    def test_has_comparison_false_otherwise(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")], rows=[[100.0]]
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.has_comparison is False


class TestKeyFindingFallback:
    """
    Regression coverage for a real bug found running a raw listing query
    (e.g. a FOLLOW_UP_REFINE "only show enterprise customers" -> SELECT *
    FROM customers) through the live pipeline: with no declared measures,
    the key finding used to always pick numeric_summaries[0] -- the
    first int/float-typed column -- with no regard for whether it was an
    actual measure. `id` is almost always the first column, so this
    produced a nonsensical key finding like "id: 10,450.00" (summing
    primary keys).
    """

    def test_multiple_numeric_columns_with_no_matching_measure_yields_no_key_finding(
        self, analyser: ResultAnalyser
    ) -> None:
        # A bare "SELECT *" listing: `id` is the first numeric column,
        # but it's a primary key, not something intent.measures names.
        result = _result(
            columns=[
                ColumnMeta(name="id", data_type="int"),
                ColumnMeta(name="tier", data_type="str"),
                ColumnMeta(name="signup_bonus", data_type="float"),
            ],
            rows=[[6, "Enterprise", 50.0], [19, "Enterprise", 75.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=[])
        analysed = analyser.analyse(result, intent)
        assert analysed.key_finding is None

    def test_single_numeric_column_still_used_even_without_a_declared_measure(
        self, analyser: ResultAnalyser
    ) -> None:
        # Unambiguous: there's only one numeric column to pick, so it's
        # still safe to use it as the key finding.
        result = _result(
            columns=[
                ColumnMeta(name="revenue", data_type="float"),
                ColumnMeta(name="region", data_type="str"),
            ],
            rows=[[100.0, "West"], [200.0, "East"]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=[])
        analysed = analyser.analyse(result, intent)
        assert analysed.key_finding == "revenue: 300.00"

    def test_declared_measure_wins_over_column_order(self, analyser: ResultAnalyser) -> None:
        # `id` still comes first, but intent.measures names `revenue` --
        # that's the one the key finding should summarise.
        result = _result(
            columns=[
                ColumnMeta(name="id", data_type="int"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[[6, 100.0], [19, 200.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)
        assert analysed.key_finding == "revenue: 300.00"
