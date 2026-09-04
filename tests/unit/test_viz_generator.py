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
    async def test_single_value_returns_no_chart_fallback(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        result = _result([ColumnMeta(name="revenue", data_type="float")], [[500.0]])
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue"], raw_question="q"
        )
        analysed = analyser.analyse(result, intent)

        viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VisualisationFallback)
        assert viz.data_available is True

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

    async def test_ranking_over_20_rows_does_not_use_bar_rule(
        self, generator: VisualisationGenerator, analyser: ResultAnalyser
    ) -> None:
        # RANKING with >20 rows isn't covered by any deterministic rule
        # (row_count <= 1 doesn't apply either), so it falls to the LLM path.
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

        with generator.agent.override(
            model=TestModel(
                custom_output_args={
                    "mark": "bar",
                    "encoding": {"x": {"field": "region"}, "y": {"field": "revenue"}},
                }
            )
        ):
            viz = await generator.generate(analysed, intent)

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.BAR

    async def test_multiple_dimensions_produces_bar_chart(
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

        assert isinstance(viz, VegaLiteSpec)
        assert viz.chart_type == ChartType.BAR

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
        assert viz.chart_type == ChartType.BAR
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
