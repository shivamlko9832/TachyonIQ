"""
Core Model Validation Tests
=============================
Tests for all Pydantic model validation logic.
NO LLM. NO DATABASE. Pure Python validation.

Run with: pytest tests/unit/test_models.py -v
"""

import pytest
from pydantic import ValidationError

from uada.models.intent import (
    AnalyticalIntent,
    Ambiguity,
    FilterOperator,
    OrderClause,
    QuestionType,
    RelativePeriod,
    SemanticFilter,
    SortDirection,
    TimeBucket,
    TimeRange,
    TimeRangeType,
)
from uada.models.conversation import (
    ActiveContext,
    ConversationState,
    ConversationTurn,
    TurnStatus,
)
from uada.models.result import (
    ColumnMeta,
    QueryResult,
    UADAError,
    UADAResponse,
    PipelineStage,
    VegaLiteSpec,
    ChartType,
)
from uada.scl.schema import (
    ColumnDefinition,
    DatabaseMeta,
    ExampleQuery,
    GlossaryTerm,
    JoinDefinition,
    MetricDefinition,
    SemanticContextLayer,
    SecurityPolicy,
    SemanticType,
    SQLDialect,
    TableDefinition,
)
from datetime import datetime, timezone


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_time_range(relative: RelativePeriod = RelativePeriod.LAST_QUARTER) -> TimeRange:
    return TimeRange(range_type=TimeRangeType.RELATIVE, relative_period=relative)


def make_time_series_range() -> TimeRange:
    return TimeRange(
        range_type=TimeRangeType.RELATIVE,
        relative_period=RelativePeriod.LAST_12_MONTHS,
        bucket=TimeBucket.MONTH,
    )


def make_base_intent(**kwargs) -> AnalyticalIntent:
    defaults = {
        "question_type": QuestionType.AGGREGATION,
        "measures": ["revenue"],
        "raw_question": "What was revenue last quarter?",
    }
    defaults.update(kwargs)
    return AnalyticalIntent(**defaults)


# ════════════════════════════════════════════════════════════════════
# AnalyticalIntent
# ════════════════════════════════════════════════════════════════════


class TestAnalyticalIntent:

    def test_simple_aggregation(self) -> None:
        intent = make_base_intent()
        assert intent.question_type == QuestionType.AGGREGATION
        assert intent.measures == ["revenue"]
        assert intent.references_prior_turn is False

    def test_time_series_requires_bucket(self) -> None:
        with pytest.raises(ValidationError, match="bucket"):
            make_base_intent(
                question_type=QuestionType.TIME_SERIES,
                time_range=TimeRange(
                    range_type=TimeRangeType.RELATIVE,
                    relative_period=RelativePeriod.LAST_12_MONTHS,
                    bucket=None,  # Missing bucket — should fail
                ),
            )

    def test_time_series_valid(self) -> None:
        intent = make_base_intent(
            question_type=QuestionType.TIME_SERIES,
            time_range=make_time_series_range(),
        )
        assert intent.question_type == QuestionType.TIME_SERIES

    def test_ranking_requires_order_by(self) -> None:
        with pytest.raises(ValidationError, match="order_by"):
            make_base_intent(
                question_type=QuestionType.RANKING,
                order_by=None,  # Missing — should fail
            )

    def test_ranking_valid(self) -> None:
        intent = make_base_intent(
            question_type=QuestionType.RANKING,
            order_by=[OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)],
            limit=10,
        )
        assert intent.limit == 10

    def test_follow_up_requires_prior_turn(self) -> None:
        with pytest.raises(ValidationError, match="references_prior_turn"):
            make_base_intent(
                question_type=QuestionType.FOLLOW_UP_REFINE,
                references_prior_turn=False,  # Missing — should fail
            )

    def test_follow_up_valid(self) -> None:
        intent = make_base_intent(
            question_type=QuestionType.FOLLOW_UP_REFINE,
            references_prior_turn=True,
            prior_turn_id=2,
        )
        assert intent.references_prior_turn is True

    def test_out_of_scope_no_measures(self) -> None:
        with pytest.raises(ValidationError, match="measures"):
            make_base_intent(
                question_type=QuestionType.OUT_OF_SCOPE,
                measures=["revenue"],  # Should not have measures
            )

    def test_out_of_scope_valid(self) -> None:
        intent = make_base_intent(
            question_type=QuestionType.OUT_OF_SCOPE,
            measures=[],
        )
        assert intent.question_type == QuestionType.OUT_OF_SCOPE

    def test_semantic_filter(self) -> None:
        intent = make_base_intent(
            filters=[
                SemanticFilter(
                    entity="customer_segment",
                    operator=FilterOperator.EQUALS,
                    value="Enterprise",
                    glossary_term="enterprise customer",
                )
            ]
        )
        assert len(intent.filters) == 1
        assert intent.filters[0].glossary_term == "enterprise customer"

    def test_ambiguity_tracking(self) -> None:
        intent = make_base_intent(
            ambiguities=[
                Ambiguity(
                    description="'active customers' is ambiguous",
                    resolution="Used 90-day definition per SCL",
                    requires_clarification=False,
                )
            ],
            confidence=0.8,
        )
        assert len(intent.ambiguities) == 1

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            make_base_intent(confidence=1.5)  # Above 1.0

    def test_absolute_time_range_requires_dates(self) -> None:
        with pytest.raises(ValidationError):
            TimeRange(
                range_type=TimeRangeType.ABSOLUTE,
                start_date=None,  # Missing
                end_date=None,
            )

    def test_relative_range_requires_period(self) -> None:
        with pytest.raises(ValidationError):
            TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=None,  # Missing
            )


# ════════════════════════════════════════════════════════════════════
# ConversationState
# ════════════════════════════════════════════════════════════════════


class TestConversationState:

    def _make_turn(self, turn_id: int, status: TurnStatus = TurnStatus.SUCCESS) -> ConversationTurn:
        return ConversationTurn(
            turn_id=turn_id,
            timestamp=datetime.now(tz=timezone.utc),
            user_question=f"Question {turn_id}",
            status=status,
            result_summary=f"Result summary {turn_id}",
            active_measures=["revenue"],
            active_dimensions=["region"],
        )

    def _make_state(self, n_turns: int = 0) -> ConversationState:
        state = ConversationState(
            session_id="test-session",
            database_id="test-db",
            created_at=datetime.now(tz=timezone.utc),
            last_updated=datetime.now(tz=timezone.utc),
        )
        for i in range(n_turns):
            state.turns.append(self._make_turn(i))
        return state

    def test_empty_state(self) -> None:
        state = self._make_state()
        assert state.turn_count == 0
        assert state.last_successful_turn is None

    def test_turn_count(self) -> None:
        state = self._make_state(n_turns=4)
        assert state.turn_count == 4

    def test_last_successful_turn(self) -> None:
        state = self._make_state(n_turns=3)
        last = state.last_successful_turn
        assert last is not None
        assert last.turn_id == 2

    def test_last_successful_skips_errors(self) -> None:
        state = self._make_state(n_turns=2)
        state.turns.append(self._make_turn(2, TurnStatus.ERROR))
        last = state.last_successful_turn
        assert last is not None
        assert last.turn_id == 1

    def test_get_recent_turns(self) -> None:
        state = self._make_state(n_turns=10)
        recent = state.get_recent_turns(n=3)
        assert len(recent) == 3
        assert recent[-1].turn_id == 9

    def test_edition_context_empty(self) -> None:
        state = self._make_state()
        ctx = state.get_edition_context()
        assert ctx == "No prior context."

    def test_edition_context_with_turns(self) -> None:
        state = self._make_state(n_turns=2)
        state.active_context = ActiveContext(
            current_measures=["revenue"],
            accumulated_filters=["customer_segment = Enterprise"],
        )
        ctx = state.get_edition_context()
        assert "revenue" in ctx
        assert "Enterprise" in ctx

    def test_active_context_accumulation(self) -> None:
        ctx = ActiveContext(
            current_measures=["revenue"],
            accumulated_filters=["region = West"],
        )
        ctx.accumulated_filters.append("customer_segment = Enterprise")
        assert len(ctx.accumulated_filters) == 2


# ════════════════════════════════════════════════════════════════════
# VegaLiteSpec validation
# ════════════════════════════════════════════════════════════════════


class TestVegaLiteSpec:

    def test_valid_spec(self) -> None:
        spec = VegaLiteSpec(
            spec={
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "mark": "bar",
                "encoding": {
                    "x": {"field": "month", "type": "temporal"},
                    "y": {"field": "revenue", "type": "quantitative"},
                },
            },
            chart_type=ChartType.BAR,
        )
        valid, reason = spec.is_valid()
        assert valid is True
        assert reason is None

    def test_missing_encoding(self) -> None:
        spec = VegaLiteSpec(
            spec={"mark": "bar"},
            chart_type=ChartType.BAR,
        )
        valid, reason = spec.is_valid()
        assert valid is False
        assert "encoding" in (reason or "")

    def test_missing_mark(self) -> None:
        spec = VegaLiteSpec(
            spec={"encoding": {"x": {"field": "a"}}},
            chart_type=ChartType.BAR,
        )
        valid, reason = spec.is_valid()
        assert valid is False


# ════════════════════════════════════════════════════════════════════
# QueryResult
# ════════════════════════════════════════════════════════════════════


class TestQueryResult:

    def test_empty_result(self) -> None:
        result = QueryResult(
            columns=[ColumnMeta(name="revenue", data_type="float")],
            rows=[],
            row_count=0,
            executed_sql="SELECT SUM(revenue) FROM orders",
            execution_time_ms=42.0,
            database_dialect="postgres",
        )
        assert result.is_empty()

    def test_column_names(self) -> None:
        result = QueryResult(
            columns=[
                ColumnMeta(name="region", data_type="str"),
                ColumnMeta(name="revenue", data_type="float"),
            ],
            rows=[["West", 123456.78]],
            row_count=1,
            executed_sql="SELECT region, SUM(revenue) FROM orders GROUP BY region",
            execution_time_ms=15.3,
            database_dialect="postgres",
        )
        assert result.column_names == ["region", "revenue"]
        assert not result.is_empty()

    def test_truncation_flag(self) -> None:
        result = QueryResult(
            columns=[ColumnMeta(name="id", data_type="int")],
            rows=[[i] for i in range(1000)],
            row_count=1000,
            is_truncated=True,
            truncated_at=1000,
            executed_sql="SELECT id FROM orders",
            execution_time_ms=200.0,
            database_dialect="postgres",
        )
        assert result.is_truncated
        assert result.truncated_at == 1000


# ════════════════════════════════════════════════════════════════════
# SemanticContextLayer (SCL)
# ════════════════════════════════════════════════════════════════════


class TestSCL:

    def _make_minimal_scl(self) -> SemanticContextLayer:
        return SemanticContextLayer(
            version="1.0",
            database=DatabaseMeta(
                name="test_db",
                dialect=SQLDialect.POSTGRESQL,
            ),
            tables=[
                TableDefinition(
                    name="orders",
                    description="Orders table",
                    columns=[
                        ColumnDefinition(
                            name="order_id", type="integer",
                            semantic_type=SemanticType.IDENTIFIER, is_primary_key=True,
                        ),
                        ColumnDefinition(
                            name="revenue", type="decimal",
                            semantic_type=SemanticType.MEASURE,
                        ),
                    ],
                ),
                TableDefinition(name="customers"),
            ],
            joins=[
                JoinDefinition(
                    from_table="orders",
                    to_table="customers",
                    on="orders.customer_id = customers.customer_id",
                )
            ],
        )

    def test_minimal_scl_valid(self) -> None:
        scl = self._make_minimal_scl()
        assert scl.version == "1.0"
        assert len(scl.tables) == 2

    def test_join_references_valid_tables(self) -> None:
        """Join validation: referenced tables must exist."""
        with pytest.raises(ValidationError, match="unknown table"):
            SemanticContextLayer(
                version="1.0",
                database=DatabaseMeta(name="test", dialect=SQLDialect.SQLITE),
                tables=[TableDefinition(name="orders")],
                joins=[
                    JoinDefinition(
                        from_table="orders",
                        to_table="nonexistent",  # Not in tables
                        on="orders.x = nonexistent.x",
                    )
                ],
            )

    def test_get_allowed_tables(self) -> None:
        scl = self._make_minimal_scl()
        scl.security.excluded_tables = ["orders"]
        allowed = scl.get_allowed_tables()
        assert "orders" not in allowed
        assert "customers" in allowed

    def test_included_tables_excludes_flagged(self) -> None:
        scl = self._make_minimal_scl()
        scl.tables[0].excluded = True
        included_names = [t.name for t in scl.included_tables]
        assert "orders" not in included_names

    def test_get_metric(self) -> None:
        scl = self._make_minimal_scl()
        scl.metrics = [
            MetricDefinition(name="revenue", description="Total revenue", formula="SUM(orders.revenue)"),
        ]
        m = scl.get_metric("revenue")
        assert m is not None
        assert m.formula == "SUM(orders.revenue)"

    def test_get_metric_by_alias(self) -> None:
        scl = self._make_minimal_scl()
        scl.metrics = [
            MetricDefinition(
                name="revenue",
                description="Total revenue",
                formula="SUM(orders.revenue)",
                aliases=["sales", "earnings"],
            ),
        ]
        m = scl.get_metric("sales")
        assert m is not None
        assert m.name == "revenue"

    def test_get_glossary_term(self) -> None:
        scl = self._make_minimal_scl()
        scl.glossary = [
            GlossaryTerm(
                term="enterprise customer",
                description="Segment = Enterprise",
                sql_filter="customers.segment = 'Enterprise'",
            )
        ]
        term = scl.get_glossary_term("enterprise customer")
        assert term is not None

    def test_get_glossary_term_case_insensitive(self) -> None:
        scl = self._make_minimal_scl()
        scl.glossary = [
            GlossaryTerm(term="Enterprise Customer", description="...", sql_filter="x")
        ]
        term = scl.get_glossary_term("enterprise customer")
        assert term is not None

    def test_default_time_column(self) -> None:
        scl = self._make_minimal_scl()
        scl.tables[0].columns.append(
            ColumnDefinition(
                name="order_date", type="timestamp",
                semantic_type=SemanticType.TEMPORAL,
                default_time_column=True,
            )
        )
        tc = scl.tables[0].default_time_column
        assert tc is not None
        assert tc.name == "order_date"
