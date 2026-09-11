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

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic-settings' own env_file=".env" parsing (below) only populates
# this Settings model's fields -- it never exports values into the real
# process environment. Provider SDKs pydantic-ai depends on (OpenAI,
# Anthropic, ...) read ANTHROPIC_API_KEY/OPENAI_API_KEY directly from
# os.environ, so without this, exactly the setup .env.example itself
# documents (drop the key in .env, nothing else) silently leaves those
# keys invisible to anything but this Settings object.
load_dotenv()


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
        # .env.example itself documents ANTHROPIC_API_KEY/OPENAI_API_KEY
        # living in the same .env file, unprefixed, for the provider SDKs
        # to read directly -- env_prefix only filters which *matching*
        # keys map to a field, it doesn't make pydantic-settings ignore
        # the rest of a dotenv file's other keys, so without extra="ignore"
        # any such key (present exactly as documented) crashes Settings()
        # construction with "Extra inputs are not permitted".
        extra="ignore",
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
    llm_router_model: str = Field(
        default="",
        description="Model for the Complexity Router (fast question classifier). "
        "When empty, falls back to llm_model. "
        "Set to a faster model such as 'claude-haiku-4-5' to reduce "
        "classification latency without affecting SQL generation quality.",
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

    # ── Redis (optional — ConversationStore falls back to in-memory if unset) ──
    redis_url: str | None = Field(
        default=None,
        description="Redis connection URL, e.g. redis://localhost:6379/0. "
                    "If unset, ConversationStore uses an in-memory dict (POC mode).",
    )
    redis_ttl_seconds: int = Field(
        default=86400,
        description="Session TTL in Redis (seconds). Default 24 h.",
    )

    # ── RBAC / JWT (optional — disabled when jwt_secret_key is None) ──────────
    jwt_secret_key: SecretStr | None = Field(
        default=None,
        description="Secret for HS256 JWT verification. If None, JWT RBAC is disabled.",
    )
    jwt_algorithm: str = Field(default="HS256")
    rbac_enabled: bool = Field(
        default=False,
        description="Enable role-based access control (requires jwt_secret_key).",
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_enabled: bool = Field(
        default=False,
        description="Enable per-client sliding-window rate limiting. "
                    "Disabled by default for local dev; enable in production.",
    )
    rate_limit_rpm: int = Field(
        default=60,
        ge=1,
        le=10_000,
        description="Maximum requests per minute per client IP.",
    )
    rate_limit_burst: int = Field(
        default=10,
        ge=1,
        le=500,
        description="Additional burst allowance above rate_limit_rpm. "
                    "Total capacity = rpm + burst.",
    )

    # ── Result quality (P4) ──────────────────────────────────────────────────
    enable_result_critic: bool = Field(
        default=True,
        description="Enable the deterministic ResultCritic quality gate. "
                    "When True, each query result is scored before insight "
                    "generation; a score below the sufficiency threshold "
                    "triggers the Replanner (if also enabled). "
                    "Disable to skip quality gating entirely.",
    )
    enable_replanner: bool = Field(
        default=True,
        description="Enable the deterministic Replanner retry loop. "
                    "Has no effect when enable_result_critic is False. "
                    "When True, a failed ResultCritic evaluation triggers "
                    "one plan-mutation + SQL-regeneration retry.",
    )

    # ── Audit logging ─────────────────────────────────────────────────────────
    audit_log_path: Path | None = Field(
        default=None,
        description="Path for the structured audit JSONL log. "
                    "If None, audit records are emitted to the application logger only.",
    )

    @field_validator("llm_model")
    @classmethod
    def validate_llm_model(cls, v: str) -> str:
        """Ensure model string is non-empty."""
        if not v.strip():
            raise ValueError("llm_model must not be empty")
        return v.strip()


# Module-level singleton — import and use directly.
settings = Settings()  # type: ignore[call-arg]
