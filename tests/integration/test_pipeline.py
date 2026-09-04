"""
End-to-end pipeline integration tests (uada/pipeline/orchestrator.py).

Real SQLite in-memory database, real SCLManager/HybridRetriever/Embedder
(so schema linking is genuine, not mocked), TestModel/FunctionModel for
the three LLM stages (IntentExtractor, SQLGenerator, VisualisationGenerator)
-- no real LLM. Matches the project's own CI strategy for "integration"
tests: real SQLite + TestModel, no live model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import text

from uada.config import Settings
from uada.db.adapter import SQLAlchemyAdapter
from uada.pipeline.conversation_store import ConversationStore
from uada.pipeline.intent_extractor import IntentExtractor
from uada.pipeline.orchestrator import PipelineOrchestrator
from uada.pipeline.query_planner import QueryPlanner
from uada.pipeline.result_analyser import ResultAnalyser
from uada.pipeline.schema_linker import SchemaLinker
from uada.pipeline.sql_generator import SQLGenerator
from uada.pipeline.sql_validator import SQLValidator
from uada.pipeline.viz_generator import VisualisationGenerator
from uada.retrieval.bm25 import BM25Index
from uada.retrieval.chroma_backend import ChromaBackend
from uada.retrieval.embedder import Embedder
from uada.retrieval.hybrid import HybridRetriever
from uada.scl.manager import SCLManager
from uada.scl.schema import (
    ColumnDefinition,
    DatabaseMeta,
    JoinDefinition,
    MetricDefinition,
    SecurityPolicy,
    SemanticContextLayer,
    SemanticType,
    TableDefinition,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

OrchestratorFixture = tuple[
    PipelineOrchestrator, IntentExtractor, SQLGenerator, SQLAlchemyAdapter, ConversationStore
]

_INTENT_ARGS = {
    "question_type": "aggregation",
    "measures": ["revenue"],
    "raw_question": "What was total revenue?",
}


def _build_scl() -> SemanticContextLayer:
    return SemanticContextLayer(
        version="1.0",
        database=DatabaseMeta(name="test_db", dialect="sqlite"),
        tables=[
            TableDefinition(
                name="orders",
                description="Customer orders.",
                grain="order",
                columns=[
                    ColumnDefinition(name="id", type="int", is_primary_key=True),
                    ColumnDefinition(
                        name="customer_id",
                        type="int",
                        is_foreign_key=True,
                        references="customers.id",
                    ),
                    ColumnDefinition(
                        name="revenue",
                        type="float",
                        semantic_type=SemanticType.MEASURE,
                        aggregation="SUM",
                    ),
                    ColumnDefinition(
                        name="order_date",
                        type="datetime",
                        semantic_type=SemanticType.TEMPORAL,
                        is_temporal=True,
                        default_time_column=True,
                    ),
                ],
            ),
            TableDefinition(
                name="customers",
                description="Customer master data.",
                grain="customer",
                columns=[
                    ColumnDefinition(name="id", type="int", is_primary_key=True),
                    ColumnDefinition(name="name", type="str"),
                    ColumnDefinition(name="segment", type="str"),
                ],
            ),
        ],
        metrics=[
            MetricDefinition(
                name="revenue", description="Total revenue.", formula="SUM(orders.revenue)"
            )
        ],
        joins=[
            JoinDefinition(
                from_table="orders", to_table="customers", on="orders.customer_id = customers.id"
            )
        ],
        glossary=[],
        security=SecurityPolicy(),
    )


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    return Embedder()


@pytest.fixture(scope="module")
def scl_manager() -> SCLManager:
    return SCLManager(_build_scl())


@pytest.fixture(scope="module")
def retriever(
    tmp_path_factory: pytest.TempPathFactory, embedder: Embedder, scl_manager: SCLManager
) -> HybridRetriever:
    chroma_path = tmp_path_factory.mktemp("chroma_integration")
    backend = ChromaBackend(
        path=str(chroma_path), collection_name="integration_test", embedder=embedder
    )
    hybrid = HybridRetriever(vector_backend=backend, bm25_index=BM25Index())
    hybrid.build_index(scl_manager.to_indexable_documents())
    return hybrid


def _seed_database(adapter: SQLAlchemyAdapter) -> None:
    with adapter._engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "segment TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL "
                "REFERENCES customers(id), revenue REAL NOT NULL, order_date TEXT NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO customers VALUES (1, 'Acme', 'Enterprise')"))
        conn.execute(text("INSERT INTO orders VALUES (1, 1, 100.0, '2026-01-01')"))
        conn.execute(text("INSERT INTO orders VALUES (2, 1, 200.0, '2026-02-01')"))
        conn.commit()


@pytest.fixture
def orchestrator(
    tmp_path: Path, scl_manager: SCLManager, retriever: HybridRetriever
) -> OrchestratorFixture:
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    adapter = SQLAlchemyAdapter("sqlite:///:memory:", settings)
    _seed_database(adapter)

    schema_linker = SchemaLinker(retriever, scl_manager, settings)
    intent_extractor = IntentExtractor(settings)
    query_planner = QueryPlanner(scl_manager)
    sql_generator = SQLGenerator(settings)
    result_analyser = ResultAnalyser()
    viz_generator = VisualisationGenerator(settings)
    conversation_store = ConversationStore(settings, database_id="test_db")
    validator = SQLValidator(
        allowed_tables=scl_manager.get_allowed_tables(),
        max_subquery_depth=3,
        inject_limit=True,
        default_limit=1000,
    )
    pipeline = PipelineOrchestrator(
        db_adapter=adapter,
        scl_manager=scl_manager,
        schema_linker=schema_linker,
        intent_extractor=intent_extractor,
        query_planner=query_planner,
        sql_generator=sql_generator,
        result_analyser=result_analyser,
        viz_generator=viz_generator,
        conversation_store=conversation_store,
        validator=validator,
        settings=settings,
    )
    return pipeline, intent_extractor, sql_generator, adapter, conversation_store


def _intent_test_model() -> TestModel:
    return TestModel(custom_output_args=dict(_INTENT_ARGS))


class TestSuccessfulRun:
    async def test_full_pipeline_succeeds(
        self,
        orchestrator: OrchestratorFixture,
    ) -> None:
        pipeline, intent_extractor, sql_generator, _adapter, _store = orchestrator
        sql_model = TestModel(
            custom_output_text="SELECT SUM(orders.revenue) AS revenue FROM orders"
        )

        with (
            intent_extractor.agent.override(model=_intent_test_model()),
            sql_generator.agent.override(model=sql_model),
        ):
            response = await pipeline.run("What was total revenue?", "session-success")

        assert response.is_success is True
        assert response.sql is not None and "SUM(orders.revenue)" in response.sql
        assert response.row_count == 1
        assert response.answer is not None
        assert response.error is None


class TestSecurityRejection:
    async def test_security_violation_never_executes(
        self,
        orchestrator: OrchestratorFixture,
    ) -> None:
        pipeline, intent_extractor, sql_generator, adapter, store = orchestrator
        bad_sql_model = TestModel(custom_output_text="SELECT * FROM sqlite_master")

        call_count = 0
        original_execute = adapter.execute_query

        def spy(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            return original_execute(*args, **kwargs)  # type: ignore[arg-type]

        adapter.execute_query = spy  # type: ignore[method-assign]

        with (
            intent_extractor.agent.override(model=_intent_test_model()),
            sql_generator.agent.override(model=bad_sql_model),
        ):
            response = await pipeline.run("What was total revenue?", "session-security")

        assert response.is_success is False
        assert response.error is not None
        assert response.error.error_type == "security_violation"
        assert call_count == 0  # the SQL was never executed

        state = await store.load("session-security")
        assert state.turns[-1].status.value == "security_rejected"


class TestExecutionErrorRepair:
    async def test_execution_error_triggers_repair_not_security_stop(
        self,
        orchestrator: OrchestratorFixture,
    ) -> None:
        pipeline, intent_extractor, sql_generator, _adapter, _store = orchestrator
        call_count = 0

        def flaky(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # A column typo -- fails execution, not security (table is allowed).
                sql = "SELECT SUM(orders.revenu) AS revenue FROM orders"
            else:
                sql = "SELECT SUM(orders.revenue) AS revenue FROM orders"
            return ModelResponse(parts=[TextPart(content=sql)])

        with (
            intent_extractor.agent.override(model=_intent_test_model()),
            sql_generator.agent.override(model=FunctionModel(flaky)),
        ):
            response = await pipeline.run("What was total revenue?", "session-repair")

        assert call_count == 2  # one failed generate, one successful repair
        assert response.is_success is True
        assert response.error is None


class TestRetryBudget:
    async def test_retry_budget_exhausted_returns_error(
        self,
        orchestrator: OrchestratorFixture,
    ) -> None:
        pipeline, intent_extractor, sql_generator, _adapter, _store = orchestrator
        call_count = 0

        def always_broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            return ModelResponse(
                parts=[TextPart(content="SELECT SUM(orders.nonexistent_column) FROM orders")]
            )

        with (
            intent_extractor.agent.override(model=_intent_test_model()),
            sql_generator.agent.override(model=FunctionModel(always_broken)),
        ):
            response = await pipeline.run("What was total revenue?", "session-exhausted")

        # settings.llm_max_retries defaults to 2: 1 initial generate call +
        # 2 repair calls = 3 total SQL generator invocations.
        assert call_count == 3
        assert response.is_success is False
        assert response.error is not None
        assert response.error.error_type == "query_execution_error"
