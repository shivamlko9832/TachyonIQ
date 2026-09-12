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
            response = client.delete("/session/some-session-id")

        assert response.status_code == 401

    def test_blocks_request_with_wrong_key(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:", api_key="secret-key")  # type: ignore[call-arg]
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.delete(
                "/session/some-session-id", headers={"Authorization": "Bearer wrong-key"}
            )

        assert response.status_code == 401

    def test_allows_request_with_correct_key(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:", api_key="secret-key")  # type: ignore[call-arg]
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.delete(
                "/session/some-session-id", headers={"Authorization": "Bearer secret-key"}
            )

        assert response.status_code == 200

    def test_no_api_key_configured_allows_all(self, orchestrator: PipelineOrchestrator) -> None:
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]  # api_key defaults to None
        app = create_app(orchestrator=orchestrator, settings=settings)

        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200


# ── P2-1: SSE Stream ──────────────────────────────────────────────────────────

def _parse_sse_events(body: str) -> list[dict[str, str]]:
    """Parse a raw SSE response body into a list of {event, data} dicts."""
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
        elif line == "" and current:
            events.append(current)
            current = {}
    if current:
        events.append(current)
    return events


class TestSSEStream:
    """
    Integration tests for GET /query/stream — SSE event structure.

    Because sse-starlette is installed, the endpoint returns a proper
    Server-Sent Events stream.  TestClient collects the full body
    synchronously so we can parse the event stream inline.
    """

    def test_stream_returns_progress_then_result_events(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """The SSE body must contain ≥ 1 progress events and exactly 1 result event."""
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?"},
            )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)

        progress_events = [e for e in events if e.get("event") == "progress"]
        result_events = [e for e in events if e.get("event") == "result"]

        assert len(progress_events) >= 1, "Expected at least one progress event"
        assert len(result_events) == 1, "Expected exactly one result event"

    def test_stream_progress_events_have_stage_field(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """Each progress event's data must include a 'stage' key."""
        import json

        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?"},
            )

        events = _parse_sse_events(response.text)
        progress_events = [e for e in events if e.get("event") == "progress"]
        for ev in progress_events:
            payload = json.loads(ev["data"])
            assert "stage" in payload, f"progress event missing 'stage': {payload}"
            assert "status" in payload, f"progress event missing 'status': {payload}"

    def test_stream_result_event_is_valid_uada_response(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """The result event's data must parse as a UADAResponse (has 'sql', 'session_id')."""
        import json

        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?", "session_id": "stream-session"},
            )

        events = _parse_sse_events(response.text)
        result_events = [e for e in events if e.get("event") == "result"]
        assert len(result_events) == 1

        payload = json.loads(result_events[0]["data"])
        assert "session_id" in payload
        assert payload["session_id"] == "stream-session"
        assert "sql" in payload

    def test_stream_uses_session_id_from_query_params(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """session_id provided as a query param must appear in the result event."""
        import json

        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?", "session_id": "my-stream-id"},
            )

        events = _parse_sse_events(response.text)
        result_events = [e for e in events if e.get("event") == "result"]
        payload = json.loads(result_events[0]["data"])
        assert payload["session_id"] == "my-stream-id"

    def test_stream_content_type_is_text_event_stream(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """The SSE endpoint must advertise the correct content-type header."""
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?"},
            )

        assert "text/event-stream" in response.headers.get("content-type", "")


# ── P2-5: Schema Refresh ──────────────────────────────────────────────────────

class TestSchemaRefresh:
    """
    Integration tests for POST /connections/{id}/refresh.

    Creates an in-memory SQLite connection via the real ConnectionManager,
    calls the refresh endpoint, and verifies both the response shape and the
    security invariant that sample_values are never exposed.
    """

    @pytest.fixture
    def refresh_app(self, orchestrator: PipelineOrchestrator, tmp_path):
        """App with a real ConnectionManager and a pre-registered SQLite connection."""
        from uada.db.connection_manager import ConnectionConfig, DatabaseConnectionManager

        mgr = DatabaseConnectionManager(tmp_path / "connections")
        # Register a fresh in-memory SQLite db with two tables
        import sqlite3
        import tempfile
        import os

        # Use a temp file rather than :memory: so the adapter can re-open it
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tf.close()
        conn = sqlite3.connect(tf.name)
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, revenue REAL)")
        conn.execute("INSERT INTO orders VALUES (1, 100.0), (2, 200.0)")
        conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
        conn.commit()
        conn.close()

        cfg = ConnectionConfig(
            name="test-sqlite",
            dialect="sqlite",
            database=tf.name,
        )
        connection_id = mgr.register(cfg)

        app = create_app(orchestrator=orchestrator)
        app.state.connection_manager = mgr

        yield app, connection_id

        # Cleanup temp file
        try:
            os.unlink(tf.name)
        except OSError:
            pass

    def test_refresh_returns_200(self, refresh_app) -> None:
        app, cid = refresh_app
        with TestClient(app) as client:
            response = client.post(f"/connections/{cid}/refresh")
        assert response.status_code == 200

    def test_refresh_response_shape(self, refresh_app) -> None:
        """Response must include the required schema summary fields."""
        app, cid = refresh_app
        with TestClient(app) as client:
            response = client.post(f"/connections/{cid}/refresh")

        body = response.json()
        assert body["connection_id"] == cid
        assert "schema_fingerprint" in body
        assert isinstance(body["table_count"], int)
        assert body["table_count"] >= 1
        assert isinstance(body["column_count"], int)
        assert isinstance(body["relationship_count"], int)
        assert "tables" in body
        assert isinstance(body["tables"], list)

    def test_browse_schema_returns_metadata_without_profiling(self, refresh_app) -> None:
        app, cid = refresh_app
        with TestClient(app) as client:
            response = client.get(f"/connections/{cid}/schema")

        assert response.status_code == 200
        body = response.json()
        assert body["table_count"] >= 1
        assert body["column_count"] >= 1
        assert body["profiled_table_count"] == 0
        assert body["profiled_column_count"] == 0
        assert body["tables"][0]["columns"]

    def test_semantic_context_exposes_public_contract(self, refresh_app) -> None:
        """The workspace explorer receives definitions without security internals."""
        app, _cid = refresh_app
        with TestClient(app) as client:
            response = client.get("/semantic-context")

        assert response.status_code == 200
        body = response.json()
        assert body["database"]["name"] == "test_db"
        assert body["tables"][0]["name"] == "orders"
        assert body["metrics"][0]["name"] == "revenue"
        assert "security" not in body

    def test_refresh_tables_have_column_summaries(self, refresh_app) -> None:
        """Each table entry must have column_count and a columns list."""
        app, cid = refresh_app
        with TestClient(app) as client:
            response = client.post(f"/connections/{cid}/refresh")

        tables = response.json()["tables"]
        assert len(tables) >= 1
        for table in tables:
            assert "table_name" in table
            assert "column_count" in table
            assert isinstance(table["columns"], list)
            assert len(table["columns"]) > 0

    def test_refresh_does_not_expose_sample_values(self, refresh_app) -> None:
        """
        SECURITY: sample_values must never appear anywhere in the response body.
        This is a hard invariant — sample values could contain PII.
        """
        app, cid = refresh_app
        with TestClient(app) as client:
            response = client.post(f"/connections/{cid}/refresh")

        # Walk the entire JSON recursively; sample_values must not be a key
        def find_keys(obj, key: str) -> bool:
            if isinstance(obj, dict):
                if key in obj:
                    return True
                return any(find_keys(v, key) for v in obj.values())
            if isinstance(obj, list):
                return any(find_keys(item, key) for item in obj)
            return False

        body = response.json()
        assert not find_keys(body, "sample_values"), (
            "'sample_values' must NEVER appear in the /refresh response body "
            "(security invariant: sample data could contain PII)"
        )

    def test_refresh_column_entries_have_expected_fields(self, refresh_app) -> None:
        """Column summaries must include name, data_type, and profiling fields."""
        app, cid = refresh_app
        with TestClient(app) as client:
            response = client.post(f"/connections/{cid}/refresh")

        tables = response.json()["tables"]
        for table in tables:
            for col in table["columns"]:
                assert "column_name" in col
                assert "data_type" in col
                # These may be None for unprofiled columns but keys must exist
                assert "null_pct" in col
                assert "distinct_count" in col
                assert "is_temporal" in col

    def test_refresh_returns_404_for_unknown_connection(
        self, orchestrator: PipelineOrchestrator, tmp_path
    ) -> None:
        """Requesting refresh for a non-existent connection must return 404."""
        from uada.db.connection_manager import DatabaseConnectionManager

        mgr = DatabaseConnectionManager(tmp_path / "connections")
        app = create_app(orchestrator=orchestrator)
        app.state.connection_manager = mgr

        with TestClient(app) as client:
            response = client.post("/connections/does-not-exist/refresh")

        assert response.status_code == 404
