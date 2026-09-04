"""
Tests for SQLGenerator (uada/pipeline/sql_generator.py).

Uses PydanticAI's TestModel -- no real LLM connection. Same testing
pattern as IntentExtractor: the Agent is built with the real
(unreachable) settings.llm_model under defer_model_check=True, then
swapped via agent.override(model=...) before any run happens.
"""

from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel

from uada.config import Settings
from uada.db.interface import QueryExecutionError
from uada.models.query_plan import QueryPlan, ResolvedMeasure, ResolvedTable, SQLDialect
from uada.pipeline.sql_generator import SQLGenerator

pytestmark = pytest.mark.unit


def _plan() -> QueryPlan:
    return QueryPlan(
        intent_question_type="aggregation",
        original_question="What was revenue last quarter?",
        dialect=SQLDialect.POSTGRESQL,
        primary_table=ResolvedTable(table_name="orders"),
        measures=[
            ResolvedMeasure(
                name="revenue", sql_expression="SUM(orders.revenue)", output_alias="revenue"
            )
        ],
    )


@pytest.fixture
def generator() -> SQLGenerator:
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    return SQLGenerator(settings)


class TestGenerate:
    async def test_returns_model_sql(self, generator: SQLGenerator) -> None:
        with generator.agent.override(
            model=TestModel(custom_output_text="SELECT SUM(orders.revenue) AS revenue FROM orders")
        ):
            sql = await generator.generate(_plan())
        assert sql == "SELECT SUM(orders.revenue) AS revenue FROM orders"

    async def test_strips_sql_fenced_markdown(self, generator: SQLGenerator) -> None:
        fenced = "```sql\nSELECT SUM(orders.revenue) AS revenue FROM orders\n```"
        with generator.agent.override(model=TestModel(custom_output_text=fenced)):
            sql = await generator.generate(_plan())
        assert sql == "SELECT SUM(orders.revenue) AS revenue FROM orders"

    async def test_strips_bare_fenced_markdown(self, generator: SQLGenerator) -> None:
        fenced = "```\nSELECT SUM(orders.revenue) AS revenue FROM orders\n```"
        with generator.agent.override(model=TestModel(custom_output_text=fenced)):
            sql = await generator.generate(_plan())
        assert sql == "SELECT SUM(orders.revenue) AS revenue FROM orders"

    async def test_empty_output_raises(self, generator: SQLGenerator) -> None:
        with (
            generator.agent.override(model=TestModel(custom_output_text="   ")),
            pytest.raises(ValueError, match="empty output"),
        ):
            await generator.generate(_plan())

    async def test_empty_fenced_block_raises(self, generator: SQLGenerator) -> None:
        with (
            generator.agent.override(model=TestModel(custom_output_text="```sql\n\n```")),
            pytest.raises(ValueError, match="empty output"),
        ):
            await generator.generate(_plan())


class TestRepair:
    async def test_returns_repaired_sql(self, generator: SQLGenerator) -> None:
        error = QueryExecutionError(
            "column orders.revenu does not exist", "SELECT orders.revenu FROM orders", "postgres"
        )
        with generator.agent.override(
            model=TestModel(custom_output_text="SELECT orders.revenue FROM orders")
        ):
            sql = await generator.repair("SELECT orders.revenu FROM orders", error, _plan())
        assert sql == "SELECT orders.revenue FROM orders"

    async def test_strips_markdown_from_repaired_sql(self, generator: SQLGenerator) -> None:
        error = QueryExecutionError("syntax error", "SELECT FROM orders", "postgres")
        with generator.agent.override(
            model=TestModel(custom_output_text="```sql\nSELECT * FROM orders\n```")
        ):
            sql = await generator.repair("SELECT FROM orders", error, _plan())
        assert sql == "SELECT * FROM orders"
