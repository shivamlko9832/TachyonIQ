#!/usr/bin/env python
"""
Onboard a new database: reflect its schema and write a candidate SCL YAML.

The written file is a starting point -- table/column descriptions,
metric definitions, glossary terms, and example Q&A pairs are left for a
human to fill in.

Usage:
    python scripts/onboard_db.py --db-url postgresql://readonly:pass@host/db
    python scripts/onboard_db.py --db-url sqlite:///./data/mydb.sqlite --output config/my_scl.yaml
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
        "--db-url",
        required=True,
        help=(
            "SQLAlchemy connection string for the database to onboard. "
            "Must be a read-only account."
        ),
    )
    parser.add_argument(
        "--output",
        default="config/semantic_context.yaml",
        help="Path to write the candidate SCL YAML (default: config/semantic_context.yaml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    output_path = Path(args.output)

    # Imported here, not at module scope: Settings() requires a resolvable
    # db_url, and this script is the one place that legitimately gets it
    # from a CLI flag rather than the environment/.env file.
    from uada.config import Settings
    from uada.db.adapter import SQLAlchemyAdapter

    settings = Settings(db_url=args.db_url)  # type: ignore[call-arg]

    try:
        adapter = SQLAlchemyAdapter(args.db_url, settings)
        adapter.test_connection()
    except Exception as exc:
        logger.error("Could not connect to the database: %s", exc)
        return 1

    from uada.scl.loader import SCLLoader
    from uada.scl.reflector import SCLReflector

    scl = SCLReflector().from_adapter(adapter)
    SCLLoader.save(scl, output_path)

    logger.info(
        "Discovered %d table(s), %d foreign key(s). SCL written to %s.",
        len(scl.tables),
        len(scl.joins),
        output_path,
    )
    logger.info("")
    logger.info("Next: Edit %s to add:", output_path)
    logger.info("  - table/column descriptions")
    logger.info("  - metric definitions")
    logger.info("  - glossary terms")
    logger.info("  - example Q&A pairs")
    logger.info("Then run: python scripts/build_index.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
