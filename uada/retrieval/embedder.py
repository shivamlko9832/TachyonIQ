"""
Embedder
========
Wraps a SentenceTransformer model for embedding schema documents and
queries. The model is loaded once at construction and cached for the life
of the instance -- callers should build one Embedder per process, not one
per call.
"""

from __future__ import annotations

import logging

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class Embedder:
    """Text -> dense vector embeddings, via a cached SentenceTransformer model."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", batch_size: int = 64) -> None:
        """
        Load `model_name` (downloading it on first use if not cached locally).

        Args:
            model_name: A Sentence Transformers model id, e.g. 'BAAI/bge-small-en-v1.5'.
            batch_size: Number of texts embedded per forward pass in `embed()`.
        """
        self._model_name = model_name
        self._batch_size = batch_size
        logger.info("Loading embedding model '%s'...", model_name)
        self._model = SentenceTransformer(model_name)
        logger.info("Embedding model '%s' loaded.", model_name)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        """The embedding vector size produced by this model."""
        dim = self._model.get_embedding_dimension()
        if dim is None:
            raise RuntimeError(
                f"Model '{self._model_name}' did not report an embedding dimension."
            )
        return dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of texts.

        Args:
            texts: Texts to embed. An empty list returns an empty list
                without invoking the model.

        Returns:
            One embedding vector per input text, in the same order.
        """
        if not texts:
            return []
        # Positional call: the first parameter's name has changed across
        # sentence-transformers versions ('sentences' -> 'inputs').
        embeddings = self._model.encode(
            texts,
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [vector.tolist() for vector in embeddings]
