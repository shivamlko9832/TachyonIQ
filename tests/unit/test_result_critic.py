"""
Unit tests for ResultCritic (uada/pipeline/result_critic.py).

All checks are deterministic and require no LLM or database connection.

Coverage:
  C1  empty_result            — zero-row result
  C2  unexpected_cardinality  — fan-out and over-aggregation paths
  C3  missing_time_axis       — time-series intent without time column
  C4  suspicious_joins        — deep join chain with high row count
  C5  aggregation_mismatch    — COUNT→text, SUM→_id column
  C6  insufficient_sample     — statistical intent with n < 30
  C7  intent_alignment        — missing SELECT aliases
  C8  result_anomaly          — IQR-based outlier detection
  composite scoring
  safe-fail behaviour
"""

from __future__ import annotations

import pytest

from uada.db.interface import QueryExecutionResult
from uada.models.query_plan import (
    AggregationFunction,
    QueryPlan,
    ResolvedDimension,
    ResolvedJoin,
    ResolvedMeasure,
    ResolvedTable,
    SQLDialect,
    TimeResolution,
)
from uada.pipeline.result_critic import CriticResult, ResultCritic

pytestmark = pytest.mark.unit


# ── Helpers ───────────────────────────────────────────────────────────────────


def _exec(
    *,
    columns: list[str] | None = None,
    types: list[str] | None = None,
    rows: list[list[object]] | None = None,
    row_count: int | None = None,
    is_truncated: bool = False,
) -> QueryExecutionResult:
    """Minimal QueryExecutionResult factory."""
    cols = columns or ["value"]
    typs = types or ["float"] * len(cols)
    rws = rows or [[1.0]] * (row_count if row_count is not None else 1)
    rc = row_count if row_count is not None else len(rws)
    return QueryExecutionResult(
        column_names=cols,
        column_types=typs,
        rows=rws,
        row_count=rc,
        is_truncated=is_truncated,
    )


def _plan(
    *,
    joins: list[ResolvedJoin] | None = None,
    dimensions: list[ResolvedDimension] | None = None,
    measures: list[ResolvedMeasure] | None = None,
    time_resolution: TimeResolution | None = None,
    estimated_complexity: str = "simple",
) -> QueryPlan:
    """Minimal QueryPlan factory."""
    return QueryPlan(
        intent_question_type="aggregation",
        original_question="test q",
        dialect=SQLDialect.SQLITE,
        primary_table=ResolvedTable(table_name="orders"),
        joins=joins or [],
        dimensions=dimensions or [],
        measures=measures or [],
        time_resolution=time_resolution,
        estimated_complexity=estimated_complexity,
    )


def _measure(name: str, agg: str = "SUM", alias: str | None = None) -> ResolvedMeasure:
    return ResolvedMeasure(
        name=name,
        sql_expression=f"{agg}({name})",
        aggregation=AggregationFunction[agg] if agg in AggregationFunction.__members__ else None,
        output_alias=alias or name,
    )


def _dim(name: str) -> ResolvedDimension:
    return ResolvedDimension(name=name, sql_expression=name, output_alias=name)


def _join(from_t: str = "orders", to_t: str = "customers") -> ResolvedJoin:
    from uada.models.query_plan import JoinType
    return ResolvedJoin(
        from_table=from_t,
        to_table=to_t,
        join_type=JoinType.LEFT,
        condition=f"{from_t}.id = {to_t}.id",
    )


def _time_res() -> TimeResolution:
    return TimeResolution(
        time_column="orders.order_date",
        dialect=SQLDialect.SQLITE,
        group_by_sql="strftime('%Y-%m', order_date) AS period",
    )


@pytest.fixture
def critic() -> ResultCritic:
    return ResultCritic()


# ── C1: empty result ──────────────────────────────────────────────────────────


class TestC1EmptyResult:
    def test_empty_result_fails(self, critic: ResultCritic) -> None:
        r = _exec(rows=[], row_count=0)
        result = critic.critique(r, _plan())
        assert result.empty_result is True
        assert result.score == 0.0
        assert result.is_sufficient is False

    def test_non_empty_passes(self, critic: ResultCritic) -> None:
        r = _exec(rows=[[42.0]], row_count=1)
        result = critic.critique(r, _plan())
        c1 = next(d for d in result.dimensions if d.name == "empty_result")
        assert c1.passed is True
        assert c1.score == 1.0


# ── C2: unexpected cardinality ────────────────────────────────────────────────


class TestC2UnexpectedCardinality:
    def test_fan_out_detected(self, critic: ResultCritic) -> None:
        """Truncated + has joins + no dimensions → fan-out."""
        r = _exec(row_count=10_000, is_truncated=True)
        plan = _plan(joins=[_join()], dimensions=[])
        result = critic.critique(r, plan)
        c2 = next(d for d in result.dimensions if d.name == "unexpected_cardinality")
        assert c2.passed is False
        assert c2.score == 0.0

    def test_fan_out_not_triggered_with_dimensions(self, critic: ResultCritic) -> None:
        """Truncated + has joins + has dimensions → no fan-out (grouped query)."""
        r = _exec(columns=["region", "revenue"], types=["str", "float"],
                  rows=[["East", 100.0]], row_count=1, is_truncated=True)
        plan = _plan(joins=[_join()], dimensions=[_dim("region")])
        result = critic.critique(r, plan)
        c2 = next(d for d in result.dimensions if d.name == "unexpected_cardinality")
        assert c2.passed is True

    def test_over_aggregation_time_series(self, critic: ResultCritic) -> None:
        """Time-series intent + time_resolution in plan + row_count==1 → over-agg."""
        r = _exec(columns=["revenue"], types=["float"], rows=[[500.0]], row_count=1)
        plan = _plan(time_resolution=_time_res())
        result = critic.critique(r, plan, intent_type="time_series")
        c2 = next(d for d in result.dimensions if d.name == "unexpected_cardinality")
        assert c2.passed is False
        assert c2.score == 0.5

    def test_over_aggregation_not_triggered_without_time_intent(
        self, critic: ResultCritic
    ) -> None:
        r = _exec(row_count=1)
        plan = _plan(time_resolution=_time_res())
        result = critic.critique(r, plan, intent_type="aggregation")
        c2 = next(d for d in result.dimensions if d.name == "unexpected_cardinality")
        assert c2.passed is True


# ── C3: missing time axis ──────────────────────────────────────────────────────


class TestC3MissingTimeAxis:
    def test_missing_time_col_fails(self, critic: ResultCritic) -> None:
        """Time-series intent + time_resolution set + no time keyword in column names."""
        r = _exec(columns=["revenue", "region"], types=["float", "str"],
                  rows=[[100.0, "East"]], row_count=1)
        plan = _plan(time_resolution=_time_res())
        result = critic.critique(r, plan, intent_type="time_series")
        c3 = next(d for d in result.dimensions if d.name == "missing_time_axis")
        assert c3.passed is False
        assert c3.score == 0.0

    def test_time_column_present_passes(self, critic: ResultCritic) -> None:
        r = _exec(columns=["period", "revenue"], types=["str", "float"],
                  rows=[["2024-01", 100.0]], row_count=1)
        plan = _plan(time_resolution=_time_res())
        result = critic.critique(r, plan, intent_type="time_series")
        c3 = next(d for d in result.dimensions if d.name == "missing_time_axis")
        assert c3.passed is True

    def test_skipped_without_time_intent(self, critic: ResultCritic) -> None:
        r = _exec(columns=["revenue"], types=["float"], rows=[[100.0]], row_count=1)
        plan = _plan(time_resolution=_time_res())
        result = critic.critique(r, plan, intent_type="aggregation")
        c3 = next(d for d in result.dimensions if d.name == "missing_time_axis")
        assert c3.passed is True

    def test_skipped_without_time_resolution(self, critic: ResultCritic) -> None:
        r = _exec(columns=["revenue"], types=["float"], rows=[[100.0]], row_count=1)
        plan = _plan(time_resolution=None)
        result = critic.critique(r, plan, intent_type="time_series")
        c3 = next(d for d in result.dimensions if d.name == "missing_time_axis")
        assert c3.passed is True


# ── C4: suspicious joins ──────────────────────────────────────────────────────


class TestC4SuspiciousJoins:
    def test_suspicious_deep_join_chain(self, critic: ResultCritic) -> None:
        """3+ joins + no dimensions + >5000 rows → suspicious."""
        r = _exec(row_count=6_000, rows=[[1.0]] * 6_000)
        plan = _plan(
            joins=[_join("a", "b"), _join("b", "c"), _join("c", "d")],
            dimensions=[],
        )
        result = critic.critique(r, plan)
        c4 = next(d for d in result.dimensions if d.name == "suspicious_joins")
        assert c4.passed is False
        assert c4.score == 0.0

    def test_not_suspicious_with_dimensions(self, critic: ResultCritic) -> None:
        r = _exec(row_count=6_000, rows=[[1.0]] * 6_000)
        plan = _plan(
            joins=[_join("a", "b"), _join("b", "c"), _join("c", "d")],
            dimensions=[_dim("region")],
        )
        result = critic.critique(r, plan)
        c4 = next(d for d in result.dimensions if d.name == "suspicious_joins")
        assert c4.passed is True

    def test_not_suspicious_low_row_count(self, critic: ResultCritic) -> None:
        r = _exec(row_count=100, rows=[[1.0]] * 100)
        plan = _plan(joins=[_join("a", "b"), _join("b", "c"), _join("c", "d")])
        result = critic.critique(r, plan)
        c4 = next(d for d in result.dimensions if d.name == "suspicious_joins")
        assert c4.passed is True


# ── C5: aggregation mismatch ──────────────────────────────────────────────────


class TestC5AggregationMismatch:
    def test_count_measure_with_text_type_fails(self, critic: ResultCritic) -> None:
        r = _exec(columns=["order_count"], types=["varchar"],
                  rows=[["many"]], row_count=1)
        plan = _plan(measures=[_measure("orders", agg="COUNT", alias="order_count")])
        result = critic.critique(r, plan)
        c5 = next(d for d in result.dimensions if d.name == "aggregation_mismatch")
        assert c5.passed is False
        assert c5.score == 0.5

    def test_sum_on_id_column_fails(self, critic: ResultCritic) -> None:
        r = _exec(columns=["customer_id"], types=["int"],
                  rows=[[12345]], row_count=1)
        plan = _plan(measures=[_measure("customer_id", agg="SUM", alias="customer_id")])
        result = critic.critique(r, plan)
        c5 = next(d for d in result.dimensions if d.name == "aggregation_mismatch")
        assert c5.passed is False
        assert c5.score == 0.5

    def test_sum_on_revenue_passes(self, critic: ResultCritic) -> None:
        r = _exec(columns=["revenue"], types=["float"], rows=[[1000.0]], row_count=1)
        plan = _plan(measures=[_measure("revenue", agg="SUM", alias="revenue")])
        result = critic.critique(r, plan)
        c5 = next(d for d in result.dimensions if d.name == "aggregation_mismatch")
        assert c5.passed is True


# ── C6: insufficient sample ────────────────────────────────────────────────────


class TestC6InsufficientSample:
    @pytest.mark.parametrize("intent", ["comparison", "diagnostic", "time_series"])
    def test_small_sample_fails_for_statistical_intents(
        self, critic: ResultCritic, intent: str
    ) -> None:
        r = _exec(row_count=5, rows=[[i] for i in range(5)])
        result = critic.critique(r, _plan(), intent_type=intent)
        c6 = next(d for d in result.dimensions if d.name == "insufficient_sample")
        assert c6.passed is False
        assert c6.score == 0.3

    def test_sufficient_sample_passes(self, critic: ResultCritic) -> None:
        r = _exec(row_count=30, rows=[[i] for i in range(30)])
        result = critic.critique(r, _plan(), intent_type="comparison")
        c6 = next(d for d in result.dimensions if d.name == "insufficient_sample")
        assert c6.passed is True

    def test_skipped_for_non_statistical_intent(self, critic: ResultCritic) -> None:
        r = _exec(row_count=1, rows=[[1]])
        result = critic.critique(r, _plan(), intent_type="aggregation")
        c6 = next(d for d in result.dimensions if d.name == "insufficient_sample")
        assert c6.passed is True


# ── C7: intent alignment ───────────────────────────────────────────────────────


class TestC7IntentAlignment:
    def test_missing_alias_fails(self, critic: ResultCritic) -> None:
        r = _exec(columns=["wrong_col"], types=["float"], rows=[[1.0]], row_count=1)
        plan = _plan(measures=[_measure("revenue", alias="revenue")])
        result = critic.critique(r, plan)
        c7 = next(d for d in result.dimensions if d.name == "intent_alignment")
        assert c7.passed is False
        assert c7.score < 1.0

    def test_all_aliases_present_passes(self, critic: ResultCritic) -> None:
        r = _exec(columns=["revenue", "region"], types=["float", "str"],
                  rows=[[100.0, "East"]], row_count=1)
        plan = _plan(
            measures=[_measure("revenue", alias="revenue")],
            dimensions=[_dim("region")],
        )
        result = critic.critique(r, plan)
        c7 = next(d for d in result.dimensions if d.name == "intent_alignment")
        assert c7.passed is True

    def test_partial_aliases_gives_partial_score(self, critic: ResultCritic) -> None:
        """2 expected, 1 missing → completeness = 0.5."""
        r = _exec(columns=["revenue"], types=["float"], rows=[[100.0]], row_count=1)
        plan = _plan(
            measures=[_measure("revenue", alias="revenue"), _measure("profit", alias="profit")],
        )
        result = critic.critique(r, plan)
        c7 = next(d for d in result.dimensions if d.name == "intent_alignment")
        assert c7.passed is False
        assert abs(c7.score - 0.5) < 1e-6

    def test_no_expected_aliases_always_passes(self, critic: ResultCritic) -> None:
        """No measures/dimensions → nothing to align."""
        r = _exec(columns=["x"], types=["int"], rows=[[1]], row_count=1)
        result = critic.critique(r, _plan())
        c7 = next(d for d in result.dimensions if d.name == "intent_alignment")
        assert c7.passed is True


# ── C8: result anomaly ─────────────────────────────────────────────────────────


class TestC8ResultAnomaly:
    def test_anomaly_detected_high_outlier_rate(self, critic: ResultCritic) -> None:
        """Many extreme outliers (>10% outside 3×IQR) → flagged."""
        # 90 normal values + 10 extreme values = 10% outlier rate exactly at boundary
        # Use 11 outliers out of 50 = 22% > 10%
        normal = [[float(i)] for i in range(40)]
        outliers = [[float(i * 10_000)] for i in range(1, 12)]
        rows = normal + outliers
        r = QueryExecutionResult(
            column_names=["amount"],
            column_types=["float"],
            rows=rows,
            row_count=len(rows),
            is_truncated=False,
        )
        result = critic.critique(r, _plan())
        c8 = next(d for d in result.dimensions if d.name == "result_anomaly")
        assert c8.passed is False
        assert c8.score == 0.8  # Flag-only — score is 0.8, not 0.0

    def test_uniform_distribution_passes(self, critic: ResultCritic) -> None:
        rows = [[float(i)] for i in range(50)]
        r = QueryExecutionResult(
            column_names=["amount"],
            column_types=["float"],
            rows=rows,
            row_count=50,
            is_truncated=False,
        )
        result = critic.critique(r, _plan())
        c8 = next(d for d in result.dimensions if d.name == "result_anomaly")
        assert c8.passed is True

    def test_skipped_for_too_few_rows(self, critic: ResultCritic) -> None:
        r = _exec(row_count=3, rows=[[1.0], [2.0], [3.0]])
        result = critic.critique(r, _plan())
        c8 = next(d for d in result.dimensions if d.name == "result_anomaly")
        assert c8.passed is True


# ── Composite scoring ─────────────────────────────────────────────────────────


class TestCompositeScoring:
    def test_weights_sum_to_one(self) -> None:
        from uada.pipeline.result_critic import _DIM_WEIGHTS
        assert abs(sum(_DIM_WEIGHTS.values()) - 1.0) < 1e-9

    def test_all_passing_gives_score_one(self, critic: ResultCritic) -> None:
        """No failures → composite score == 1.0 (sufficient)."""
        rows = [[float(i)] for i in range(40)]
        r = QueryExecutionResult(
            column_names=["revenue"],
            column_types=["float"],
            rows=rows,
            row_count=40,
            is_truncated=False,
        )
        plan = _plan(measures=[_measure("revenue", alias="revenue")])
        result = critic.critique(r, plan)
        assert result.score == 1.0
        assert result.is_sufficient is True

    def test_empty_result_alone_fails_threshold(self, critic: ResultCritic) -> None:
        """C1 weight=0.25, score 0.0 → composite ≤ 0.75 < 0.8."""
        r = _exec(rows=[], row_count=0)
        result = critic.critique(r, _plan())
        assert result.score < ResultCritic.SUFFICIENCY_THRESHOLD
        assert result.is_sufficient is False

    def test_replan_hints_collected(self, critic: ResultCritic) -> None:
        r = _exec(rows=[], row_count=0)
        result = critic.critique(r, _plan())
        assert len(result.replan_hints) >= 1


# ── Safe-fail behaviour ────────────────────────────────────────────────────────


class TestSafeFail:
    def test_exception_returns_score_zero(self, critic: ResultCritic) -> None:
        """Passing None as result should trigger the safe-fail path, not raise."""
        result = critic.critique(None, _plan())  # type: ignore[arg-type]
        assert isinstance(result, CriticResult)
        assert result.score == 0.0
        assert result.is_sufficient is False
        assert result.empty_result is True
