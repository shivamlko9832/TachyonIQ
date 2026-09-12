"""
Tests for QueryPlanner (uada/pipeline/query_planner.py).

No LLM, no database (AnalyticalIntents are built by hand, not extracted).
SCLManager is built from config/semantic_context.yaml. A few of the
SQLite-dialect time fragments are additionally executed against a real
SQLite adapter, as a genuine syntax check beyond string comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from uada.config import Settings
from uada.db.adapter import SQLAlchemyAdapter
from uada.models.intent import (
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
from uada.models.query_plan import SQLDialect
from uada.models.schema_context import ColumnContext, SchemaContext, TableContext
from uada.pipeline.query_planner import QueryPlanner
from uada.scl.loader import SCLLoader
from uada.scl.manager import SCLManager

pytestmark = pytest.mark.unit

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "semantic_context.yaml"
DEMO_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "demo_semantic_context.yaml"


@pytest.fixture(scope="module")
def scl_manager() -> SCLManager:
    return SCLManager(SCLLoader.load(CONFIG_PATH))


@pytest.fixture
def planner(scl_manager: SCLManager) -> QueryPlanner:
    return QueryPlanner(scl_manager)


def _schema_context(dialect: str = "postgresql") -> SchemaContext:
    return SchemaContext(
        tables=[
            TableContext(
                table_name="orders",
                columns=[
                    ColumnContext(column_name="revenue", table_name="orders", data_type="float"),
                    ColumnContext(column_name="region", table_name="orders", data_type="str"),
                    ColumnContext(
                        column_name="order_date",
                        table_name="orders",
                        data_type="datetime",
                        is_temporal=True,
                        is_default_time_column=True,
                    ),
                ],
            ),
            TableContext(
                table_name="customers",
                columns=[
                    ColumnContext(column_name="segment", table_name="customers", data_type="str"),
                ],
            ),
        ],
        dialect=dialect,
        retrieval_query="q",
        total_retrieved=2,
    )


class TestAggregation:
    def test_revenue_last_quarter(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE, relative_period=RelativePeriod.LAST_QUARTER
            ),
            raw_question="What was revenue last quarter?",
        )
        plan = planner.plan(intent, _schema_context())

        assert plan.primary_table.table_name == "orders"
        assert plan.measures[0].name == "revenue"
        assert plan.measures[0].sql_expression == "SUM(orders.revenue)"
        assert plan.time_resolution is not None
        assert plan.time_resolution.filter_sql == (
            "orders.order_date >= DATE_TRUNC('quarter', NOW() - INTERVAL '3 months') "
            "AND orders.order_date < DATE_TRUNC('quarter', NOW())"
        )

    def test_unresolved_measure_falls_back_to_sum(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["not_a_real_metric"],
            raw_question="What was not_a_real_metric?",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.measures[0].sql_expression == "SUM(orders.not_a_real_metric)"


class TestTimeSeries:
    def test_monthly_revenue_last_12_months(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.TIME_SERIES,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE,
                relative_period=RelativePeriod.LAST_12_MONTHS,
                bucket=TimeBucket.MONTH,
            ),
            raw_question="Show monthly revenue for the last 12 months",
        )
        plan = planner.plan(intent, _schema_context())

        assert plan.time_resolution is not None
        assert plan.time_resolution.filter_sql == (
            "orders.order_date >= NOW() - INTERVAL '12 months'"
        )
        assert plan.time_resolution.group_by_sql == (
            "DATE_TRUNC('month', orders.order_date) AS period"
        )


class TestRanking:
    def test_top_10_regions_by_revenue(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.RANKING,
            measures=["revenue"],
            dimensions=["region"],
            order_by=[OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)],
            limit=10,
            raw_question="Top 10 regions by revenue",
        )
        plan = planner.plan(intent, _schema_context())

        assert plan.dimensions[0].sql_expression == "orders.region"
        assert plan.dimensions[0].output_alias == "region"
        assert len(plan.order_by) == 1
        assert plan.order_by[0].sql_expression == "revenue"
        assert plan.order_by[0].direction == "DESC"
        assert plan.limit == 10

    def test_duplicate_dimension_prefers_metric_fact_table(self) -> None:
        demo_planner = QueryPlanner(SCLManager(SCLLoader.load(DEMO_CONFIG_PATH)))
        schema = SchemaContext(
            tables=[
                TableContext(
                    table_name="customers",
                    columns=[
                        ColumnContext(column_name="region", table_name="customers", data_type="str")
                    ],
                ),
                TableContext(
                    table_name="marketing_performance",
                    columns=[
                        ColumnContext(
                            column_name="region",
                            table_name="marketing_performance",
                            data_type="str",
                        )
                    ],
                ),
            ],
            dialect="sqlite",
            retrieval_query="marketing spend by region",
            total_retrieved=2,
        )
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["marketing_spend"],
            dimensions=["region"],
            raw_question="Show marketing spend by region",
        )

        plan = demo_planner.plan(intent, schema)

        assert plan.primary_table.table_name == "marketing_performance"
        assert plan.dimensions[0].sql_expression == "marketing_performance.region"
        assert plan.joins == []

    def test_unresolved_dimension_falls_back_to_primary_table(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.RANKING,
            measures=["revenue"],
            dimensions=["not_a_real_column"],
            order_by=[OrderClause(measure_or_dimension="revenue", direction=SortDirection.DESC)],
            raw_question="Top not_a_real_column by revenue",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.dimensions[0].sql_expression == "orders.not_a_real_column"


class TestFilter:
    def test_glossary_term_resolves_to_sql_filter(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.FILTER,
            measures=["revenue"],
            filters=[
                SemanticFilter(
                    entity="segment",
                    operator=FilterOperator.EQUALS,
                    value="Enterprise",
                    glossary_term="enterprise customer",
                )
            ],
            raw_question="Only show enterprise customers.",
        )
        plan = planner.plan(intent, _schema_context())

        assert plan.filters[0].sql_fragment == "customers.segment = 'Enterprise'"

    def test_filter_on_non_primary_table_adds_join(self, planner: QueryPlanner) -> None:
        # measures=['revenue'] makes 'orders' the primary table, but the
        # glossary filter references 'customers' -- that table must still
        # be joined in, or the generated SQL's WHERE clause would reference
        # a table absent from FROM/JOIN.
        intent = AnalyticalIntent(
            question_type=QuestionType.FILTER,
            measures=["revenue"],
            filters=[
                SemanticFilter(
                    entity="segment",
                    operator=FilterOperator.EQUALS,
                    value="Enterprise",
                    glossary_term="enterprise customer",
                )
            ],
            raw_question="Only show enterprise customers.",
        )
        plan = planner.plan(intent, _schema_context())

        assert plan.primary_table.table_name == "orders"
        assert [t.table_name for t in plan.additional_tables] == ["customers"]
        assert len(plan.joins) == 1
        assert plan.joins[0].from_table == "orders"
        assert plan.joins[0].to_table == "customers"

    def test_direct_filter_without_glossary_term(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.FILTER,
            measures=["revenue"],
            dimensions=["region"],
            filters=[SemanticFilter(entity="region", operator=FilterOperator.EQUALS, value="EMEA")],
            raw_question="Only show EMEA.",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.filters[0].sql_fragment == "orders.region = 'EMEA'"

    def test_in_operator_renders_value_list(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.FILTER,
            measures=["revenue"],
            dimensions=["region"],
            filters=[
                SemanticFilter(entity="region", operator=FilterOperator.IN, value=["EMEA", "APAC"])
            ],
            raw_question="Only EMEA or APAC.",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.filters[0].sql_fragment == "orders.region IN ('EMEA', 'APAC')"

    def test_is_null_operator_ignores_value(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.FILTER,
            measures=["revenue"],
            dimensions=["region"],
            filters=[SemanticFilter(entity="region", operator=FilterOperator.IS_NULL)],
            raw_question="Where region is unknown.",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.filters[0].sql_fragment == "orders.region IS NULL"


class TestDialectSpecificTimeFilters:
    """The phase spec's literal per-dialect examples, verified exactly."""

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            (
                "postgresql",
                "orders.order_date >= DATE_TRUNC('quarter', NOW() - INTERVAL '3 months') "
                "AND orders.order_date < DATE_TRUNC('quarter', NOW())",
            ),
            ("mysql", "orders.order_date >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH)"),
            ("sqlite", "orders.order_date >= date('now', '-3 months')"),
            ("tsql", "orders.order_date >= DATEADD(quarter, -1, GETDATE())"),
        ],
    )
    def test_last_quarter_per_dialect(
        self, planner: QueryPlanner, dialect: str, expected: str
    ) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE, relative_period=RelativePeriod.LAST_QUARTER
            ),
            raw_question="What was revenue last quarter?",
        )
        plan = planner.plan(intent, _schema_context(dialect=dialect))
        assert plan.time_resolution is not None
        assert plan.time_resolution.filter_sql == expected

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("postgresql", "DATE_TRUNC('month', orders.order_date) AS period"),
            ("mysql", "DATE_FORMAT(orders.order_date, '%Y-%m') AS period"),
            ("sqlite", "strftime('%Y-%m', orders.order_date) AS period"),
        ],
    )
    def test_month_bucket_per_dialect(
        self, planner: QueryPlanner, dialect: str, expected: str
    ) -> None:
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
        plan = planner.plan(intent, _schema_context(dialect=dialect))
        assert plan.time_resolution is not None
        assert plan.time_resolution.group_by_sql == expected

    def test_unknown_dialect_raises(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            raw_question="q",
        )
        with pytest.raises(ValueError, match="Unsupported dialect"):
            planner.plan(intent, _schema_context(dialect="oracle"))


class TestAbsoluteAndComparison:
    def test_absolute_range(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.ABSOLUTE,
                start_date="2025-01-01",
                end_date="2025-04-01",
            ),
            raw_question="Revenue from Jan to Apr 2025",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.time_resolution is not None
        assert plan.time_resolution.filter_sql == (
            "orders.order_date >= '2025-01-01' AND orders.order_date < '2025-04-01'"
        )

    def test_comparison_flag_and_cte_name(self, planner: QueryPlanner) -> None:
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
        plan = planner.plan(intent, _schema_context())
        assert plan.is_comparison is True
        assert plan.comparison_cte_name == "comparison_period"
        assert plan.time_resolution is not None
        assert plan.time_resolution.comparison_label == "Last Year"
        assert plan.time_resolution.comparison_filter_sql == (
            "orders.order_date >= DATE_TRUNC('year', NOW() - INTERVAL '1 year') "
            "AND orders.order_date < DATE_TRUNC('year', NOW())"
        )
        assert plan.estimated_complexity == "complex"


class TestFollowUp:
    def test_follow_up_flags_propagate(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.FOLLOW_UP_REFINE,
            measures=["revenue"],
            references_prior_turn=True,
            follow_up_description="Adds enterprise filter to prior query.",
            raw_question="Only enterprise customers.",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.is_follow_up is True
        assert plan.base_plan_summary == "Adds enterprise filter to prior query."


class TestComplexityHeuristic:
    def test_simple_when_no_joins_or_time(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue"], raw_question="q"
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.estimated_complexity == "simple"

    def test_moderate_with_time_resolution(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE, relative_period=RelativePeriod.LAST_QUARTER
            ),
            raw_question="q",
        )
        plan = planner.plan(intent, _schema_context())
        assert plan.estimated_complexity == "moderate"


class TestSQLiteExecutability:
    """Real syntax check: the generated SQLite time fragments actually run."""

    def test_last_quarter_filter_executes_on_sqlite(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION,
            measures=["revenue"],
            time_range=TimeRange(
                range_type=TimeRangeType.RELATIVE, relative_period=RelativePeriod.LAST_QUARTER
            ),
            raw_question="What was revenue last quarter?",
        )
        plan = planner.plan(intent, _schema_context(dialect="sqlite"))
        assert plan.time_resolution is not None

        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        adapter = SQLAlchemyAdapter("sqlite:///:memory:", settings)
        with adapter._engine.connect() as conn:
            conn.execute(
                text("CREATE TABLE orders (revenue REAL NOT NULL, order_date TEXT NOT NULL)")
            )
            conn.commit()
        result = adapter.execute_query(
            f"SELECT SUM(revenue) FROM orders WHERE {plan.time_resolution.filter_sql}"
        )
        assert result.row_count == 1

    def test_month_bucket_executes_on_sqlite(self, planner: QueryPlanner) -> None:
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
        plan = planner.plan(intent, _schema_context(dialect="sqlite"))
        assert plan.time_resolution is not None

        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        adapter = SQLAlchemyAdapter("sqlite:///:memory:", settings)
        with adapter._engine.connect() as conn:
            conn.execute(
                text("CREATE TABLE orders (revenue REAL NOT NULL, order_date TEXT NOT NULL)")
            )
            conn.execute(
                text("INSERT INTO orders VALUES (100.0, '2026-01-15'), (200.0, '2026-01-20')")
            )
            conn.commit()
        # group_by_sql is a "<expr> AS period" fragment for the SELECT list;
        # GROUP BY groups by the alias, per standard SQL (not by repeating
        # the fragment, which would invalidly re-include "AS period").
        result = adapter.execute_query(
            f"SELECT {plan.time_resolution.group_by_sql}, SUM(revenue) FROM orders GROUP BY period"
        )
        assert result.rows == [["2026-01", 300.0]]


class TestDialectEnumMapping:
    def test_postgresql_maps_to_sqlglot_postgres(self, planner: QueryPlanner) -> None:
        intent = AnalyticalIntent(
            question_type=QuestionType.AGGREGATION, measures=["revenue"], raw_question="q"
        )
        plan = planner.plan(intent, _schema_context(dialect="postgresql"))
        assert plan.dialect == SQLDialect.POSTGRESQL
        assert plan.dialect.value == "postgres"
