"""
Tests for SchemaLinker (uada/pipeline/schema_linker.py).

Builds a real hybrid index (Embedder + ChromaBackend + BM25Index) from
config/semantic_context.yaml and exercises link() end-to-end -- no mocks
for retrieval, since the whole point of this stage is what actually gets
retrieved and passed through the security filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uada.config import Settings
from uada.pipeline.schema_linker import SchemaLinker
from uada.retrieval.bm25 import BM25Index
from uada.retrieval.chroma_backend import ChromaBackend
from uada.retrieval.embedder import Embedder
from uada.retrieval.hybrid import HybridRetriever
from uada.scl.loader import SCLLoader
from uada.scl.manager import SCLManager

pytestmark = pytest.mark.unit

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "semantic_context.yaml"


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    return Embedder()


@pytest.fixture(scope="module")
def scl_manager() -> SCLManager:
    scl = SCLLoader.load(CONFIG_PATH)
    return SCLManager(scl)


@pytest.fixture
def linker(tmp_path: Path, embedder: Embedder, scl_manager: SCLManager) -> SchemaLinker:
    backend = ChromaBackend(
        path=str(tmp_path / "chroma"),
        collection_name="schema_linker_test",
        embedder=embedder,
    )
    retriever = HybridRetriever(vector_backend=backend, bm25_index=BM25Index())
    retriever.build_index(scl_manager.to_indexable_documents())
    settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
    return SchemaLinker(retriever=retriever, scl_manager=scl_manager, settings=settings)


class TestLink:
    def test_revenue_question_surfaces_orders_table(self, linker: SchemaLinker) -> None:
        context = linker.link("What was revenue last quarter?")
        table_names = {t.table_name for t in context.tables}
        assert "orders" in table_names

    def test_revenue_column_present(self, linker: SchemaLinker) -> None:
        context = linker.link("What was revenue last quarter?")
        orders = next(t for t in context.tables if t.table_name == "orders")
        column_names = {c.column_name for c in orders.columns}
        assert "revenue" in column_names

    def test_excluded_tables_never_appear(self, linker: SchemaLinker) -> None:
        context = linker.link("What was revenue last quarter?")
        table_names = {t.table_name for t in context.tables}
        assert "audit_log" not in table_names
        assert "user_sessions" not in table_names
        assert "api_keys" not in table_names

    def test_security_excluded_column_never_appears(self, linker: SchemaLinker) -> None:
        context = linker.link("Tell me about our customers")
        customers = next((t for t in context.tables if t.table_name == "customers"), None)
        assert customers is not None
        column_names = {c.column_name for c in customers.columns}
        assert "internal_credit_score" not in column_names

    def test_dialect_and_retrieval_metadata_populated(self, linker: SchemaLinker) -> None:
        context = linker.link("What was revenue last quarter?")
        assert context.dialect == "postgresql"
        assert context.retrieval_query == "What was revenue last quarter?"
        assert context.total_retrieved > 0

    def test_session_context_appended_to_query(self, linker: SchemaLinker) -> None:
        context = linker.link(
            "Only show enterprise customers.",
            session_context="Previous: revenue by month",
        )
        assert "Previous: revenue by month" in context.retrieval_query

    def test_metric_surfaced_for_revenue_question(self, linker: SchemaLinker) -> None:
        context = linker.link("What was total revenue?")
        metric_names = {m.metric_name for m in context.metrics}
        assert "revenue" in metric_names

    def test_glossary_term_surfaced_for_enterprise_question(self, linker: SchemaLinker) -> None:
        context = linker.link("Show me enterprise customers")
        terms = {g.term for g in context.glossary_terms}
        assert "enterprise customer" in terms


class TestBuildTableContextSecurity:
    def test_disallowed_table_returns_none(self, linker: SchemaLinker) -> None:
        assert linker._build_table_context("audit_log", 1.0) is None

    def test_unknown_table_returns_none(self, linker: SchemaLinker) -> None:
        assert linker._build_table_context("does_not_exist", 1.0) is None

    def test_allowed_table_returns_context(self, linker: SchemaLinker) -> None:
        context = linker._build_table_context("orders", 1.0)
        assert context is not None
        assert context.table_name == "orders"
        assert context.retrieval_score == 1.0

    def test_excluded_column_omitted(self, linker: SchemaLinker) -> None:
        context = linker._build_table_context("customers", 1.0)
        assert context is not None
        column_names = {c.column_name for c in context.columns}
        assert "internal_credit_score" not in column_names


class TestCollectJoins:
    def test_join_found_between_retrieved_tables(self, linker: SchemaLinker) -> None:
        orders_ctx = linker._build_table_context("orders", 1.0)
        customers_ctx = linker._build_table_context("customers", 1.0)
        assert orders_ctx is not None
        assert customers_ctx is not None

        joins = linker._collect_joins([orders_ctx, customers_ctx])
        assert len(joins) == 1
        assert {joins[0].from_table, joins[0].to_table} == {"orders", "customers"}

    def test_no_join_for_single_table(self, linker: SchemaLinker) -> None:
        orders_ctx = linker._build_table_context("orders", 1.0)
        assert orders_ctx is not None
        assert linker._collect_joins([orders_ctx]) == []


class TestDefaultTimeColumn:
    def test_picks_up_orders_default_time_column(self, linker: SchemaLinker) -> None:
        orders_ctx = linker._build_table_context("orders", 1.0)
        assert orders_ctx is not None
        assert linker._default_time_column([orders_ctx]) == "order_date"

    def test_returns_none_when_no_table_has_one(self, linker: SchemaLinker) -> None:
        # customers.last_order_date is temporal but not marked as the
        # table's default_time_column in the example SCL.
        customers_ctx = linker._build_table_context("customers", 1.0)
        assert customers_ctx is not None
        assert linker._default_time_column([customers_ctx]) is None


class TestTableRetrievalNotCrowdedOutByExamples:
    """
    Regression test for a real bug found while onboarding a demo database:
    link() used to retrieve one shared top_k across every doc_type, so a
    schema with several worked examples whose "Question: ..." text closely
    matches the real question could out-rank -- and entirely crowd out --
    the actual table doc. Confirmed empirically: with 8 examples in a
    3-table demo schema, a plain revenue question didn't surface the
    'orders' table even in the top 10 combined results. Each doc_type now
    gets its own retrieval budget.
    """

    def test_table_survives_many_lexically_similar_examples(
        self, tmp_path: Path, embedder: Embedder
    ) -> None:
        from uada.scl.schema import (
            ColumnDefinition,
            DatabaseMeta,
            ExampleQuery,
            MetricDefinition,
            SecurityPolicy,
            SemanticContextLayer,
            TableDefinition,
        )

        question = "What was total revenue last quarter?"
        scl = SemanticContextLayer(
            version="1.0",
            database=DatabaseMeta(name="crowd_test", dialect="sqlite"),
            tables=[
                TableDefinition(
                    name="orders",
                    description="Orders.",
                    columns=[ColumnDefinition(name="revenue", type="float")],
                )
            ],
            metrics=[
                MetricDefinition(
                    name="revenue", description="Total revenue.", formula="SUM(orders.revenue)"
                )
            ],
            examples=[
                ExampleQuery(question=question, sql="SELECT SUM(orders.revenue) FROM orders")
                for _ in range(10)
            ],
            security=SecurityPolicy(),
        )
        manager = SCLManager(scl)
        backend = ChromaBackend(
            path=str(tmp_path / "chroma_crowd"), collection_name="crowd_test", embedder=embedder
        )
        retriever = HybridRetriever(vector_backend=backend, bm25_index=BM25Index())
        retriever.build_index(manager.to_indexable_documents())
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        linker = SchemaLinker(retriever, manager, settings)

        context = linker.link(question)

        assert "orders" in {t.table_name for t in context.tables}
