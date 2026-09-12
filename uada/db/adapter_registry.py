"""
Adapter Registry (P1-3)
========================
Maps a dialect name to a factory that produces a DatabaseAdapter.
Adapters are loaded lazily so missing optional drivers only raise at
the point of use, not at import time.

Usage:
    adapter = AdapterRegistry.build(config, settings)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.db.connection_manager import ConnectionConfig
    from uada.db.interface import DatabaseAdapter

logger = logging.getLogger(__name__)

# Dialects that ship with the core SQLAlchemy install (no extra driver needed).
_BUILTIN_DIALECTS = frozenset({"postgresql", "mysql", "sqlite", "mssql"})


class AdapterRegistry:
    """Factory that returns the correct DatabaseAdapter subclass for a dialect."""

    @classmethod
    def build(cls, config: ConnectionConfig, settings: Settings) -> DatabaseAdapter:
        """Instantiate and return the appropriate adapter for `config.dialect`."""
        dialect = config.dialect.lower()

        if dialect in _BUILTIN_DIALECTS:
            from uada.db.adapter import SQLAlchemyAdapter
            return SQLAlchemyAdapter(config.to_url(), settings)

        if dialect == "snowflake":
            from uada.db.adapters.snowflake import SnowflakeAdapter
            return SnowflakeAdapter(config, settings)

        if dialect == "bigquery":
            from uada.db.adapters.bigquery import BigQueryAdapter
            return BigQueryAdapter(config, settings)

        if dialect == "redshift":
            from uada.db.adapters.redshift import RedshiftAdapter
            return RedshiftAdapter(config, settings)

        if dialect == "duckdb":
            from uada.db.adapters.duckdb import DuckDBAdapter
            return DuckDBAdapter(config, settings)

        # Fallback: try SQLAlchemy anyway — the caller may have installed a
        # third-party dialect. If the URL is invalid the engine creation fails
        # with a clear error from SQLAlchemy.
        logger.warning("Unknown dialect %r — falling back to generic SQLAlchemyAdapter.", dialect)
        from uada.db.adapter import SQLAlchemyAdapter
        return SQLAlchemyAdapter(config.to_url(), settings)
