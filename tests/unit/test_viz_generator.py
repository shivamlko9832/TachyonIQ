"""
Tests for VisualisationGenerator (uada/pipeline/viz_generator.py).

The deterministic (Altair) paths are exercised end-to-end with no LLM.
The LLM fallback path uses PydanticAI's TestModel, same override pattern
as the other LLM stages.
"""

from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel

from uada.config import Settings
from uada.models.intent import (
    AnalyticalIntent,
    OrderClause,
    QuestionType,
    RelativePeriod,
    SortDirection,
    TimeBucket,
    TimeComparison,
    TimeRange,
    TimeRangeType,
)
from uada.models.result import (
    ChartType,
    ColumnMeta,
    KpiSpec,
    QueryResult,
    VegaLiteSpec,
    VisualisationFallback,
)
from uada.pipeline.result_analyser import ResultAnalyser
from uada.pipeline.viz_generator import VisualisationGenerator

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


@pytest.fixture
def analyser() -> ResultAnalyser:
    return ResultAnalyser()


@pytest.fixture
def generator() -> VisualisationGenerator:
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    return VisualisationGenerator(settings)


class TestDeterministicSelection:
    async def test_single_value_returns_kpi_spec(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        # P2 introduced KpiSpec: scalar results now render as a KPI card, not
        # a VisualisationFallback.
        result = _result([ColumnMeta(name="revenue", data_type="float")], [[500.0]])
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue"], raw_question="q"
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, KpiSpec)
        assert viz.value == 500.0
        assert "revenue" in viz.label.lower()

    async def test_time_series_produces_line_chart(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            [
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            [["2026-01", 100.0], ["2026-02", 150.0], ["2026-03", 130.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.TIME_SERIES,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_12_MONTHS,
                bucket=TimeBucket.MONTH,
            ),
            raw_question="Show monthly revenue",
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.LINE
        assert viz.is_valid() == (True, None)
        assert viz.spec["mark"]["type"] == "line"

    async def test_ranking_produces_bar_chart(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            [
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            [["West", 500.0], ["East", 300.0], ["North", 200.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.RANKING,
            measures=["revenue"],
            dimensions=["region"],
            order_by=[OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)],
            limit=10,
            raw_question="Top regions by revenue",
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.BAR
        assert viz.is_valid() == (True, None)

    async def test_ranking_over_20_rows_produces_table_viz(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        # P2 change: RANKING with >_MAX_BAR_ROWS (20) rows + 1 numeric column
        # now resolves deterministically to TABLE_VIZ (avoids a cramped bar
        # chart with 25 bars).  The LLM is not called.
        rows: list[list[object]] = [[f"R{i}", float(i)] for i in range(25)]
        result = _result(
            [
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows,
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.RANKING,
            measures=["revenue"],
            dimensions=["region"],
            order_by=[OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)],
            raw_question="Top 25 regions by revenue",
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.TABLE_VIZ

    async def test_multiple_dimensions_produces_bar_stacked_chart(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            [
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="segment", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            [["West", "Enterprise", 500.0], ["East", "SMB", 300.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.FILTER,
            measures=["revenue"],
            dimensions=["region", "segment"],
            raw_question="Revenue by region and segment",
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        # P2 change: >= 2 dimensions now routes to BAR_STACKED (grouped/normalised)
        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.BAR_STACKED

    async def test_comparison_produces_grouped_bar_with_color(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        # A real comparison result (per the SQL Generator's own "UNION ALL
        # with period labels" convention) has a dimension column, a period
        # label column, and the measure -- two categoricals, one numeric.
        result = _result(
            [
                ColumnMeta(name="quarter", data_type="str"),
                ColumnMeta(name="period_label", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            [
                ["Q1", "This Year", 300.0],
                ["Q1", "Last Year", 250.0],
                ["Q2", "This Year", 320.0],
                ["Q2", "Last Year", 280.0],
            ],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.COMPARISON,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE, relative_period=RelativePeriod.THIS_YEAR
            ),
            time_comparison=TimeComparison(
                comparison_period=RelativePeriod.LAST_YEAR, comparison_label="Last Year"
            ),
            raw_question="Compare revenue this year vs last year",
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.BAR_GROUPED  # grouped bar for comparisons (P2)
        assert "color" in viz.spec["encoding"]

    async def test_empty_result_returns_fallback(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            [
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            [],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.RANKING,
            measures=["revenue"],
            dimensions=["region"],
            order_by=[OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)],
            raw_question="Top regions by revenue",
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VisualisationFallback)
        assert viz.data_available is False


class TestLLMFallback:
    async def test_undetermined_shape_falls_back_to_llm(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        # AGGREGATION with 2 rows and no dimensions: not single-value, not
        # TIME_SERIES/RANKING/COMPARISON, fewer than 2 dimensions -- no
        # deterministic rule matches.
        result = _result(
            [
                ColumnMeta(name="revenue", data_type="float"),
                ColumnMeta(name="cost", data_type="float"),
            ],
            [[100.0, 50.0], [200.0, 90.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue", "cost"], raw_question="q"
        )
        analysed = analyser.analyse(result, intent)

        with generator.agent.override(
            model=TestModel(
                custom_output_args={
                    "mark": "point",
                    "encoding": {"x": {"field": "revenue"}, "y": {"field": "cost"}},
                }
            )
        ):
            viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.POINT
        # Real data was injected, not left to the model.
        assert viz.spec["data"]["values"] == [
            {"revenue": 100.0, "cost": 50.0},
            {"revenue": 200.0, "cost": 90.0},
        ]

    async def test_hallucinated_column_is_rejected(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            [
                ColumnMeta(name="revenue", data_type="float"),
                ColumnMeta(name="cost", data_type="float"),
            ],
            [[100.0, 50.0], [200.0, 90.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue", "cost"], raw_question="q"
        )
        analysed = analyser.analyse(result, intent)

        with generator.agent.override(
            model=TestModel(
                custom_output_args={
                    "mark": "point",
                    "encoding": {"x": {"field": "profit_margin"}, "y": {"field": "cost"}},
                }
            )
        ):
            viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VisualisationFallback)

    async def test_invalid_mark_is_rejected(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result(
            [
                ColumnMeta(name="revenue", data_type="float"),
                ColumnMeta(name="cost", data_type="float"),
            ],
            [[100.0, 50.0], [200.0, 90.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue", "cost"], raw_question="q"
        )
        analysed = analyser.analyse(result, intent)

        with generator.agent.override(
            model=TestModel(
                custom_output_args={
                    "mark": "not_a_real_mark",
                    "encoding": {"x": {"field": "revenue"}, "y": {"field": "cost"}},
                }
            )
        ):
            viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VisualisationFallback)


# ── P2-3: MultiVizPlanner / generate_supplementary() ─────────────────────────

class TestMultiVizPlanner:
    """
    Tests for VisualisationGenerator.generate_supplementary() — the
    deterministic multi-viz planner added in P2-3.

    Preconditions satisfied by each test are noted in its docstring.
    The method must never raise; it must return at most 2 specs.
    """

    # ── TIME_SERIES → histogram ───────────────────────────────────────────────

    async def test_time_series_produces_histogram_secondary(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """TIME_SERIES with numeric column → secondary is a histogram."""
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-0%d" % i, float(i * 10)] for i in range(1, 7)],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.TIME_SERIES,
            measures=["revenue"],
            raw_question="Revenue trend",
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_12_MONTHS,
                bucket=TimeBucket.MONTH,
            ),
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        assert len(specs) >= 1
        first = specs[0]
        assert isinstance(first, VegaLiteSpec)
        assert first.chart_type == ChartType.HISTOGRAM

    # ── RANKING + time dim → line ────────────────────────────────────────────

    async def test_ranking_with_time_dim_produces_line_secondary(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """RANKING intent with a date column → secondary is a line chart."""
        result = _result(
            columns=[
                ColumnMeta(name="order_date", data_type="date"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-01-01", 200.0], ["2026-02-01", 350.0], ["2026-03-01", 150.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.RANKING,
            measures=["revenue"],
            dimensions=["order_date"],
            raw_question="Top months by revenue",
            order_by=[
                OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)
            ],
        )
        analysed = analyser.analyse(result, intent)
        # Verify the analyser picked up the date column as time dimension
        assert analysed.has_time_dimension
        assert analysed.time_column == "order_date"

        specs = await generator.generate_supplementary(analysed, intent)

        assert len(specs) >= 1
        first = specs[0]
        assert isinstance(first, VegaLiteSpec)
        assert first.chart_type == ChartType.LINE

    # ── AGGREGATION ≥ 5 rows → histogram ─────────────────────────────────────

    async def test_aggregation_5plus_rows_produces_histogram(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """AGGREGATION with ≥ 5 rows and a numeric column → secondary histogram."""
        result = _result(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[
                ["West", 500.0],
                ["East", 300.0],
                ["North", 100.0],
                ["South", 250.0],
                ["Central", 400.0],
            ],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            dimensions=["region"],
            raw_question="Revenue by region",
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        assert len(specs) >= 1
        first = specs[0]
        assert isinstance(first, VegaLiteSpec)
        assert first.chart_type == ChartType.HISTOGRAM

    async def test_aggregation_fewer_than_5_rows_no_supplementary(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """AGGREGATION with < 5 rows → no histogram supplement (rule not triggered)."""
        result = _result(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["West", 500.0], ["East", 300.0], ["North", 100.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            dimensions=["region"],
            raw_question="Revenue by region",
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        # None of the AGGREGATION branch triggers; outlier check also won't fire
        # (no time dim), so result should be empty.
        assert specs == []

    # ── outliers → rule_overlay tertiary ─────────────────────────────────────

    async def test_outlier_produces_rule_overlay_as_second_spec(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """TIME_SERIES with an extreme outlier → [histogram, rule_overlay]."""
        # 20 "normal" points near 100 + one extreme outlier at 100,000
        normal_rows: list[list[object]] = [
            ["2026-%02d" % i, 100.0] for i in range(1, 21)
        ]
        outlier_row: list[object] = ["2026-21", 100000.0]
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[*normal_rows, outlier_row],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.TIME_SERIES,
            measures=["revenue"],
            raw_question="Revenue trend",
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_12_MONTHS,
                bucket=TimeBucket.MONTH,
            ),
        )
        analysed = analyser.analyse(result, intent)
        # Must have detected the outlier
        assert len(analysed.outliers) >= 1

        specs = await generator.generate_supplementary(analysed, intent)

        # First: histogram (from TIME_SERIES rule)
        # Second: rule_overlay (from outlier + time_dim rule)
        assert len(specs) == 2
        chart_types = [s.chart_type for s in specs if isinstance(s, VegaLiteSpec)]
        assert ChartType.HISTOGRAM in chart_types
        assert ChartType.RULE_OVERLAY in chart_types

    # ── at most 2 specs ───────────────────────────────────────────────────────

    async def test_returns_at_most_2_specs(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """No matter the input, generate_supplementary returns ≤ 2 specs."""
        # Construct a result that hits multiple rules
        normal_rows = [["2026-%02d" % i, 100.0] for i in range(1, 21)]
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[*normal_rows, ["2026-21", 999999.0]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.TIME_SERIES,
            measures=["revenue"],
            raw_question="q",
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_12_MONTHS,
                bucket=TimeBucket.MONTH,
            ),
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        assert len(specs) <= 2

    # ── never raises ─────────────────────────────────────────────────────────

    async def test_never_raises_on_empty_result(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """generate_supplementary() returns [] for a result with < 2 rows — no exception."""
        result = _result(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[[100.0]],  # only 1 row
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            raw_question="q",
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        assert specs == []

    async def test_never_raises_on_no_numeric_columns(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """generate_supplementary() returns [] when no numeric columns — no exception."""
        result = _result(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="category", data_type="str"),
            ],
            rows=[["West", "A"], ["East", "B"], ["North", "C"], ["South", "D"], ["Central", "E"]],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            dimensions=["region", "category"],
            raw_question="q",
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        assert isinstance(specs, list)

    # ── supplementary specs are well-formed VegaLiteSpecs ────────────────────

    async def test_supplementary_specs_have_data_injected(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        """Each returned VegaLiteSpec must have 'data.values' with the query rows."""
        result = _result(
            columns=[
                ColumnMeta(name="period", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["2026-0%d" % i, float(i * 10)] for i in range(1, 7)],
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.TIME_SERIES,
            measures=["revenue"],
            raw_question="q",
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_12_MONTHS,
                bucket=TimeBucket.MONTH,
            ),
        )
        analysed = analyser.analyse(result, intent)
        specs = await generator.generate_supplementary(analysed, intent)

        for spec in specs:
            if isinstance(spec, VegaLiteSpec):
                assert "data" in spec.spec
                assert "values" in spec.spec["data"]
                assert len(spec.spec["data"]["values"]) == len(result.rows)
