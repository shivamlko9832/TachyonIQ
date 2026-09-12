"""Regression tests for the governed natural-language analytics path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from uada.analytics.statistical_engine import StatisticalEngine
from uada.models.intent import (
    AnalyticalIntent,
    FilterOperator,
    OrderClause,
    QuestionType,
    RelativePeriod,
    SemanticFilter,
    SortDirection,
    TimeComparison,
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


def test_response_shortcut_builders_are_absent() -> None:
    source = (ROOT / "uada" / "pipeline" / "orchestrator.py").read_text(encoding="utf-8")
    forbidden = {
        "_forecast_response",
        "_executive_summary_response",
        "_is_forecast_question",
        "_is_executive_summary_question",
    }
    assert not forbidden.intersection(source.split())


def test_explicit_comparison_replaces_model_time_noise() -> None:
    question = (
        "Compare Europe's recognized revenue for the latest six complete months "
        "with the preceding six months."
    )
    extracted = _base_intent(question).model_copy(
        update={
            "dimensions": ["comparison_period"],
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
            ],
        }
    )

    normalized = PipelineOrchestrator._normalise_explicit_comparison(
        extracted, question
    )

    assert normalized.question_type == QuestionType.COMPARISON
    assert normalized.measures == ["net_revenue"]
    assert normalized.dimensions == []
    assert normalized.time_range is not None
    assert normalized.time_comparison is not None
    assert [(item.entity, item.value) for item in normalized.filters] == [
        ("tier", "Enterprise"),
        ("region", "Europe"),
    ]


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
