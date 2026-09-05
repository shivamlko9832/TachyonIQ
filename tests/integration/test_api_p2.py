"""
P2 API integration tests: SSE streaming (/query/stream) and schema refresh
(/connections/{id}/refresh).

These tests build a lightweight PipelineOrchestrator that stubs out the
embedding-based SchemaLinker so no chromadb or sentence-transformers are
needed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import text

from uada.api.app import create_app
from uada.config import Settings
from uada.db.adapter import SQLAlchemyAdapter
from uada.models.schema_context import (
    ColumnContext,
    SchemaContext,
    TableContext,
)
from uada.pipeline.conversation_store import ConversationStore
from uada.pipeline.intent_extractor import IntentExtractor
from uada.pipeline.orchestrator import PipelineOrchestrator
from uada.pipeline.query_planner import QueryPlanner
from uada.pipeline.result_analyser import ResultAnalyser
from uada.pipeline.schema_linker import SchemaLinker
from uada.pipeline.sql_generator import SQLGenerator
from uada.pipeline.sql_validator import SQLValidator
from uada.pipeline.viz_generator import VisualisationGenerator
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

pytestmark = pytest.mark.integration


# ── Shared LLM overrides ──────────────────────────────────────────────────────

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


def _build_schema_context() -> SchemaContext:
    """A minimal SchemaContext referencing the orders.revenue column."""
    return SchemaContext(
        tables=[
            TableContext(
                table_name="orders",
                description="Customer orders.",
                columns=[
                    ColumnContext(
                        column_name="id",
                        table_name="orders",
                        data_type="int",
                        is_primary_key=True,
                    ),
                    ColumnContext(
                        column_name="revenue",
                        table_name="orders",
                        data_type="float",
                        semantic_type="measure",
                        aggregation="SUM",
                    ),
                ],
            )
        ],
        metrics=[],
        joins=[],
        dialect="sqlite",
        retrieval_query="What was total revenue?",
        total_retrieved=1,
    )


def _make_stub_schema_linker(scl_manager: SCLManager, settings: Settings) -> SchemaLinker:
    """
    Return a SchemaLinker whose `link()` method is mocked to return a
    pre-built SchemaContext, so chromadb / sentence-transformers are never
    imported.
    """
    stub = MagicMock(spec=SchemaLinker)
    stub.link.return_value = _build_schema_context()
    return stub  # type: ignore[return-value]


def _build_orchestrator(settings: Settings, scl_manager: SCLManager) -> PipelineOrchestrator:
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
    schema_linker = _make_stub_schema_linker(scl_manager, settings)

    return PipelineOrchestrator(
        db_adapter=adapter,
        scl_manager=scl_manager,
        schema_linker=schema_linker,
        intent_extractor=IntentExtractor(settings),
        query_planner=QueryPlanner(scl_manager),
        sql_generator=SQLGenerator(settings),
        result_analyser=ResultAnalyser(),
        viz_generator=VisualisationGenerator(settings),
        conversation_store=ConversationStore(settings, database_id="test_db"),
        validator=validator,
        settings=settings,
    )


@pytest.fixture(scope="module")
def scl_manager() -> SCLManager:
    return SCLManager(_build_scl())


@pytest.fixture
def orchestrator(scl_manager: SCLManager) -> PipelineOrchestrator:
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    return _build_orchestrator(settings, scl_manager)


def _override_llms(orchestrator: PipelineOrchestrator):
    intent_model = TestModel(custom_output_args=dict(_INTENT_ARGS))
    sql_model = TestModel(custom_output_text="SELECT SUM(orders.revenue) AS revenue FROM orders")
    return (
        orchestrator._intent_extractor.agent.override(model=intent_model),
        orchestrator._sql_generator.agent.override(model=sql_model),
    )


# ── SSE helpers ───────────────────────────────────────────────────────────────

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


# ── P2-1: SSE Streaming ───────────────────────────────────────────────────────

class TestSSEStream:
    """
    Integration tests for GET /query/stream — SSE event structure.

    sse-starlette is installed; the endpoint returns a proper
    Server-Sent Events stream. TestClient collects the full body
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

    def test_stream_progress_events_have_stage_and_status_fields(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """Each progress event's data must include 'stage' and 'status' keys."""
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?"},
            )

        events = _parse_sse_events(response.text)
        progress_events = [e for e in events if e.get("event") == "progress"]
        assert len(progress_events) >= 1

        for ev in progress_events:
            payload = json.loads(ev["data"])
            assert "stage" in payload, f"progress event missing 'stage': {payload}"
            assert "status" in payload, f"progress event missing 'status': {payload}"

    def test_stream_result_event_contains_session_id(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """The result event's data must include session_id."""
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?", "session_id": "stream-abc"},
            )

        events = _parse_sse_events(response.text)
        result_events = [e for e in events if e.get("event") == "result"]
        assert len(result_events) == 1

        payload = json.loads(result_events[0]["data"])
        assert payload["session_id"] == "stream-abc"

    def test_stream_result_event_contains_sql(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """The result event must carry the generated SQL."""
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?"},
            )

        events = _parse_sse_events(response.text)
        result_events = [e for e in events if e.get("event") == "result"]
        payload = json.loads(result_events[0]["data"])
        assert "sql" in payload
        assert payload["sql"] is not None

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

    def test_stream_generates_session_id_when_not_provided(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """When no session_id param given, result must still carry a non-empty session_id."""
        app = create_app(orchestrator=orchestrator)
        override_intent, override_sql = _override_llms(orchestrator)

        with TestClient(app) as client, override_intent, override_sql:
            response = client.get(
                "/query/stream",
                params={"question": "What was total revenue?"},
            )

        events = _parse_sse_events(response.text)
        result_events = [e for e in events if e.get("event") == "result"]
        payload = json.loads(result_events[0]["data"])
        assert payload.get("session_id"), "session_id must be set even when not provided"


# ── P2-5: Schema Refresh ──────────────────────────────────────────────────────

class TestSchemaRefresh:
    """
    Integration tests for POST /connections/{id}/refresh.

    Creates a temp-file SQLite connection via the real DatabaseConnectionManager,
    calls the refresh endpoint, and verifies both the response shape and the
    security invariant that sample_values are never exposed.

    NOTE: create_app() sets app.state.connection_manager inside its lifespan,
    so we always re-override it AFTER entering the TestClient context manager.
    """

    @pytest.fixture
    def refresh_app(self, orchestrator: PipelineOrchestrator):
        """App + pre-registered SQLite connection + the manager instance."""
        from uada.db.connection_manager import ConnectionConfig, DatabaseConnectionManager

        mgr = DatabaseConnectionManager()

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

        # Yield the app, connection id, AND manager so tests can re-inject
        # mgr after the lifespan has run inside TestClient.
        app = create_app(orchestrator=orchestrator)
        yield app, connection_id, mgr

        try:
            os.unlink(tf.name)
        except OSError:
            pass

    def test_refresh_returns_200(self, refresh_app: Any) -> None:
        app, cid, mgr = refresh_app
        with TestClient(app) as client:
            app.state.connection_manager = mgr
            response = client.post(f"/connections/{cid}/refresh")
        assert response.status_code == 200

    def test_refresh_response_has_required_fields(self, refresh_app: Any) -> None:
        """Response must include the required schema summary fields."""
        app, cid, mgr = refresh_app
        with TestClient(app) as client:
            app.state.connection_manager = mgr
            response = client.post(f"/connections/{cid}/refresh")

        body = response.json()
        assert body["connection_id"] == cid
        assert "schema_fingerprint" in body
        assert isinstance(body["table_count"], int)
        assert body["table_count"] >= 1
        assert isinstance(body["column_count"], int)
        assert isinstance(body["tables"], list)

    def test_refresh_tables_have_column_summaries(self, refresh_app: Any) -> None:
        """Each table entry must have a columns list with at least one entry."""
        app, cid, mgr = refresh_app
        with TestClient(app) as client:
            app.state.connection_manager = mgr
            response = client.post(f"/connections/{cid}/refresh")

        tables = response.json()["tables"]
        assert len(tables) >= 1
        for table in tables:
            assert "table_name" in table
            assert isinstance(table["columns"], list)
            assert len(table["columns"]) > 0

    def test_refresh_column_entries_have_expected_fields(self, refresh_app: Any) -> None:
        """Column summaries must include name, data_type, and profiling fields."""
        app, cid, mgr = refresh_app
        with TestClient(app) as client:
            app.state.connection_manager = mgr
            response = client.post(f"/connections/{cid}/refresh")

        for table in response.json()["tables"]:
            for col in table["columns"]:
                assert "column_name" in col
                assert "data_type" in col
                # Keys must exist even if profiling returned None
                assert "null_pct" in col
                assert "distinct_count" in col
                assert "is_temporal" in col

    def test_refresh_does_not_expose_sample_values(self, refresh_app: Any) -> None:
        """
        SECURITY INVARIANT: sample_values must NEVER appear anywhere in the
        /refresh response body — sample data could contain PII.
        """
        app, cid, mgr = refresh_app
        with TestClient(app) as client:
            app.state.connection_manager = mgr
            response = client.post(f"/connections/{cid}/refresh")

        def _has_key(obj: Any, key: str) -> bool:
            if isinstance(obj, dict):
                if key in obj:
                    return True
                return any(_has_key(v, key) for v in obj.values())
            if isinstance(obj, list):
                return any(_has_key(item, key) for item in obj)
            return False

        assert not _has_key(response.json(), "sample_values"), (
            "'sample_values' must NEVER appear in the /refresh response body "
            "(security invariant: sample data could contain PII)"
        )

    def test_refresh_returns_404_for_unknown_connection(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """Requesting refresh for a non-existent connection must return 404."""
        from uada.db.connection_manager import DatabaseConnectionManager

        mgr = DatabaseConnectionManager()
        app = create_app(orchestrator=orchestrator)

        with TestClient(app) as client:
            app.state.connection_manager = mgr
            response = client.post("/connections/does-not-exist/refresh")

        assert response.status_code == 404

    def test_refresh_schema_fingerprint_changes_after_schema_change(
        self, orchestrator: PipelineOrchestrator
    ) -> None:
        """Two different schemas must produce different fingerprints."""
        from uada.db.connection_manager import ConnectionConfig, DatabaseConnectionManager

        mgr = DatabaseConnectionManager()
        tfiles: list[str] = []
        connection_ids: list[str] = []

        db_fixtures = [
            (
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, revenue REAL)",
                "INSERT INTO orders VALUES (1, 100.0)",
            ),
            (
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)",
                "INSERT INTO users VALUES (1, 'Alice', 'alice@example.com')",
            ),
        ]

        for create_sql, insert_sql in db_fixtures:
            tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tf.close()
            tfiles.append(tf.name)
            conn = sqlite3.connect(tf.name)
            conn.execute(create_sql)
            conn.execute(insert_sql)
            conn.commit()
            conn.close()
            table_name = create_sql.split("(")[0].split()[-1]
            cid = mgr.register(ConnectionConfig(
                name=f"db-{table_name}", dialect="sqlite", database=tf.name
            ))
            connection_ids.append(cid)

        app = create_app(orchestrator=orchestrator)
        fingerprints: list[str] = []

        with TestClient(app) as client:
            app.state.connection_manager = mgr
            for cid in connection_ids:
                r = client.post(f"/connections/{cid}/refresh")
                assert r.status_code == 200, f"Refresh failed for {cid}: {r.text}"
                fingerprints.append(r.json()["schema_fingerprint"])

        for tf_name in tfiles:
            try:
                os.unlink(tf_name)
            except OSError:
                pass

        assert fingerprints[0] != fingerprints[1], (
            "Different schemas must produce different schema_fingerprints"
        )
