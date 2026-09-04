#!/usr/bin/env python
"""
Build the retrieval index (BM25 + ChromaDB) from the SCL YAML file.

Run this after authoring/editing the SCL (see scripts/onboard_db.py for
generating a candidate one). Re-run it whenever the SCL changes -- table
descriptions, metrics, glossary terms, or examples.

Usage:
    python scripts/build_index.py
    python scripts/build_index.py --scl-path config/my_scl.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scl-path",
        default=None,
        help="Path to the SCL YAML file (default: settings.scl_path).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    from uada.config import settings
    from uada.retrieval.bm25 import BM25Index
    from uada.retrieval.chroma_backend import ChromaBackend
    from uada.retrieval.embedder import Embedder
    from uada.retrieval.hybrid import HybridRetriever
    from uada.scl.loader import SCLLoader
    from uada.scl.manager import SCLManager

    scl_path = Path(args.scl_path) if args.scl_path else settings.scl_path

    try:
        scl = SCLLoader.load(scl_path)
    except Exception as exc:
        logger.error("Failed to load SCL from %s: %s", scl_path, exc)
        return 1

    scl_manager = SCLManager(scl)
    documents = scl_manager.to_indexable_documents()

    logger.info("Loading embedding model '%s'...", settings.embedding_model)
    embedder = Embedder(settings.embedding_model, settings.embedding_batch_size)
    vector_backend = ChromaBackend(
        path=str(settings.chroma_path),
        collection_name=settings.chroma_collection_name,
        embedder=embedder,
    )
    retriever = HybridRetriever(vector_backend=vector_backend, bm25_index=BM25Index())
    retriever.build_index(documents)

    logger.info("Indexed %d document(s). Ready.", len(documents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
