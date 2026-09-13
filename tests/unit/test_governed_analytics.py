"""Regression tests for the governed natural-language analytics path."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from uada.analytics.statistical_engine import StatisticalEngine
from uada.models.conversation import ActiveContext
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
from uada.models.result import AnalysedResult, ColumnMeta, QueryResult
from uada.models.schema_context import ColumnContext, SchemaContext, TableContext
from uada.pipeline.intent_normalizer import IntentNormalizer
from uada.pipeline.orchestrator import PipelineOrchestrator
from uada.pipeline.query_planner import QueryPlanner
from uada.pipeline.sql_generator import SQLGenerator
from uada.scl.loader import SCLLoader
from uada.scl.manager import SCLManager

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DEMO_SCL = ROOT / "config" / "demo_semantic_context.yaml"
DEMO_DB = ROOT / "data" / "demo.sqlite"


@pytest.fixture(scope="module")
def manager() -> SCLManager:
    return SCLManager(SCLLoader.load(DEMO_SCL))


def _context() -> SchemaContext:
    """Put the unrelated marketing region first to exercise join-aware resolution."""
    return SchemaContext(
        tables=[
            TableContext(
                table_name="marketing_performance",
                columns=[
                    ColumnContext(
                        column_name="region",
                        table_name="marketing_performance",
                        data_type="str",
                        semantic_type="dimension",
                    )
                ],
            ),
            TableContext(
                table_name="orders",
                columns=[
                    ColumnContext(
                        column_name="order_date",
                        table_name="orders",
                        data_type="date",
                        semantic_type="temporal",
                        is_temporal=True,
                        is_default_time_column=True,
                    )
                ],
            ),
            TableContext(
                table_name="customers",
                columns=[
                    ColumnContext(
                        column_name="region",
                        table_name="customers",
                        data_type="str",
                        semantic_type="dimension",
                    ),
                    ColumnContext(
                        column_name="account_name",
                        table_name="customers",
                        data_type="str",
                        semantic_type="text",
                    ),
                ],
            ),
            TableContext(
                table_name="customer_monthly_metrics",
                columns=[
                    ColumnContext(
                        column_name="month",
                        table_name="customer_monthly_metrics",
                        data_type="date",
                        semantic_type="temporal",
                        is_temporal=True,
                        is_default_time_column=True,
                    )
                ],
            ),
        ],
        dialect="sqlite",
        retrieval_query="regression test",
        total_retrieved=4,
    )


def _base_intent(question: str) -> AnalyticalIntent:
    return AnalyticalIntent(
        question_type=QuestionType.AGGREGATION,
        measures=[],
        raw_question=question,
    )


def test_forecast_profile_honours_horizon_and_joinable_region(
    manager: SCLManager,
) -> None:
    question = "Break the revenue forecast down by region for next three months."
    intent = IntentNormalizer(manager).normalise(
        _base_intent(question), question, _context()
    )

    assert intent.analysis_profile == "recognized_revenue_forecast"
    assert intent.measures == ["net_revenue"]
    assert intent.dimensions == ["region"]
    assert intent.forecast_horizon == 3

    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    assert plan.dimensions[0].sql_expression == "customers.region"
    assert [(join.from_table, join.to_table) for join in plan.joins] == [
        ("orders", "customers")
    ]
    sql = SQLGenerator._deterministic_analysis_sql(plan)
    assert sql is not None
    assert "customers.region" in sql
    assert "marketing_performance.region" not in sql


def test_churn_risk_profile_builds_a_governed_ranking(manager: SCLManager) -> None:
    question = "Which customer accounts have the highest churn risk and what evidence explains it?"
    extracted = _base_intent(question).model_copy(
        update={
            "filters": [
                SemanticFilter(
                    entity="churn_risk_score",
                    operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                    value=70,
                    glossary_term="high risk account",
                )
            ]
        }
    )
    intent = IntentNormalizer(manager).normalise(extracted, question, _context())
    validated = AnalyticalIntent.model_validate(intent.model_dump())

    assert validated.question_type == QuestionType.RANKING
    assert validated.analysis_profile == "customer_churn_risk_review"
    assert validated.limit == 5000
    assert validated.filters == []
    assert validated.order_by and validated.order_by[0].measure_or_dimension == (
        "average_churn_risk"
    )
    assert validated.measures == [
        "average_churn_risk",
        "feature_adoption",
        "average_monthly_active_users",
        "average_monthly_support_tickets",
    ]


def test_risk_narrative_and_fingerprint_come_from_executed_rows(
    manager: SCLManager,
) -> None:
    question = "Which customer accounts have the highest churn risk and what evidence explains it?"
    intent = IntentNormalizer(manager).normalise(
        _base_intent(question), question, _context()
    )
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    sql = SQLGenerator._deterministic_analysis_sql(plan)
    assert sql is not None

    with sqlite3.connect(DEMO_DB) as connection:
        cursor = connection.execute(sql)
        rows = [list(row) for row in cursor.fetchall()]
        names = [column[0] for column in cursor.description]

    query_result = QueryResult(
        columns=[ColumnMeta(name=name, data_type="unknown") for name in names],
        rows=rows,
        row_count=len(rows),
        executed_sql=sql,
        execution_time_ms=1.0,
        database_dialect="sqlite",
    )
    analysed = AnalysedResult(query_result=query_result)
    engine = StatisticalEngine()
    report = engine.analyse(analysed, intent, plan)
    narrative = engine.narrative(report, intent, plan)

    assert rows
    assert report["ranking"]["rows"][0]["account_name"] == rows[0][0]
    assert report["ranking"]["population_observations"] == len(rows) == 2500
    assert len(report["ranking"]["rows"]) == 10
    assert narrative is not None and rows[0][0] in narrative
    assert report["provenance"]["deterministic"] is True
    regression = report["tests"]["regression"]
    assert regression["status"] == "fitted"
    assert regression["observations"] == 2500
    assert regression["covariance_estimator"] == "HC3"
    assert regression["causal_interpretation"] is False
    correlations = report["tests"]["correlation"]
    assert correlations
    assert all("q_value" in item for item in correlations.values())
    compatibility_correlation = engine.correlation_result(report)
    assert compatibility_correlation is not None
    assert compatibility_correlation.method == "pearson_with_benjamini_hochberg_fdr"
    assert compatibility_correlation.top_pairs
    assert "q_value" in compatibility_correlation.top_pairs[0]

    distribution_plan = plan.model_copy(
        update={"analysis_operations": [*plan.analysis_operations, "distribution"]}
    )
    distribution_report = engine.analyse(analysed, intent, distribution_plan)
    distributions = distribution_report["tests"]["distribution"]
    assert distributions
    assert all("q_value" in item for item in distributions.values())

    changed = query_result.model_copy(
        update={"rows": [[rows[0][0], float(rows[0][1]) + 1, *rows[0][2:]], *rows[1:]]}
    )
    changed_report = engine.analyse(
        AnalysedResult(query_result=changed), intent, plan
    )
    assert (
        report["provenance"]["result_sha256"]
        != changed_report["provenance"]["result_sha256"]
    )


def test_compatibility_anomalies_are_only_governed_flagged_rows() -> None:
    engine = StatisticalEngine()
    result = engine.anomaly_result(
        {
            "anomalies": {
                "recognized_revenue": {
                    "method": "modified_z_score_mad",
                    "count": 1,
                    "rows": [
                        {
                            "row_index": 8,
                            "period": "2025-05-01",
                            "value": 10_800_000.0,
                            "score": 4.2,
                        }
                    ],
                },
                "profit": {
                    "method": "modified_z_score_mad",
                    "count": 0,
                    "rows": [],
                },
            }
        }
    )

    assert result is not None
    assert result.method == "modified_z_score_mad"
    assert result.anomaly_count == 1
    assert len(result.anomaly_rows) == 1
    assert result.anomaly_rows[0].is_anomaly is True
    assert result.anomaly_rows[0].column == "recognized_revenue"
    assert result.anomaly_rows[0].period == "2025-05-01"


def test_ui_fallback_reads_generated_vega_datasets() -> None:
    source = (ROOT / "uada" / "api" / "routes" / "ui.py").read_text(encoding="utf-8")
    assert "spec?.datasets?.[data.name]" in source
    assert "robust anomal" in source


def test_response_shortcut_builders_are_absent() -> None:
    source = (ROOT / "uada" / "pipeline" / "orchestrator.py").read_text(encoding="utf-8")
    forbidden = {
        "_forecast_response",
        "_executive_summary_response",
        "_is_forecast_question",
        "_is_executive_summary_question",
    }
    assert not forbidden.intersection(source.split())


def test_scalar_metric_narrative_uses_governed_result_and_semantic_unit(
    manager: SCLManager,
) -> None:
    question = "Tell me about the revenue generated for this month."
    intent = AnalyticalIntent(
        question_type=QuestionType.AGGREGATION,
        measures=["scorecard_revenue"],
        time_dimension="month",
        time_range=TimeRange(
            range_type=TimeRangeType.RELATIVE,
            relative_period=RelativePeriod.THIS_MONTH,
            bucket=TimeBucket.MONTH,
        ),
        raw_question=question,
    )
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    query_result = QueryResult(
        columns=[ColumnMeta(name="scorecard_revenue", data_type="float")],
        rows=[[9_698_813.69]],
        row_count=1,
        executed_sql=(
            "SELECT SUM(business_kpi_monthly.net_revenue) AS scorecard_revenue "
            "FROM business_kpi_monthly"
        ),
        execution_time_ms=1.0,
        database_dialect="sqlite",
    )
    analysed = AnalysedResult(query_result=query_result)
    engine = StatisticalEngine()
    report = engine.analyse(analysed, intent, plan)

    narrative = engine.narrative(report, intent, plan)

    assert narrative == (
        "Governed results for this month: revenue is $9,698,813.69. Each value was "
        "calculated from its semantic metric definition and the validated aggregate result."
    )
    assert report["aggregates"]["scorecard_revenue"]["value"] == 9_698_813.69


def test_explicit_comparison_replaces_model_time_noise() -> None:
    question = (
        "Compare Europe's recognized revenue for the latest six complete months "
        "with the preceding six months."
    )
    extracted = _base_intent(question).model_copy(
        update={
            "measures": ["net_revenue"],
            "dimensions": [],
            "time_dimension": "order_date",
            "filters": [
                SemanticFilter(
                    entity="month",
                    operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                    value="2026-03-01",
                    glossary_term="recent six months",
                ),
                SemanticFilter(
                    entity="tier",
                    operator=FilterOperator.EQUALS,
                    value="Enterprise",
                ),
                SemanticFilter(
                    entity="region",
                    operator=FilterOperator.EQUALS,
                    value="Europe",
                ),
            ],
        }
    )

    normalized = PipelineOrchestrator._normalise_explicit_comparison(
        extracted, question
    )

    assert normalized.question_type == QuestionType.COMPARISON
    assert normalized.measures == ["net_revenue"]
    assert normalized.dimensions == []
    assert normalized.time_dimension == "order_date"
    assert normalized.time_range is not None
    assert normalized.time_comparison is not None
    assert [(item.entity, item.value) for item in normalized.filters] == [
        ("tier", "Enterprise"),
        ("region", "Europe"),
    ]


def test_multi_measure_scalar_narrative_reports_every_governed_value(
    manager: SCLManager,
) -> None:
    question = "What was our revenue and operating margin in 2026?"
    intent = IntentNormalizer(manager).fallback(question, _context())
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    query_result = QueryResult(
        columns=[
            ColumnMeta(name="waste_revenue", data_type="float", unit="USD"),
            ColumnMeta(name="waste_operating_margin_pct", data_type="float", unit="%"),
        ],
        rows=[[268_403_459.60, 36.470458]],
        row_count=1,
        executed_sql="SELECT governed waste metrics",
        execution_time_ms=1.0,
        database_dialect="sqlite",
    )
    analysed = AnalysedResult(query_result=query_result)
    engine = StatisticalEngine()
    report = engine.analyse(analysed, intent, plan)

    narrative = engine.narrative(report, intent, plan)

    assert narrative is not None
    assert "revenue is $268,403,459.60" in narrative
    assert "operating margin pct is 36.47%" in narrative
    expected_end = min(date(2027, 1, 1), date.today().replace(day=1)) - timedelta(days=1)
    assert f"2026-01-01 through {expected_end.isoformat()}" in narrative


def test_unresolved_model_time_bucket_falls_back_to_governed_date_column(
    manager: SCLManager,
) -> None:
    intent = AnalyticalIntent(
        question_type=QuestionType.COMPARISON,
        measures=["net_revenue"],
        time_dimension="month",
        time_range=TimeRange(
            range_type=TimeRangeType.RELATIVE,
            relative_period=RelativePeriod.THIS_YEAR,
        ),
        time_comparison=TimeComparison(
            comparison_period=RelativePeriod.LAST_YEAR,
            comparison_label="Last year",
        ),
        raw_question="Compare revenue this year vs last year.",
    )

    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())

    assert plan.primary_table.table_name == "orders"
    assert plan.time_resolution is not None
    assert plan.time_resolution.time_column == "orders.order_date"
    assert "orders.month" not in plan.time_resolution.filter_sql


def test_explicit_waste_comparison_keeps_waste_semantics(
    manager: SCLManager,
) -> None:
    question = "Compare waste revenue from January to August 2026 with January to August 2025."
    intent = IntentNormalizer(manager).fallback(question, _context())

    normalized = PipelineOrchestrator._normalise_explicit_comparison(intent, question)

    assert normalized.measures == ["waste_revenue"]
    assert "net_revenue" not in normalized.measures
    assert normalized.time_range is not None
    assert normalized.time_range.start_date == "2026-01-01"
    assert normalized.time_range.end_date == "2026-09-01"
    assert normalized.time_comparison is not None
    assert normalized.time_comparison.comparison_start_date == "2025-01-01"
    assert normalized.time_comparison.comparison_end_date == "2025-09-01"
    plan = QueryPlanner(manager, strict_semantics=True).plan(normalized, _context())
    assert plan.time_resolution is not None
    assert plan.time_resolution.time_column == "wm_finance_monthly.month"


def test_governed_profile_discards_invented_filters(manager: SCLManager) -> None:
    question = (
        "Give me an executive summary of revenue, profitability, customer health, "
        "support and marketing for Europe."
    )
    extracted = _base_intent(question).model_copy(
        update={
            "filters": [
                SemanticFilter(
                    entity="region",
                    operator=FilterOperator.EQUALS,
                    value="Europe",
                ),
                SemanticFilter(
                    entity="customer_health",
                    operator=FilterOperator.EQUALS,
                    value="at risk",
                ),
            ]
        }
    )

    normalized = IntentNormalizer(manager).normalise(
        extracted, question, _context()
    )

    assert normalized.analysis_profile == "executive_business_review"
    assert [(item.entity, item.value) for item in normalized.filters] == [
        ("region", "Europe")
    ]


def test_governed_profile_replaces_invented_planning_fields(
    manager: SCLManager,
) -> None:
    question = (
        "Give me an executive summary of revenue, profitability, customer health, "
        "support and marketing."
    )
    extracted = _base_intent(question).model_copy(
        update={
            "time_comparison": TimeComparison(
                comparison_period=RelativePeriod.LAST_YEAR,
                comparison_label="Invented comparison",
            ),
            "order_by": [
                OrderClause(
                    measure_or_dimension="invented_metric",
                    direction=SortDirection.DESC,
                )
            ],
            "limit": 1,
            "references_prior_turn": True,
            "prior_turn_id": 99,
            "follow_up_description": "Invented follow-up",
        }
    )

    normalized = IntentNormalizer(manager).normalise(
        extracted, question, _context()
    )

    assert normalized.analysis_profile == "executive_business_review"
    assert normalized.time_comparison is None
    assert normalized.order_by is None
    assert normalized.limit is None
    assert normalized.references_prior_turn is False
    assert normalized.prior_turn_id is None
    assert normalized.follow_up_description is None

    plan = QueryPlanner(manager, strict_semantics=True).plan(
        normalized, _context()
    )
    assert plan.primary_table.table_name == "business_kpi_monthly"
    assert plan.joins == []


def test_waste_revenue_margin_profile_executes_only_governed_metrics(
    manager: SCLManager,
) -> None:
    question = "What was our revenue and operating margin in 2026?"
    extracted = _base_intent(question).model_copy(
        update={
            "time_range": TimeRange(
                range_type=TimeRangeType.ABSOLUTE,
                start_date="2026-01-01",
                end_date="2026-09-01",
            )
        }
    )
    intent = IntentNormalizer(manager).normalise(extracted, question, _context())

    assert intent.analysis_profile == "waste_revenue_margin_review"
    assert intent.measures == ["waste_revenue", "waste_operating_margin_pct"]
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    assert plan.primary_table.table_name == "wm_finance_monthly"
    assert plan.joins == []

    sql = SQLGenerator._deterministic_analysis_sql(plan)
    assert sql is not None
    assert "wm_finance_monthly.revenue" in sql
    assert "orders.net_revenue" not in sql
    with sqlite3.connect(DEMO_DB) as connection:
        revenue, margin = connection.execute(sql).fetchone()
    assert revenue > 0
    assert 0 < margin < 100


def test_waste_year_aggregate_drops_unrequested_model_time_bucket(
    manager: SCLManager,
) -> None:
    question = "What was our revenue and operating margin in 2026?"
    extracted = _base_intent(question).model_copy(
        update={
            "time_range": TimeRange(
                range_type=TimeRangeType.ABSOLUTE,
                start_date="2026-01-01",
                end_date="2027-01-01",
                bucket=TimeBucket.MONTH,
            )
        }
    )

    intent = IntentNormalizer(manager).normalise(extracted, question, _context())

    assert intent.time_range is not None
    assert intent.time_range.bucket is None
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    sql = SQLGenerator._deterministic_analysis_sql(plan)
    assert sql is not None
    assert "GROUP BY" not in sql


def test_waste_cost_driver_profile_uses_declared_multihop_join(
    manager: SCLManager,
) -> None:
    question = "What is driving our margin decline?"
    intent = IntentNormalizer(manager).normalise(
        _base_intent(question), question, _context()
    )

    assert intent.analysis_profile == "waste_cost_driver_review"
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    assert plan.primary_table.table_name == "wm_finance_monthly"
    assert [(join.from_table, join.to_table) for join in plan.joins] == [
        ("wm_finance_monthly", "wm_contracts"),
        ("wm_contracts", "wm_customers"),
    ]
    assert plan.dimensions[0].sql_expression == "wm_customers.region"

    sql = SQLGenerator._deterministic_analysis_sql(plan)
    assert sql is not None
    with sqlite3.connect(DEMO_DB) as connection:
        rows = connection.execute(sql).fetchall()
    assert len(rows) >= 100


def test_qualified_dimension_from_model_is_validated_against_semantic_contract(
    manager: SCLManager,
) -> None:
    intent = AnalyticalIntent(
        question_type=QuestionType.AGGREGATION,
        measures=["net_revenue"],
        dimensions=["customers.tier"],
        raw_question="Show revenue for enterprise customers",
    )
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())

    assert plan.dimensions[0].sql_expression == "customers.tier"
    assert [(join.from_table, join.to_table) for join in plan.joins] == [
        ("orders", "customers")
    ]


def test_governed_intent_fallback_executes_waste_profile(
    manager: SCLManager,
) -> None:
    intent = IntentNormalizer(manager).fallback(
        "What was our revenue and operating margin in 2026?",
        _context(),
    )

    assert intent.analysis_profile == "waste_revenue_margin_review"
    assert intent.measures == ["waste_revenue", "waste_operating_margin_pct"]
    assert intent.time_range is not None
    assert intent.time_range.start_date == "2026-01-01"
    assert AnalysisOperation.DESCRIPTIVE in intent.analysis_operations

    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    sql = SQLGenerator._deterministic_analysis_sql(plan)
    assert sql is not None
    with sqlite3.connect(DEMO_DB) as connection:
        revenue, margin = connection.execute(sql).fetchone()
    assert revenue == pytest.approx(268_403_459.60, abs=0.02)
    assert margin == pytest.approx(36.4705, abs=0.001)


def test_governed_intent_fallback_carries_context_and_adds_west_filter(
    manager: SCLManager,
) -> None:
    prior = ActiveContext(
        current_measures=["waste_revenue", "waste_operating_margin_pct"],
        current_dimensions=["region"],
    )
    intent = IntentNormalizer(manager).fallback("Now only show the West", _context(), prior)

    assert intent.references_prior_turn is True
    assert intent.measures == prior.current_measures
    assert intent.dimensions == prior.current_dimensions
    assert [(item.entity, item.value) for item in intent.filters] == [("region", "West")]


def test_governed_intent_fallback_refuses_unknown_metric(
    manager: SCLManager,
) -> None:
    intent = IntentNormalizer(manager).fallback(
        "What is employee engagement by office?",
        _context(),
    )
    assert intent.question_type == QuestionType.OUT_OF_SCOPE
    assert intent.measures == []


def test_follow_up_context_reapplies_typed_filters() -> None:
    prior = ActiveContext(
        current_measures=["waste_revenue"],
        current_dimensions=["region"],
        semantic_filters=[
            SemanticFilter(entity="region", operator=FilterOperator.EQUALS, value="West")
        ],
        time_range=TimeRange(
            range_type=TimeRangeType.ABSOLUTE,
            start_date="2026-01-01",
            end_date="2026-09-01",
        ),
    )
    follow_up = AnalyticalIntent(
        question_type=QuestionType.AGGREGATION,
        measures=[],
        dimensions=["service_line"],
        references_prior_turn=True,
        raw_question="Now break that down by service line",
    )

    inherited = PipelineOrchestrator._inherit_active_context(follow_up, prior)
    assert inherited.measures == ["waste_revenue"]
    assert inherited.dimensions == ["service_line"]
    assert inherited.filters == prior.semantic_filters
    assert inherited.time_range == prior.time_range


def test_fallback_comparison_reuses_active_window_with_matched_prior_period(
    manager: SCLManager,
) -> None:
    prior = ActiveContext(
        current_measures=["waste_revenue", "waste_operating_margin_pct"],
        time_range=TimeRange(
            range_type=TimeRangeType.ABSOLUTE,
            start_date="2026-01-01",
            end_date="2026-09-01",
        ),
    )
    intent = IntentNormalizer(manager).fallback("Compare that with last year", _context(), prior)

    assert intent.question_type == QuestionType.COMPARISON
    assert intent.time_range is not None
    assert intent.time_range.start_date == "2026-01-01"
    assert intent.time_range.end_date == "2026-09-01"
    assert intent.time_comparison is not None
    assert intent.time_comparison.comparison_start_date == "2025-01-01"
    assert intent.time_comparison.comparison_end_date == "2025-09-01"


def test_contextual_same_months_comparison_uses_prior_typed_window() -> None:
    prior = ActiveContext(
        current_measures=["waste_revenue", "waste_operating_margin_pct"],
        current_dimensions=["region"],
        time_range=TimeRange(
            range_type=TimeRangeType.ABSOLUTE,
            start_date="2026-01-01",
            end_date="2027-01-01",
        ),
    )
    extracted = AnalyticalIntent(
        question_type=QuestionType.FOLLOW_UP_EXTEND,
        measures=[],
        references_prior_turn=True,
        time_comparison=TimeComparison(
            comparison_period=RelativePeriod.LAST_YEAR,
            comparison_label="Model supplied but follow-up typed comparison",
        ),
        raw_question="Compare that with the same months in 2025.",
    )

    resolved = PipelineOrchestrator._resolve_contextual_comparison(
        extracted,
        extracted.raw_question,
        prior,
    )
    inherited = PipelineOrchestrator._inherit_active_context(resolved, prior)

    assert inherited.measures == prior.current_measures
    assert inherited.dimensions == prior.current_dimensions
    assert inherited.time_range is not None
    assert inherited.time_range.start_date == "2026-01-01"
    assert inherited.time_range.end_date == "2026-09-01"
    assert inherited.time_comparison is not None
    assert inherited.time_comparison.comparison_start_date == "2025-01-01"
    assert inherited.time_comparison.comparison_end_date == "2025-09-01"


def test_grouped_narrative_lists_observed_waste_regions(manager: SCLManager) -> None:
    intent = AnalyticalIntent(
        question_type=QuestionType.AGGREGATION,
        measures=["waste_revenue", "waste_operating_margin_pct"],
        dimensions=["region"],
        time_range=TimeRange(
            range_type=TimeRangeType.ABSOLUTE,
            start_date="2026-01-01",
            end_date="2026-09-01",
        ),
        raw_question="Break that down by region.",
    )
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    sql = """
        SELECT SUM(f.revenue) AS waste_revenue,
               100.0 * SUM(f.operating_profit) / NULLIF(SUM(f.revenue), 0)
                   AS waste_operating_margin_pct,
               c.region AS region
        FROM wm_finance_monthly AS f
        JOIN wm_contracts AS k ON f.contract_id = k.id
        JOIN wm_customers AS c ON k.customer_id = c.id
        WHERE f.month >= '2026-01-01' AND f.month < '2026-09-01'
        GROUP BY c.region
    """
    with sqlite3.connect(DEMO_DB) as connection:
        rows = connection.execute(sql).fetchall()
    result = QueryResult(
        columns=[
            ColumnMeta(name="waste_revenue", data_type="float", unit="USD"),
            ColumnMeta(name="waste_operating_margin_pct", data_type="float", unit="%"),
            ColumnMeta(name="region", data_type="str"),
        ],
        rows=[list(row) for row in rows],
        row_count=len(rows),
        executed_sql=sql,
        execution_time_ms=1.0,
        database_dialect="sqlite",
    )
    analysed = AnalysedResult(query_result=result)
    engine = StatisticalEngine()
    report = engine.analyse(analysed, intent, plan)

    narrative = engine.narrative(report, intent, plan)

    assert narrative is not None
    for region in ("Midwest", "Northeast", "Southeast", "West"):
        assert region in narrative
    assert "$92,587,901.74" in narrative
    assert "33.83%" in narrative


def test_comparison_narrative_reports_all_measures_and_units(
    manager: SCLManager,
) -> None:
    intent = AnalyticalIntent(
        question_type=QuestionType.COMPARISON,
        measures=["waste_revenue", "waste_operating_margin_pct"],
        time_range=TimeRange(
            range_type=TimeRangeType.ABSOLUTE,
            start_date="2026-01-01",
            end_date="2026-09-01",
        ),
        time_comparison=TimeComparison(
            comparison_period=RelativePeriod.LAST_YEAR,
            comparison_label="Same observed months in 2025",
            comparison_start_date="2025-01-01",
            comparison_end_date="2025-09-01",
        ),
        raw_question="Compare that with the same months in 2025.",
    )
    plan = QueryPlanner(manager, strict_semantics=True).plan(intent, _context())
    result = QueryResult(
        columns=[
            ColumnMeta(name="waste_revenue", data_type="float", unit="USD"),
            ColumnMeta(name="waste_operating_margin_pct", data_type="float", unit="%"),
            ColumnMeta(name="comparison_period", data_type="str"),
        ],
        rows=[
            [51_474_889.90, 33.826410, "Latest period"],
            [49_034_692.47, 40.917050, "Same observed months in 2025"],
        ],
        row_count=2,
        executed_sql="SELECT governed comparison",
        execution_time_ms=1.0,
        database_dialect="sqlite",
    )
    analysed = AnalysedResult(query_result=result)
    engine = StatisticalEngine()
    report = engine.analyse(analysed, intent, plan)

    narrative = engine.narrative(report, intent, plan)

    assert narrative is not None
    assert "revenue increased by 4.98%" in narrative
    assert "$49,034,692.47" in narrative
    assert "$51,474,889.90" in narrative
    assert "operating margin pct decreased by 7.09 percentage points" in narrative
    assert "40.92%" in narrative and "33.83%" in narrative
