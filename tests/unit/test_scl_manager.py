"""
Tests for SCLManager (uada/scl/manager.py).

Built against the checked-in config/semantic_context.yaml example, so
these tests double as a regression check on that file's content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uada.retrieval.interface import DocType
from uada.scl.loader import SCLLoader
from uada.scl.manager import SCLManager

pytestmark = pytest.mark.unit

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "semantic_context.yaml"


@pytest.fixture(scope="module")
def manager() -> SCLManager:
    scl = SCLLoader.load(CONFIG_PATH)
    return SCLManager(scl)


class TestAllowedTables:
    def test_excluded_tables_are_removed(self, manager: SCLManager) -> None:
        allowed = manager.get_allowed_tables()
        assert isinstance(allowed, frozenset)
        assert allowed == {"orders", "customers", "products"}
        assert "audit_log" not in allowed
        assert "user_sessions" not in allowed
        assert "api_keys" not in allowed


class TestGetTable:
    def test_known_table_found(self, manager: SCLManager) -> None:
        table = manager.get_table("orders")
        assert table is not None
        assert table.name == "orders"

    def test_unknown_table_returns_none(self, manager: SCLManager) -> None:
        assert manager.get_table("does_not_exist") is None


class TestResolveMetric:
    def test_resolve_by_exact_name(self, manager: SCLManager) -> None:
        metric = manager.resolve_metric("revenue")
        assert metric is not None
        assert metric.formula == "SUM(orders.revenue)"

    def test_resolve_by_alias_case_insensitive(self, manager: SCLManager) -> None:
        metric = manager.resolve_metric("EARNINGS")
        assert metric is not None
        assert metric.name == "revenue"

    def test_resolve_name_case_insensitive(self, manager: SCLManager) -> None:
        metric = manager.resolve_metric("Revenue")
        assert metric is not None
        assert metric.name == "revenue"

    def test_unknown_metric_returns_none(self, manager: SCLManager) -> None:
        assert manager.resolve_metric("nonexistent_metric") is None


class TestResolveGlossaryTerm:
    def test_resolve_by_exact_term(self, manager: SCLManager) -> None:
        term = manager.resolve_glossary_term("enterprise customer")
        assert term is not None
        assert term.sql_filter == "customers.segment = 'Enterprise'"

    def test_resolve_by_alias_case_insensitive(self, manager: SCLManager) -> None:
        term = manager.resolve_glossary_term("ENTERPRISE CLIENTS")
        assert term is not None
        assert term.term == "enterprise customer"

    def test_unknown_term_returns_none(self, manager: SCLManager) -> None:
        assert manager.resolve_glossary_term("nonexistent term") is None


class TestGetJoinPath:
    def test_forward_direction(self, manager: SCLManager) -> None:
        join = manager.get_join_path("orders", "customers")
        assert join is not None
        assert join.on == "orders.customer_id = customers.customer_id"

    def test_reverse_direction_also_resolves(self, manager: SCLManager) -> None:
        join = manager.get_join_path("customers", "orders")
        assert join is not None
        assert join.from_table == "orders"
        assert join.to_table == "customers"

    def test_no_join_returns_none(self, manager: SCLManager) -> None:
        assert manager.get_join_path("orders", "nonexistent_table") is None


class TestToIndexableDocuments:
    def test_document_count_matches_scl_content(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        # config/semantic_context.yaml: 3 included tables, 4 metrics,
        # 3 glossary terms, 4 examples.
        assert len(docs) == 3 + 4 + 3 + 4

    def test_table_document_format(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        orders_doc = next(d for d in docs if d["doc_id"] == "table:orders")
        assert orders_doc["content"].startswith(
            "orders: Customer orders. One row per order placed."
        )
        assert "order_id" in orders_doc["content"]
        assert orders_doc["metadata"] == {
            "doc_type": DocType.TABLE,
            "table_name": "orders",
            "excluded": False,
        }

    def test_excluded_column_omitted_from_table_content(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        customers_doc = next(d for d in docs if d["doc_id"] == "table:customers")
        assert "internal_credit_score" not in customers_doc["content"]

    def test_excluded_table_produces_no_document(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        doc_ids = {d["doc_id"] for d in docs}
        assert "table:audit_log" not in doc_ids
        assert "table:user_sessions" not in doc_ids
        assert "table:api_keys" not in doc_ids

    def test_metric_document_format(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        revenue_doc = next(d for d in docs if d["doc_id"] == "metric:revenue")
        assert revenue_doc["content"] == (
            "revenue: Total net revenue across completed orders. "
            "Formula: SUM(orders.revenue)"
        )
        assert revenue_doc["metadata"]["doc_type"] == DocType.METRIC

    def test_glossary_document_format(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        glossary_doc = next(d for d in docs if d["doc_id"] == "glossary:enterprise customer")
        assert glossary_doc["content"] == (
            "'enterprise customer': A customer in the Enterprise segment"
        )
        assert glossary_doc["metadata"]["table_name"] == "customers"

    def test_example_document_format(self, manager: SCLManager) -> None:
        docs = manager.to_indexable_documents()
        example_docs = [d for d in docs if d["doc_id"].startswith("example:")]
        assert len(example_docs) == 4
        first = next(d for d in example_docs if d["doc_id"] == "example:0")
        assert first["content"] == "Question: What was total revenue last quarter?"
        assert first["metadata"]["doc_type"] == DocType.EXAMPLE
