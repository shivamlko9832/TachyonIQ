"""
RetrievalBackend Interface
===========================
Abstract interface for the vector store backend.

Implementations:
  ChromaBackend  — In-process ChromaDB (POC)
  PgvectorBackend — PostgreSQL pgvector extension (production)

The retrieval engine (uada/retrieval/hybrid.py) uses this interface.
Switching backends is an environment variable change (UADA_VECTOR_BACKEND).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Document:
    """A document stored in the vector index."""

    doc_id: str
    content: str       # Text that was embedded
    metadata: dict     # Arbitrary metadata for filtering
    embedding: list[float] | None = None  # Stored or returned embedding


@dataclass
class RetrievalResult:
    """A single retrieved document with its relevance score."""

    document: Document
    score: float       # Similarity score (higher = more relevant)
    rank: int          # Position in the ranked result list


class RetrievalBackend(ABC):
    """
    Abstract interface for vector storage and retrieval.

    The backend stores embedded schema documents (table descriptions,
    column descriptions, metric definitions, Q&A examples) and retrieves
    the most relevant ones for a given query.
    """

    @abstractmethod
    def upsert(self, documents: list[Document]) -> None:
        """
        Insert or update documents in the index.
        If a document with the same doc_id exists, it is replaced.

        Args:
            documents: List of documents to upsert. Each document's
                       content will be embedded if embedding is None.
        """
        ...

    @abstractmethod
    def query(
        self,
        query_text: str,
        top_k: int = 10,
        filters: dict | None = None,
        collection: str | None = None,
    ) -> list[RetrievalResult]:
        """
        Retrieve the most relevant documents for the query.

        Args:
            query_text: The search query (user question or schema fragment).
            top_k: Number of results to return.
            filters: Metadata filters to apply before similarity search.
                     E.g. {"doc_type": "table"} to retrieve only table docs.
            collection: Override the default collection name.

        Returns:
            List of RetrievalResult, ordered by score descending.
        """
        ...

    @abstractmethod
    def delete(self, doc_ids: list[str]) -> None:
        """Delete documents by ID."""
        ...

    @abstractmethod
    def delete_collection(self, collection_name: str) -> None:
        """Delete an entire collection (used during schema re-indexing)."""
        ...

    @abstractmethod
    def count(self, collection: str | None = None) -> int:
        """Return the number of documents in the collection."""
        ...

    @abstractmethod
    def collection_exists(self, collection_name: str) -> bool:
        """Check if a collection exists."""
        ...


# ── Document type constants ────────────────────────────────────────────────────
# Used as metadata["doc_type"] to enable filtered retrieval.

class DocType:
    TABLE = "table"
    COLUMN = "column"
    METRIC = "metric"
    GLOSSARY = "glossary"
    EXAMPLE = "example"
    JOIN = "join"
