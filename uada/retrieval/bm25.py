"""
BM25Index
=========
Pure-Python lexical retrieval over the SCL document corpus, using
rank_bm25's Okapi BM25 implementation. Complements the vector backend:
exact-term matches (table names, column names, metric names) often rank
higher under BM25 than under embedding similarity.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from rank_bm25 import BM25Okapi

from uada.retrieval.interface import Document, RetrievalResult

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


class BM25Index:
    """In-memory BM25 index over a document corpus."""

    def __init__(self) -> None:
        self._bm25: BM25Okapi | None = None
        self._documents: list[Document] = []

    def build(self, documents: list[dict[str, Any]]) -> None:
        """
        (Re)build the index from a document corpus.

        Args:
            documents: Dicts shaped like SCLManager.to_indexable_documents()
                output: {"doc_id": str, "content": str, "metadata": dict}.
        """
        self._documents = [
            Document(
                doc_id=doc["doc_id"],
                content=doc["content"],
                metadata=doc.get("metadata", {}),
            )
            for doc in documents
        ]
        tokenized_corpus = [_tokenize(doc.content) for doc in self._documents]
        self._bm25 = BM25Okapi(tokenized_corpus) if tokenized_corpus else None
        logger.info("BM25 index built over %d document(s).", len(self._documents))

    def query(self, text: str, top_k: int = 10) -> list[RetrievalResult]:
        """Return the top_k documents ranked by BM25 score, descending."""
        if self._bm25 is None or not self._documents:
            return []

        scores = self._bm25.get_scores(_tokenize(text))
        ranked_indices = sorted(
            range(len(self._documents)), key=lambda i: scores[i], reverse=True
        )[:top_k]

        return [
            RetrievalResult(
                document=self._documents[index],
                score=float(scores[index]),
                rank=rank,
            )
            for rank, index in enumerate(ranked_indices)
        ]
