"""
API integration tests (uada/api/app.py and routes/middleware).

Real SQLite in-memory database, real SCLManager/HybridRetriever/Embedder,
TestModel for the LLM stages -- no real LLM, no real HTTP server (FastAPI
TestClient talks to the ASGI app in-process).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import text

from uada.api.app import create_app
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
    MetricDefinition,
    SecurityPolicy,
    SemanticContextLayer,
    SemanticType,
    TableDefinition,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

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
                        name="revenue",
                        type="float",
                        semantic_type=SemanticType.MEASURE,
                        aggregation="SUM",
                    ),
                ],
            ),
        ],
        metrics=[
            MetricDefinition(
                name="revenue", description="Total revenue.", formula="SUM(orders.revenue)"
            )
        ],
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
    chroma_path = tmp_path_factory.mktemp("chroma_api")
    backend = ChromaBackend(path=str(chroma_path), collection_name="api_test", embedder=embedder)
    hybrid = HybridRetriever(vector_backend=backend, bm25_index=BM25Index())
    hybrid.build_index(scl_manager.to_indexable_documents())
    return hybrid


def _build_orchestrator(
    tmp_path: Path, scl_manager: SCLManager, retriever: HybridRetriever, settings: Settings
) -> PipelineOrchestrator:
    adapter = SQLAlchemyAdapter("sqlite:///:memory:", settings)
    with adapter._engine.connect() as conn:
        conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, revenue REAL NOT NULL)"))
        conn.execute(text("INSERT INTO orders VALUES (1, 100.0)"))
        conn.execute(text("INSERT INTO orders VALUES (2, 200.0)"))
        conn.commit()

    validator = SQLValidator(
        allowed_tables=scl_manager.get_allowed_tables(),
        max_subquery_depth=3,
        inject_limit=True,
        default_limit=1000,
    )
    return PipelineOrchestrator(
        db_adapter=adapter,
        scl_manager=scl_manager,
        schema_linker=SchemaLinker(retriever, scl_manager, settings),
        intent_extractor=IntentExtractor(settings),
        query_planner=QueryPlanner(scl_manager),
        sql_generator=SQLGenerator(settings),
        result_analyser=ResultAnalyser(),
        viz_generator=VisualisationGenerator(settings),
        conversation_store=ConversationStore(settings, database_id="test_db"),
        validator=validator,
        settings=settings,
    )


@pytest.fixture
def orchestrator(
    tmp_path: Path, scl_manager: SCLManager, retriever: HybridRetriever
) -> PipelineOrchestrator:
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    return _build_orchestrator(tmp_path, scl_manager, retriever, settings)


def _override_llms(orchestrator: PipelineOrchestrator):
    intent_model = TestModel(custom_output_args=dict(_INTENT_ARGS))
    sql_model = TestModel(custom_output_text="SELECT SUM(orders.revenue) AS revenue FROM orders")
    return (
        orchestrator._intent_extractor.agent.override(model=intent_model),
        orchestrator._sql_generator.agent.override(model=sql_model),
    )


class TestHealth:
    def test_returns_200_with_expected_shape(self, orchestrator: PipelineOrchestrator) -> None:
        app = create_app(orchestrator=orchestrator)
        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.1.0"
        assert body["database"] == "connected"


class TestQuery:
    def test_post_query_returns_uada_response(self, orchestrator: PipelineOrchestrator) -> None:
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.post("/query", json={"question": "What was total revenue?"})

        assert response.status_code == 200
        body = response.json()
        # is_success is a computed property, not a serialized field --
        # check the underlying `error` field it's derived from instead.
        assert body["error"] is None
        assert body["sql"] is not None
        assert "SUM(orders.revenue)" in body["sql"]
        assert body["session_id"]  # a UUID was generated

    def test_reuses_provided_session_id(self, orchestrator: PipelineOrchestrator) -> None:
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.post(
                "/query", json={"question": "What was total revenue?", "session_id": "my-session"}
            )

        assert response.json()["session_id"] == "my-session"

    def test_empty_question_is_rejected(self, orchestrator: PipelineOrchestrator) -> None:
        app = create_app(orchestrator=orchestrator)
        with TestClient(app) as client:
            response = client.post("/query", json={"question": ""})
        assert response.status_code == 422


class TestSession:
    def test_delete_session_returns_deleted_true(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        app = create_app(orchestrator=orchestrator)
        with TestClient(app) as client:
            response = client.delete("/session/some-session-id")

        assert response.status_code == 200
        assert response.json() == {"deleted": True}


class TestAuthMiddleware:
    def test_blocks_request_with_no_key(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:", api_key="secret-key")  # type: ignore[call-arg]
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 401

    def test_blocks_request_with_wrong_key(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:", api_key="secret-key")  # type: ignore[call-arg]
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.get("/health", headers={"Authorization": "Bearer wrong-key"})

        assert response.status_code == 401

    def test_allows_request_with_correct_key(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:", api_key="secret-key")  # type: ignore[call-arg]
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.get("/health", headers={"Authorization": "Bearer secret-key"})

        assert response.status_code == 200

    def test_no_api_key_configured_allows_all(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]  # api_key defaults to None
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200
