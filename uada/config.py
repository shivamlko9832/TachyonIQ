"""
UADA Configuration
==================
All runtime configuration is loaded from environment variables (or .env).
No configuration value is hardcoded in business logic — always read from settings.

Usage:
    from uada.config import settings
    db_url = settings.db_url
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VectorBackend(str, Enum):
    CHROMA = "chroma"
    PGVECTOR = "pgvector"


class LLMProvider(str, Enum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Settings(BaseSettings):
    """
    All UADA runtime configuration.
    Loaded from environment variables or .env file.
    Every field has a sensible default for local development.
    """

    model_config = SettingsConfigDict(
        env_prefix="UADA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ─────────────────────────────────────────────────────────────
    db_url: SecretStr = Field(
        description="SQLAlchemy connection string for the client database. "
        "Must be a read-only account.",
    )
    db_query_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Maximum seconds a generated SQL query may run.",
    )
    db_max_rows: int = Field(
        default=1000,
        ge=1,
        le=50_000,
        description="Maximum rows returned per query. Enforced post-execution.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_pool_recycle_seconds: int = Field(default=1800)

    # ── LLM ──────────────────────────────────────────────────────────────────
    llm_model: str = Field(
        default="ollama:qwen2.5-coder:7b",
        description="PydanticAI model string. Examples: "
        "'ollama:qwen2.5-coder:7b', "
        "'claude-sonnet-4-6', "
        "'openai:gpt-4o'",
    )
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=2048, ge=256, le=8192)
    llm_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Maximum LLM retry attempts per pipeline stage. "
        "Does NOT apply to security violations — those always stop.",
    )

    # ── Embedding ─────────────────────────────────────────────────────────────
    embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5",
        description="Sentence Transformers model name for schema embeddings.",
    )
    embedding_batch_size: int = Field(default=64, ge=1, le=512)

    # ── Vector Store ──────────────────────────────────────────────────────────
    vector_backend: VectorBackend = Field(
        default=VectorBackend.CHROMA,
        description="Vector store backend. Use 'chroma' for POC, 'pgvector' for production.",
    )
    chroma_path: Path = Field(
        default=Path("./data/chroma"),
        description="Persistent ChromaDB storage path.",
    )
    chroma_collection_name: str = Field(default="uada_schema_context")
    retrieval_top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of candidates retrieved before RRF merge.",
    )
    retrieval_final_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of schema elements returned after RRF merge.",
    )

    # ── SCL ───────────────────────────────────────────────────────────────────
    scl_path: Path = Field(
        default=Path("./config/semantic_context.yaml"),
        description="Path to the Semantic Context Layer YAML file.",
    )

    # ── Security ──────────────────────────────────────────────────────────────
    sql_max_subquery_depth: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum nesting depth of subqueries in generated SQL.",
    )
    sql_inject_limit: bool = Field(
        default=True,
        description="Automatically inject LIMIT if not present in generated SQL.",
    )
    prompt_max_length: int = Field(
        default=4096,
        description="Maximum user question length (characters). Longer inputs rejected.",
    )

    # ── Conversation ──────────────────────────────────────────────────────────
    conversation_max_turns: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Number of full turns kept in session context. Older turns are compressed.",
    )
    conversation_context_budget_tokens: int = Field(
        default=2000,
        description="Maximum tokens allocated to conversation history in prompts.",
    )

    # ── Observability ─────────────────────────────────────────────────────────
    langfuse_host: str = Field(
        default="http://localhost:3000",
        description="Langfuse self-hosted endpoint.",
    )
    langfuse_secret_key: SecretStr | None = Field(default=None)
    langfuse_public_key: SecretStr | None = Field(default=None)
    otel_enabled: bool = Field(
        default=True,
        description="Enable OpenTelemetry tracing.",
    )

    # ── API ───────────────────────────────────────────────────────────────────
    api_key: SecretStr | None = Field(
        default=None,
        description="API key for request authentication. If None, auth is disabled (local dev only).",
    )
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1024, le=65535)
    api_debug: bool = Field(default=False)

    # ── Paths ─────────────────────────────────────────────────────────────────
    data_dir: Path = Field(default=Path("./data"))
    log_level: str = Field(default="INFO")

    @field_validator("llm_model")
    @classmethod
    def validate_llm_model(cls, v: str) -> str:
        """Ensure model string is non-empty."""
        if not v.strip():
            raise ValueError("llm_model must not be empty")
        return v.strip()


# Module-level singleton — import and use directly.
settings = Settings()  # type: ignore[call-arg]
