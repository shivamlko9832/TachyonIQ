"""
HybridRetriever
===============
Combines the vector backend (semantic similarity) and BM25 (lexical
matching) via Reciprocal Rank Fusion, so schema linking finds both
"what does this mean" matches and exact-name matches.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from uada.retrieval.interface import Document, RetrievalResult

if TYPE_CHECKING:
    from uada.retrieval.bm25 import BM25Index
    from uada.retrieval.interface import RetrievalBackend

logger = logging.getLogger(__name__)

_RRF_K = 60


def _matches_filters(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    return all(metadata.get(key) == value for key, value in filters.items())


class HybridRetriever:
    """Reciprocal-Rank-Fusion merge of a vector backend and a BM25 index."""

    def __init__(self, vector_backend: RetrievalBackend, bm25_index: BM25Index) -> None:
        self._vector_backend = vector_backend
        self._bm25_index = bm25_index

    def build_index(self, documents: list[dict[str, Any]]) -> None:
        """
        Index a document corpus in both backends.

        Args:
            documents: Dicts shaped like SCLManager.to_indexable_documents()
                output: {"doc_id": str, "content": str, "metadata": dict}.
        """
        vector_documents = [
            Document(doc_id=doc["doc_id"], content=doc["content"], metadata=doc.get("metadata", {}))
            for doc in documents
        ]
        self._vector_backend.upsert(vector_documents)
        self._bm25_index.build(documents)
        logger.info("Hybrid index built over %d document(s).", len(documents))

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        """
        Retrieve the top_k documents for `query`, merging vector and BM25
        rankings via Reciprocal Rank Fusion (score = sum(1 / (rank + 60))
        across the lists a document appears in).

        Args:
            query: The search query (user question or schema fragment).
            top_k: Number of results to return after fusion.
            filters: Metadata equality filters. Passed through to the
                vector backend's native filtering; applied by this method
                as a post-filter on BM25 results, since BM25Index has no
                filtering of its own.
        """
        candidate_k = top_k * 2
        vector_results = self._vector_backend.query(query, top_k=candidate_k, filters=filters)
        bm25_results = self._bm25_index.query(query, top_k=candidate_k)
        if filters:
            bm25_results = [
                r for r in bm25_results if _matches_filters(r.document.metadata, filters)
            ]
        return self._reciprocal_rank_fusion([vector_results, bm25_results], top_k)

    def _reciprocal_rank_fusion(
        self,
        ranked_lists: list[list[RetrievalResult]],
        top_k: int,
    ) -> list[RetrievalResult]:
        scores: dict[str, float] = {}
        documents: dict[str, Document] = {}

        for ranked_list in ranked_lists:
            for result in ranked_list:
                doc_id = result.document.doc_id
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (result.rank + _RRF_K)
                documents.setdefault(doc_id, result.document)

        ordered_ids = sorted(scores, key=lambda doc_id: scores[doc_id], reverse=True)[:top_k]
        return [
            RetrievalResult(document=documents[doc_id], score=scores[doc_id], rank=rank)
            for rank, doc_id in enumerate(ordered_ids)
        ]
