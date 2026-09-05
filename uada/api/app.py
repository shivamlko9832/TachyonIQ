"""
FastAPI Application
======================
Wires the full pipeline (Phases 1-11) into HTTP endpoints.

Real deployments call `create_app()` with no arguments: the lifespan hook
bootstraps every pipeline dependency from `Settings` when the app starts
serving. Tests instead build their own `PipelineOrchestrator` (with
TestModel-overridden LLM agents) and pass it directly, skipping the real
bootstrap (and its embedding-model load, live database connection, etc.)
entirely.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from uada.api.middleware.auth import AuthMiddleware
from uada.api.middleware.rate_limit import RateLimitMiddleware
from uada.api.middleware.request_id import RequestIdMiddleware
from uada.api.routes import analyses, connections, health, query, session, ui

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from uada.config import Settings
    from uada.pipeline.orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)


def _bootstrap_orchestrator(settings: Settings) -> PipelineOrchestrator:
    """Construct every pipeline dependency from `settings` for a real deployment."""
    from uada.db.adapter import SQLAlchemyAdapter
    from uada.db.connection_manager import DatabaseConnectionManager
    from uada.pipeline.followup_engine import FollowUpEngine
    from uada.observability.tracer import configure_tracing
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
    from uada.scl.loader import SCLLoader
    from uada.scl.manager import SCLManager

    # Only for real deployments -- tests build their own orchestrator and
    # never call this function, so tracing setup never runs against the
    # TestModel-overridden agents a test wires up.
    configure_tracing(settings)

    scl = SCLLoader.load(settings.scl_path)
    scl_manager = SCLManager(scl)

    db_adapter = SQLAlchemyAdapter(settings.db_url.get_secret_value(), settings)

    embedder = Embedder(settings.embedding_model, settings.embedding_batch_size)
    vector_backend = ChromaBackend(
        path=str(settings.chroma_path),
        collection_name=settings.chroma_collection_name,
        embedder=embedder,
    )
    retriever = HybridRetriever(vector_backend=vector_backend, bm25_index=BM25Index())
    retriever.build_index(scl_manager.to_indexable_documents())

    validator = SQLValidator(
        allowed_tables=set(scl_manager.get_allowed_tables()),
        max_subquery_depth=settings.sql_max_subquery_depth,
        inject_limit=settings.sql_inject_limit,
        default_limit=settings.db_max_rows,
    )

    followup_engine = FollowUpEngine()

    from uada.observability.audit import AuditLogger
    audit_logger = AuditLogger(settings.audit_log_path)

    return PipelineOrchestrator(
        db_adapter=db_adapter,
        scl_manager=scl_manager,
        schema_linker=SchemaLinker(retriever, scl_manager, settings),
        intent_extractor=IntentExtractor(settings),
        query_planner=QueryPlanner(scl_manager),
        sql_generator=SQLGenerator(settings),
        result_analyser=ResultAnalyser(),
        viz_generator=VisualisationGenerator(settings),
        conversation_store=ConversationStore(settings, database_id=scl.database.name),
        validator=validator,
        settings=settings,
        followup_engine=followup_engine,
        audit_logger=audit_logger,
    )


def create_app(
    orchestrator: PipelineOrchestrator | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """
    Build the UADA FastAPI application.

    Args:
        orchestrator: A pre-built PipelineOrchestrator. Pass this (tests
            do) to skip the real bootstrap entirely. Production passes
            None so the lifespan hook builds one from `settings` at
            startup.
        settings: Settings to bootstrap from and to configure auth with.
            Defaults to the module-level settings singleton -- imported
            lazily here, inside the function body, so merely importing
            this module never requires UADA_DB_URL to be set.
    """
    if settings is None:
        from uada.config import settings as default_settings

        settings = default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if orchestrator is not None:
            app.state.orchestrator = orchestrator
        else:
            logger.info("Bootstrapping pipeline orchestrator...")
            app.state.orchestrator = _bootstrap_orchestrator(settings)
            logger.info("Pipeline orchestrator ready.")
        from uada.db.connection_manager import DatabaseConnectionManager
        app.state.connection_manager = DatabaseConnectionManager()
        yield

    app = FastAPI(title="UADA", version="0.1.0", lifespan=lifespan)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(RequestIdMiddleware)

    if orchestrator is None:
        # Real deployment only: instrumenting every test-created app would
        # repeatedly instrument the same underlying ASGI machinery across
        # the test suite for no benefit, since tests never export traces.
        from uada.observability.tracer import instrument_app

        instrument_app(app)

    app.include_router(analyses.router)
    app.include_router(connections.router)
    app.include_router(health.router)
    app.include_router(query.router)
    app.include_router(session.router)
    app.include_router(ui.router)
    return app


# The real deployment entry point, e.g. `uvicorn uada.api.app:app`. This
# only builds the FastAPI app object and registers the lifespan hook --
# it does not bootstrap the orchestrator; that happens once when a real
# server actually starts serving (or a TestClient enters as a context
# manager), never merely from importing this module.
app = create_app()
