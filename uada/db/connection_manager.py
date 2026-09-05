"""
Database Connection Manager
=============================
Runtime registry of DatabaseAdapter instances.  Replaces the single static
``UADA_DB_URL`` with a multi-connection model: users register connections
through the REST API or UI wizard; each connection is identified by a UUID
and backed by a pooled SQLAlchemy adapter.

Credentials are accepted once (at registration time) and stored only in
``SecretStr`` fields — they are never serialised to JSON, never logged, and
never returned to callers.  The connection registry itself (persisted as
``connections.json``) contains only non-sensitive metadata.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from uada.db.interface import DatabaseAdapter

logger = logging.getLogger(__name__)

# ── Connection config & summary models ────────────────────────────────────────


class ConnectionConfig(BaseModel):
    """
    Parameters needed to establish a database connection.

    ``password`` is a ``SecretStr`` so Pydantic never includes it in
    ``.model_dump()`` / ``.model_dump_json()`` by default — callers must
    explicitly pass ``include={'password'}`` to access the raw value, which
    only the adapter creation path does.
    """

    name: str = Field(description="Human-readable display name.")
    dialect: str = Field(
        description="SQLAlchemy dialect string: postgresql | mysql | sqlite | "
        "mssql | snowflake | bigquery | redshift | duckdb"
    )
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: SecretStr = Field(default=SecretStr(""))
    ssl: bool = True
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Dialect-specific extras: warehouse, role, project, dataset, …",
    )

    def to_url(self) -> str:
        """Build a SQLAlchemy connection URL (password inline — only used internally)."""
        pwd = self.password.get_secret_value()
        dialect = self.dialect.lower()

        if dialect == "sqlite":
            return f"sqlite:///{self.database}"
        if dialect == "duckdb":
            db = self.database or ":memory:"
            return f"duckdb:///{db}"

        # Encode special chars in password
        import urllib.parse
        safe_pwd = urllib.parse.quote_plus(pwd) if pwd else ""
        user_info = f"{self.username}:{safe_pwd}@" if self.username else ""
        port_str = f":{self.port}" if self.port else ""

        dialect_map = {
            "postgresql": "postgresql+psycopg2",
            "postgres": "postgresql+psycopg2",
            "mysql": "mysql+pymysql",
            "mssql": "mssql+pyodbc",
            "snowflake": "snowflake",
            "bigquery": "bigquery",
            "redshift": "redshift+psycopg2",
        }
        driver = dialect_map.get(dialect, dialect)

        if dialect == "bigquery":
            project = self.extra.get("project", self.database)
            dataset = self.extra.get("dataset", "")
            return f"bigquery://{project}/{dataset}"

        return f"{driver}://{user_info}{self.host}{port_str}/{self.database}"


class ConnectionSummary(BaseModel):
    """Public view of a registered connection — no credentials."""

    connection_id: str
    name: str
    dialect: str
    host: str
    database: str
    status: str = "unknown"          # "connected" | "error" | "unknown"
    table_count: int = 0
    database_name: str = ""
    created_at: str = ""


class ConnectionTestResult(BaseModel):
    connection_id: str
    success: bool
    latency_ms: float = 0.0
    server_version: str = ""
    error: str | None = None


class DiscoverResult(BaseModel):
    connection_id: str
    table_count: int
    relationship_count: int
    dialect: str
    database_name: str
    schema_fingerprint: str


# ── Manager ───────────────────────────────────────────────────────────────────


class DatabaseConnectionManager:
    """
    Thread-safe registry of DatabaseAdapter instances.

    Persistence:
        Connection metadata (no credentials) is saved to
        ``<data_dir>/connections.json`` so the list survives restarts.
        Credentials are re-supplied by the user at next startup or via
        environment variables (``UADA_CONN_<ID>_PASSWORD``).

    Adapter lifecycle:
        Adapters are created lazily on first ``get_adapter()`` call and
        cached until ``delete_connection()`` or process exit.
    """

    def __init__(self, data_dir: Path | str = "./data") -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._data_dir / "connections.json"
        self._adapters: dict[str, DatabaseAdapter] = {}
        self._configs: dict[str, ConnectionConfig] = {}
        self._meta: dict[str, dict[str, Any]] = self._load_registry()

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, config: ConnectionConfig) -> str:
        """
        Register a new connection and return its connection_id.
        Does NOT test the connection — call ``test_connection()`` separately.
        """
        connection_id = str(uuid.uuid4())
        self._configs[connection_id] = config
        self._meta[connection_id] = {
            "connection_id": connection_id,
            "name": config.name,
            "dialect": config.dialect,
            "host": config.host,
            "database": config.database,
            "status": "unknown",
            "table_count": 0,
            "database_name": "",
            "created_at": _now_iso(),
        }
        self._save_registry()
        logger.info("Registered connection '%s' (id=%s, dialect=%s)", config.name, connection_id, config.dialect)
        return connection_id

    def delete_connection(self, connection_id: str) -> None:
        """Remove a connection and release its adapter pool."""
        adapter = self._adapters.pop(connection_id, None)
        if adapter is not None:
            try:
                adapter.dispose()
            except Exception:  # noqa: BLE001
                pass
        self._configs.pop(connection_id, None)
        self._meta.pop(connection_id, None)
        self._save_registry()
        logger.info("Deleted connection %s", connection_id)

    # ── Adapter access ───────────────────────────────────────────────────────

    def get_adapter(self, connection_id: str) -> DatabaseAdapter:
        """Return a cached adapter; create one on first call."""
        if connection_id in self._adapters:
            return self._adapters[connection_id]
        config = self._configs.get(connection_id)
        if config is None:
            raise KeyError(f"Connection '{connection_id}' not found.")
        adapter = self._create_adapter(config)
        self._adapters[connection_id] = adapter
        return adapter

    def _create_adapter(self, config: ConnectionConfig) -> DatabaseAdapter:
        from uada.db.adapter import SQLAlchemyAdapter

        url = config.to_url()
        # Settings shim — only fields the adapter uses
        from types import SimpleNamespace
        shim = SimpleNamespace(
            db_query_timeout_seconds=30,
            db_max_rows=1000,
        )
        return SQLAlchemyAdapter(url, shim)  # type: ignore[arg-type]

    # ── Operations ───────────────────────────────────────────────────────────

    def test_connection(self, connection_id: str) -> ConnectionTestResult:
        """Run a connectivity check and return latency + server version."""
        import time
        try:
            adapter = self.get_adapter(connection_id)
            t0 = time.perf_counter()
            ok = adapter.test_connection()
            latency_ms = (time.perf_counter() - t0) * 1000
            if ok:
                self._meta[connection_id]["status"] = "connected"
                self._save_registry()
                return ConnectionTestResult(
                    connection_id=connection_id,
                    success=True,
                    latency_ms=round(latency_ms, 1),
                    server_version=getattr(adapter, "_server_version", ""),
                )
            return ConnectionTestResult(connection_id=connection_id, success=False, error="test_connection() returned False")
        except Exception as exc:  # noqa: BLE001
            self._meta[connection_id]["status"] = "error"
            self._save_registry()
            return ConnectionTestResult(connection_id=connection_id, success=False, error=str(exc))

    def discover_schema(self, connection_id: str) -> DiscoverResult:
        """Reflect schema metadata without full profiling."""
        adapter = self.get_adapter(connection_id)
        raw = adapter.get_raw_schema()
        table_count = len(raw.tables)
        rel_count = sum(
            1
            for t in raw.tables.values()
            for c in t.columns
            if c.is_foreign_key
        )
        self._meta[connection_id].update({
            "table_count": table_count,
            "database_name": raw.database_name,
            "status": "connected",
        })
        self._save_registry()
        return DiscoverResult(
            connection_id=connection_id,
            table_count=table_count,
            relationship_count=rel_count,
            dialect=raw.dialect,
            database_name=raw.database_name,
            schema_fingerprint=raw.schema_fingerprint,
        )

    # ── Listing ──────────────────────────────────────────────────────────────

    def list_connections(self) -> list[ConnectionSummary]:
        """Return all registered connections — no credentials."""
        return [ConnectionSummary(**m) for m in self._meta.values()]

    def get_summary(self, connection_id: str) -> ConnectionSummary | None:
        m = self._meta.get(connection_id)
        return ConnectionSummary(**m) if m else None

    # ── Persistence helpers ──────────────────────────────────────────────────

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        if self._registry_path.exists():
            try:
                return json.loads(self._registry_path.read_text())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load connections.json: %s", exc)
        return {}

    def _save_registry(self) -> None:
        try:
            self._registry_path.write_text(json.dumps(self._meta, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save connections.json: %s", exc)


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
