"""
Tests for the retrieval engine (uada/retrieval/embedder.py, bm25.py,
chroma_backend.py, hybrid.py).

Uses a real SentenceTransformer model (BAAI/bge-small-en-v1.5, CPU-only)
and a real ChromaDB PersistentClient rooted at tmp_path -- no external
services required, but the embedding model (~90MB) is downloaded on first
run if not already cached locally in ~/.cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from uada.retrieval.bm25 import BM25Index
from uada.retrieval.chroma_backend import ChromaBackend
from uada.retrieval.embedder import Embedder
from uada.retrieval.hybrid import HybridRetriever
from uada.retrieval.interface import Document, RetrievalResult

pytestmark = pytest.mark.unit

_SYNTHETIC_DOCS: list[dict[str, Any]] = [
    {
        "doc_id": "table:orders",
        "content": "orders: Customer orders. Columns: order_id, customer_id, revenue, order_date",
        "metadata": {"doc_type": "table", "table_name": "orders", "excluded": False},
    },
    {
        "doc_id": "table:customers",
        "content": "customers: Customer master data. Columns: customer_id, name, segment",
        "metadata": {"doc_type": "table", "table_name": "customers", "excluded": False},
    },
    {
        "doc_id": "metric:revenue",
        "content": (
            "revenue: Total net revenue across completed orders. "
            "Formula: SUM(orders.revenue)"
        ),
        "metadata": {"doc_type": "metric", "table_name": None, "excluded": False},
    },
    {
        "doc_id": "glossary:enterprise customer",
        "content": "'enterprise customer': A customer in the Enterprise segment",
        "metadata": {"doc_type": "glossary", "table_name": "customers", "excluded": False},
    },
    {
        "doc_id": "example:0",
        "content": "Question: What was total revenue last quarter?",
        "metadata": {"doc_type": "example", "table_name": None, "excluded": False},
    },
]


def _to_documents(docs: list[dict[str, Any]]) -> list[Document]:
    return [
        Document(doc_id=d["doc_id"], content=d["content"], metadata=d["metadata"]) for d in docs
    ]


def _result(doc_id: str, rank: int) -> RetrievalResult:
    document = Document(doc_id=doc_id, content=doc_id, metadata={})
    return RetrievalResult(document=document, score=0.0, rank=rank)


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    return Embedder()


class TestEmbedder:
    def test_embed_returns_one_vector_per_text(self, embedder: Embedder) -> None:
        vectors = embedder.embed(["hello world", "goodbye world"])
        assert len(vectors) == 2
        assert len(vectors[0]) == embedder.dimension
        assert len(vectors[1]) == embedder.dimension

    def test_embed_empty_list_returns_empty(self, embedder: Embedder) -> None:
        assert embedder.embed([]) == []


class TestBM25Index:
    def test_exact_term_match_ranks_first(self) -> None:
        index = BM25Index()
        index.build(_SYNTHETIC_DOCS)
        results = index.query("revenue formula SUM", top_k=3)
        assert results
        assert results[0].document.doc_id == "metric:revenue"

    def test_empty_index_returns_empty(self) -> None:
        index = BM25Index()
        assert index.query("anything", top_k=3) == []

    def test_results_ranked_descending_by_score(self) -> None:
        index = BM25Index()
        index.build(_SYNTHETIC_DOCS)
        results = index.query("customer orders revenue", top_k=5)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


class TestReciprocalRankFusion:
    def test_merge_favors_docs_ranked_high_in_multiple_lists(self) -> None:
        retriever = HybridRetriever(vector_backend=None, bm25_index=None)  # type: ignore[arg-type]
        list_a = [_result("x", 0), _result("y", 1)]
        list_b = [_result("x", 0), _result("z", 1)]

        merged = retriever._reciprocal_rank_fusion([list_a, list_b], top_k=3)

        assert merged[0].document.doc_id == "x"
        assert merged[0].score == pytest.approx(1 / 60 + 1 / 60)
        merged_ids = {r.document.doc_id for r in merged}
        assert merged_ids == {"x", "y", "z"}

    def test_top_k_limits_result_count(self) -> None:
        retriever = HybridRetriever(vector_backend=None, bm25_index=None)  # type: ignore[arg-type]
        list_a = [_result(str(i), i) for i in range(10)]
        merged = retriever._reciprocal_rank_fusion([list_a], top_k=3)
        assert len(merged) == 3


class TestChromaBackend:
    @pytest.fixture
    def backend(self, tmp_path: Path, embedder: Embedder) -> ChromaBackend:
        return ChromaBackend(
            path=str(tmp_path / "chroma"),
            collection_name="test_collection",
            embedder=embedder,
        )

    def test_upsert_and_count(self, backend: ChromaBackend) -> None:
        backend.upsert(_to_documents(_SYNTHETIC_DOCS))
        assert backend.count() == len(_SYNTHETIC_DOCS)

    def test_query_returns_relevant_document(self, backend: ChromaBackend) -> None:
        backend.upsert(_to_documents(_SYNTHETIC_DOCS))
        results = backend.query("How much money did we make from orders?", top_k=3)
        assert results
        # "example:0" ("What was total revenue last quarter?") is also a
        # legitimately close semantic match to a revenue question.
        assert results[0].document.doc_id in {"metric:revenue", "table:orders", "example:0"}
        assert results[0].score >= results[-1].score

    def test_delete_removes_document(self, backend: ChromaBackend) -> None:
        backend.upsert(_to_documents(_SYNTHETIC_DOCS))
        backend.delete(["table:orders"])
        assert backend.count() == len(_SYNTHETIC_DOCS) - 1

    def test_collection_exists(self, backend: ChromaBackend) -> None:
        assert backend.collection_exists("test_collection") is True
        assert backend.collection_exists("nonexistent_collection_xyz") is False

    def test_delete_collection(self, backend: ChromaBackend) -> None:
        backend.delete_collection("test_collection")
        assert backend.collection_exists("test_collection") is False

    def test_filters_restrict_results_to_doc_type(self, backend: ChromaBackend) -> None:
        backend.upsert(_to_documents(_SYNTHETIC_DOCS))
        results = backend.query("orders", top_k=5, filters={"doc_type": "table"})
        assert results
        assert all(r.document.metadata.get("doc_type") == "table" for r in results)

    def test_empty_collection_query_returns_empty(self, backend: ChromaBackend) -> None:
        assert backend.query("anything", top_k=3) == []


class TestHybridRetriever:
    @pytest.fixture
    def retriever(self, tmp_path: Path, embedder: Embedder) -> HybridRetriever:
        backend = ChromaBackend(
            path=str(tmp_path / "chroma_hybrid"),
            collection_name="hybrid_test",
            embedder=embedder,
        )
        hybrid = HybridRetriever(vector_backend=backend, bm25_index=BM25Index())
        hybrid.build_index(_SYNTHETIC_DOCS)
        return hybrid

    def test_exact_name_match_surfaces_expected_doc(self, retriever: HybridRetriever) -> None:
        # BM25 should surface this via the literal token overlap even if
        # the vector arm ranks it lower.
        results = retriever.retrieve("revenue formula SUM", top_k=3)
        doc_ids = [r.document.doc_id for r in results]
        assert "metric:revenue" in doc_ids

    def test_semantic_query_surfaces_expected_doc(self, retriever: HybridRetriever) -> None:
        # No lexical overlap with the glossary doc's content -- only the
        # vector arm can surface this.
        results = retriever.retrieve("Which clients are big corporate accounts?", top_k=3)
        doc_ids = [r.document.doc_id for r in results]
        assert "glossary:enterprise customer" in doc_ids

    def test_results_bounded_by_top_k(self, retriever: HybridRetriever) -> None:
        results = retriever.retrieve("orders customers revenue", top_k=2)
        assert len(results) <= 2
