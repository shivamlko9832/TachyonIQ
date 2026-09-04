"""
Tests for IntentExtractor (uada/pipeline/intent_extractor.py).

Uses PydanticAI's TestModel/FunctionModel -- no real LLM connection. The
Agent is built with the real (unreachable) settings.llm_model under
`defer_model_check=True`, then swapped via `agent.override(model=...)`,
PydanticAI's documented pattern for testing agents. This only tests
pipeline plumbing (prompt construction, retry-on-validation-failure) --
not LLM extraction quality, which is the evaluation suite's job.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from uada.config import Settings
from uada.models.intent import AnalyticalIntent, QuestionType
from uada.models.schema_context import ColumnContext, SchemaContext, TableContext
from uada.pipeline.intent_extractor import IntentExtractor

pytestmark = pytest.mark.unit


def _schema_context() -> SchemaContext:
    return SchemaContext(
        tables=[
            TableContext(
                table_name="orders",
                description="Customer orders.",
                columns=[
                    ColumnContext(column_name="revenue", table_name="orders", data_type="float"),
                ],
            )
        ],
        dialect="postgresql",
        retrieval_query="What was revenue last quarter?",
        total_retrieved=1,
    )


@pytest.fixture
def extractor() -> IntentExtractor:
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    return IntentExtractor(settings)


class TestExtract:
    async def test_returns_fixed_analytical_intent(self, extractor: IntentExtractor) -> None:
        fixed_args = {
            "question_type": "aggregation",
            "measures": ["revenue"],
            "raw_question": "What was revenue last quarter?",
        }
        with extractor.agent.override(model=TestModel(custom_output_args=fixed_args)):
            intent = await extractor.extract(
                question="What was revenue last quarter?",
                schema_context=_schema_context(),
                conversation_context="",
            )

        assert isinstance(intent, AnalyticalIntent)
        assert intent.question_type == QuestionType.AGGREGATION
        assert intent.measures == ["revenue"]

    async def test_prompt_includes_schema_and_conversation_context(
        self, extractor: IntentExtractor
    ) -> None:
        captured_prompt = ""

        def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal captured_prompt
            user_part = messages[-1].parts[-1]
            captured_prompt = user_part.content  # type: ignore[union-attr]
            tool_name = info.output_tools[0].name
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=tool_name,
                        args={
                            "question_type": "aggregation",
                            "measures": ["revenue"],
                            "raw_question": "What was revenue last quarter?",
                        },
                    )
                ]
            )

        with extractor.agent.override(model=FunctionModel(capture)):
            await extractor.extract(
                question="What was revenue last quarter?",
                schema_context=_schema_context(),
                conversation_context="Previous turn: showed monthly revenue.",
            )

        assert "orders" in captured_prompt
        assert "Previous turn: showed monthly revenue." in captured_prompt
        assert "What was revenue last quarter?" in captured_prompt

    async def test_validation_failure_triggers_retry(self, extractor: IntentExtractor) -> None:
        call_count = 0

        def flaky(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            tool_name = info.output_tools[0].name
            if call_count == 1:
                # Invalid: RANKING requires order_by, omitted here.
                args = {
                    "question_type": "ranking",
                    "measures": ["revenue"],
                    "raw_question": "Top regions by revenue",
                }
            else:
                args = {
                    "question_type": "ranking",
                    "measures": ["revenue"],
                    "dimensions": ["region"],
                    "order_by": [{"measure_or_dimension": "revenue", "direction": "desc"}],
                    "limit": 10,
                    "raw_question": "Top regions by revenue",
                }
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

        with extractor.agent.override(model=FunctionModel(flaky)):
            intent = await extractor.extract(
                question="Top regions by revenue",
                schema_context=_schema_context(),
                conversation_context="",
            )

        assert call_count == 2
        assert intent.question_type == QuestionType.RANKING
        assert intent.order_by is not None
        assert intent.order_by[0].measure_or_dimension == "revenue"
