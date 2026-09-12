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
from uada.models.query_plan import QueryPlan, ResolvedMeasure, ResolvedTable, SQLDialect
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


class TestMetricAdditivity:
    def test_grouped_non_additive_metric_is_not_summed(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="status", data_type="str"),
                ColumnMeta(name="customer_count", data_type="int"),
            ],
            rows=[
                ["delivered", 308],
                ["shipped", 175],
                ["cancelled", 135],
                ["pending", 128],
                ["refunded", 81],
            ],
        )
        intent = _intent(
            QuestionType.AGGREGATION,
            measures=["customer_count"],
            dimensions=["status"],
        )
        plan = QueryPlan(
            intent_question_type="aggregation",
            original_question="Break down customer count by status",
            dialect=SQLDialect.SQLITE,
            primary_table=ResolvedTable(table_name="orders"),
            measures=[
                ResolvedMeasure(
                    name="customer_count",
                    sql_expression="COUNT(DISTINCT orders.customer_id)",
                    output_alias="customer_count",
                    additivity="non_additive",
                )
            ],
        )

        analysed = analyser.analyse(result, intent, plan)

        assert analysed.key_finding is None
        assert analysed.narrative_insight is not None
        assert "not additive" in analysed.narrative_insight
        assert "827" not in " ".join(analysed.key_findings_bullets)
        assert any("must not be summed" in item for item in analysed.key_findings_bullets)

    def test_single_non_additive_metric_value_is_reported(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[ColumnMeta(name="customer_count", data_type="int")],
            rows=[[344]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["customer_count"])
        plan = QueryPlan(
            intent_question_type="aggregation",
            original_question="How many purchasing customers are there?",
            dialect=SQLDialect.SQLITE,
            primary_table=ResolvedTable(table_name="orders"),
            measures=[
                ResolvedMeasure(
                    name="customer_count",
                    sql_expression="COUNT(DISTINCT orders.customer_id)",
                    output_alias="customer_count",
                    additivity="non_additive",
                )
            ],
        )

        analysed = analyser.analyse(result, intent, plan)

        assert analysed.key_finding == "customer_count: 344.00"
        assert analysed.narrative_insight == "Total customer_count was 344.00."

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


# ── P2-4: DuckDB OLAP Enrichment ─────────────────────────────────────────────

class TestDuckDBOlap:
    """
    Tests for the _run_duckdb_olap path in ResultAnalyser.

    DuckDB is only called when row_count >= 3 and there is at least one
    numeric column; all tests satisfy those preconditions.
    """

    duckdb = pytest.importorskip("duckdb")

    # ── correlations ─────────────────────────────────────────────────────────

    def test_perfect_positive_correlation(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="x", data_type="float"),
                ColumnMeta(name="y", data_type="float"),
            ],
            rows=[[1.0, 2.0], [2.0, 4.0], [3.0, 6.0], [4.0, 8.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["x", "y"])
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        assert "x:y" in analysed.olap_insights.correlations
        corr = analysed.olap_insights.correlations["x:y"]
        assert corr == pytest.approx(1.0, abs=0.001)

    def test_perfect_negative_correlation(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="sales", data_type="float"),
                ColumnMeta(name="returns", data_type="float"),
            ],
            rows=[[10.0, 1.0], [8.0, 3.0], [6.0, 5.0], [4.0, 7.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["sales", "returns"])
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        corr = analysed.olap_insights.correlations.get("sales:returns")
        assert corr is not None
        assert corr == pytest.approx(-1.0, abs=0.001)

    def test_no_correlation_with_single_numeric_column(self, analyser: ResultAnalyser) -> None:
        """correlations dict should be empty when only one numeric column."""
        result = _result(
            columns=[
                ColumnMeta(name="revenue", data_type="float"),
                ColumnMeta(name="region", data_type="str"),
            ],
            rows=[[100.0, "A"], [200.0, "B"], [300.0, "C"]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        assert analysed.olap_insights.correlations == {}

    # ── percentiles ──────────────────────────────────────────────────────────

    def test_percentiles_populated_for_numeric_column(self, analyser: ResultAnalyser) -> None:
        rows = [[float(v)] for v in range(1, 11)]  # 10 evenly spaced values
        result = _result(
            columns=[ColumnMeta(name="score", data_type="float")],
            rows=rows,
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["score"])
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        pcts = analysed.olap_insights.percentiles
        assert "score" in pcts
        score_pcts = pcts["score"]
        expected_keys = {"p10", "p25", "p50", "p75", "p90", "p99"}
        assert set(score_pcts.keys()) == expected_keys
        # Monotonicity: percentiles should be non-decreasing
        ordered = [score_pcts[k] for k in ("p10", "p25", "p50", "p75", "p90", "p99")]
        for a, b in zip(ordered, ordered[1:]):
            assert a <= b

    def test_percentile_median_matches_expected_value(self, analyser: ResultAnalyser) -> None:
        # 1..9 — median is 5
        rows = [[float(v)] for v in range(1, 10)]
        result = _result(
            columns=[ColumnMeta(name="amount", data_type="float")],
            rows=rows,
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["amount"])
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        p50 = analysed.olap_insights.percentiles["amount"]["p50"]
        assert p50 == pytest.approx(5.0, abs=0.01)

    # ── top_contributors ─────────────────────────────────────────────────────

    def test_top_contributors_ranked_by_measure(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[
                ["West", 500.0],
                ["East", 300.0],
                ["North", 100.0],
                ["South", 50.0],
            ],
        )
        intent = _intent(
            QuestionType.AGGREGATION,
            measures=["revenue"],
            dimensions=["region"],
        )
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        top = analysed.olap_insights.top_contributors
        assert len(top) == 3
        assert top[0]["dimension"] == "West"
        assert top[0]["value"] == pytest.approx(500.0)
        assert top[1]["dimension"] == "East"

    def test_top_contributors_empty_when_no_dimensions(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0], [200.0], [300.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert analysed.olap_insights is not None
        assert analysed.olap_insights.top_contributors == []

    # ── no OLAP when too few rows ─────────────────────────────────────────────

    def test_olap_skipped_for_fewer_than_3_rows(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="a", data_type="float"),
                ColumnMeta(name="b", data_type="float"),
            ],
            rows=[[1.0, 2.0], [3.0, 4.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["a", "b"])
        analysed = analyser.analyse(result, intent)
        assert analysed.olap_insights is None

    # ── key_findings_bullets ─────────────────────────────────────────────────

    def test_key_findings_bullets_non_empty_for_multi_row_result(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0], [200.0], [300.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.key_findings_bullets) > 0
        # At minimum, there should be a row-count bullet
        combined = " ".join(analysed.key_findings_bullets)
        assert "3" in combined or "data point" in combined

    def test_key_findings_bullets_include_metric_summary(self, analyser: ResultAnalyser) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0], [200.0], [300.0], [400.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        combined = " ".join(analysed.key_findings_bullets).lower()
        assert "revenue" in combined

    def test_key_findings_bullets_include_trend_for_time_series(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 100.0], ["2026-02", 150.0], ["2026-03", 200.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.key_findings_bullets) > 0
        combined = " ".join(analysed.key_findings_bullets).lower()
        # Should contain a trend description
        assert any(word in combined for word in ("increased", "decreased", "trend", "revenue"))

    # ── drivers ──────────────────────────────────────────────────────────────

    def test_drivers_non_empty_for_time_series_with_trend(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01", 100.0], ["2026-02", 200.0], ["2026-03", 400.0]],
        )
        intent = _intent(QuestionType.TIME_SERIES, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.drivers) > 0
        # Driver should mention the trend direction
        combined = " ".join(analysed.drivers).lower()
        assert "increasing" in combined or "revenue" in combined

    def test_drivers_include_dimension_segmentation_hint(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["West", 100.0], ["East", 200.0], ["North", 300.0]],
        )
        intent = _intent(
            QuestionType.AGGREGATION, measures=["revenue"], dimensions=["region"]
        )
        analysed = analyser.analyse(result, intent)

        combined = " ".join(analysed.drivers).lower()
        assert "region" in combined

    # ── anomaly_descriptions ─────────────────────────────────────────────────

    def test_anomaly_descriptions_populated_from_outliers(
        self, analyser: ResultAnalyser
    ) -> None:
        normal_rows: list[list[object]] = [[100.0] for _ in range(20)]
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[*normal_rows, [100000.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert len(analysed.anomaly_descriptions) > 0
        # The description should reference the column
        assert any("revenue" in desc.lower() for desc in analysed.anomaly_descriptions)

    def test_anomaly_descriptions_empty_when_no_outliers(
        self, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0], [101.0], [99.0], [100.5], [98.0]],
        )
        intent = _intent(QuestionType.AGGREGATION, measures=["revenue"])
        analysed = analyser.analyse(result, intent)

        assert analysed.anomaly_descriptions == []
