"""
ChromaBackend
=============
In-process ChromaDB implementation of RetrievalBackend, used for the POC
vector store. Embeddings always come from the injected Embedder -- the
collection is created with `embedding_function=None` so Chroma never
falls back to downloading its own default embedding model.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

import chromadb
from chromadb.errors import NotFoundError

from uada.retrieval.interface import Document, RetrievalBackend, RetrievalResult

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

    from uada.retrieval.embedder import Embedder

logger = logging.getLogger(__name__)


def _build_where(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Translate a flat equality-filter dict into a Chroma `where` clause.

    Chroma rejects both `where={}` and a multi-key dict without an
    explicit boolean operator ("Expected where to have exactly one
    operator") -- collapse the empty case to None and wrap 2+ keys in
    `$and`.
    """
    if not filters:
        return None
    if len(filters) == 1:
        return dict(filters)
    return {"$and": [{key: value} for key, value in filters.items()]}


class ChromaBackend(RetrievalBackend):
    """RetrievalBackend implementation backed by a persistent ChromaDB store."""

    def __init__(self, path: str, collection_name: str, embedder: Embedder) -> None:
        self._embedder = embedder
        self._default_collection_name = collection_name
        self._client = chromadb.PersistentClient(path=path)
        self._default_collection = self._get_or_create(collection_name)

    def _get_or_create(self, name: str) -> Collection:
        return self._client.get_or_create_collection(name=name, embedding_function=None)

    def _collection(self, name: str | None) -> Collection:
        if name is None or name == self._default_collection_name:
            return self._default_collection
        return self._get_or_create(name)

    def upsert(self, documents: list[Document]) -> None:
        """Insert or update documents, embedding any without a precomputed vector."""
        if not documents:
            return

        missing_indices = [i for i, doc in enumerate(documents) if doc.embedding is None]
        computed = (
            self._embedder.embed([documents[i].content for i in missing_indices])
            if missing_indices
            else []
        )
        embeddings: list[list[float]] = []
        for i, doc in enumerate(documents):
            if doc.embedding is not None:
                embeddings.append(doc.embedding)
            else:
                embeddings.append(computed[missing_indices.index(i)])

        self._default_collection.upsert(
            ids=[doc.doc_id for doc in documents],
            embeddings=embeddings,
            documents=[doc.content for doc in documents],
            metadatas=[dict(doc.metadata) for doc in documents],
        )
        logger.info(
            "Upserted %d document(s) into '%s'.",
            len(documents),
            self._default_collection_name,
        )

    def query(
        self,
        query_text: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
        collection: str | None = None,
    ) -> list[RetrievalResult]:
        """Embed `query_text` and return its top_k nearest documents, descending by score."""
        target = self._collection(collection)
        if target.count() == 0:
            return []

        query_embedding = self._embedder.embed([query_text])[0]
        raw = target.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=_build_where(filters),
        )

        ids = raw["ids"][0]
        contents = raw["documents"][0] if raw["documents"] else [""] * len(ids)
        metadatas = raw["metadatas"][0] if raw["metadatas"] else [{}] * len(ids)
        distances = raw["distances"][0] if raw["distances"] else [0.0] * len(ids)

        results: list[RetrievalResult] = []
        for rank, (doc_id, content, metadata, distance) in enumerate(
            zip(ids, contents, metadatas, distances, strict=True)
        ):
            document = Document(doc_id=doc_id, content=content, metadata=dict(metadata or {}))
            results.append(
                RetrievalResult(document=document, score=1.0 / (1.0 + distance), rank=rank)
            )
        return results

    def delete(self, doc_ids: list[str]) -> None:
        """Delete documents by ID from the default collection."""
        if doc_ids:
            self._default_collection.delete(ids=doc_ids)

    def delete_collection(self, collection_name: str) -> None:
        """Delete an entire collection. No-op if it doesn't exist."""
        with contextlib.suppress(NotFoundError):
            self._client.delete_collection(name=collection_name)

    def count(self, collection: str | None = None) -> int:
        """Number of documents in the given (or default) collection."""
        return self._collection(collection).count()

    def collection_exists(self, collection_name: str) -> bool:
        """Whether `collection_name` exists, without creating it."""
        try:
            self._client.get_collection(name=collection_name, embedding_function=None)
        except NotFoundError:
            return False
        return True
