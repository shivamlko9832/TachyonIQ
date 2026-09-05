"""
Conversation Store
==================
Loads and persists ConversationState objects keyed by session_id.

Backend selection (runtime, no code changes needed):
  - redis_url set in Settings → Redis backend (production)
  - redis_url is None         → In-memory dict (POC / test)

Both backends expose the same async interface so callers never change.
Redis keys are namespaced to "{database_id}:{session_id}" and given a TTL.
Serialisation uses Pydantic's model_dump_json / model_validate_json.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from uada.models.conversation import ConversationState

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.models.conversation import ConversationTurn

logger = logging.getLogger(__name__)


class ConversationStore:
    """
    ConversationState store with automatic Redis / in-memory backend selection.

    Pass `settings` (always required) and an optional `database_id` that
    scopes Redis keys so multiple database deployments can share one Redis.
    """

    def __init__(self, settings: Settings, database_id: str = "default") -> None:
        self._settings = settings
        self._database_id = database_id
        self._redis: object | None = None          # redis.asyncio.Redis when connected
        self._sessions: dict[str, ConversationState] = {}  # fallback in-memory store

        if settings.redis_url:
            self._redis = self._connect_redis(settings.redis_url)

    # ── Redis connection (lazy, non-blocking) ─────────────────────────────────
    @staticmethod
    def _connect_redis(url: str) -> object | None:
        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]
            client = aioredis.from_url(url, decode_responses=True)
            logger.info("ConversationStore: Redis backend at %s", url.split("@")[-1])
            return client
        except ImportError:
            logger.warning(
                "redis package not installed (pip install redis). "
                "Falling back to in-memory ConversationStore."
            )
            return None
        except Exception as exc:
            logger.warning("Redis connection failed (%s). Falling back to in-memory.", exc)
            return None

    def _redis_key(self, session_id: str) -> str:
        return f"uada:{self._database_id}:{session_id}"

    # ── Public interface ──────────────────────────────────────────────────────
    async def load(self, session_id: str) -> ConversationState:
        """Return the session's ConversationState, creating a fresh one if absent."""
        if self._redis is not None:
            return await self._redis_load(session_id)
        return await self._mem_load(session_id)

    async def save(self, state: ConversationState) -> None:
        """Persist the state, compressing old turns if over the max-turns limit."""
        self._compress_if_needed(state)
        state.last_updated = datetime.now(tz=UTC)

        if self._redis is not None:
            await self._redis_save(state)
        else:
            await self._mem_save(state)

    async def delete(self, session_id: str) -> None:
        """Remove a session from the store."""
        if self._redis is not None:
            try:
                await self._redis.delete(self._redis_key(session_id))  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning("Redis delete failed for %s: %s", session_id, exc)
        else:
            self._sessions.pop(session_id, None)

    # ── In-memory backend ─────────────────────────────────────────────────────
    async def _mem_load(self, session_id: str) -> ConversationState:
        if session_id not in self._sessions:
            now = datetime.now(tz=UTC)
            self._sessions[session_id] = ConversationState(
                session_id=session_id,
                database_id=self._database_id,
                created_at=now,
                last_updated=now,
            )
            logger.info("New in-memory session '%s'.", session_id)
        return self._sessions[session_id]

    async def _mem_save(self, state: ConversationState) -> None:
        self._sessions[state.session_id] = state

    # ── Redis backend ─────────────────────────────────────────────────────────
    async def _redis_load(self, session_id: str) -> ConversationState:
        key = self._redis_key(session_id)
        try:
            raw = await self._redis.get(key)  # type: ignore[union-attr]
            if raw:
                return ConversationState.model_validate_json(raw)
        except Exception as exc:
            logger.warning("Redis load failed for %s: %s — creating fresh session.", session_id, exc)

        now = datetime.now(tz=UTC)
        state = ConversationState(
            session_id=session_id,
            database_id=self._database_id,
            created_at=now,
            last_updated=now,
        )
        logger.info("New Redis session '%s'.", session_id)
        return state

    async def _redis_save(self, state: ConversationState) -> None:
        key = self._redis_key(state.session_id)
        try:
            payload = state.model_dump_json()
            await self._redis.set(key, payload, ex=self._settings.redis_ttl_seconds)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("Redis save failed for %s: %s — state not persisted.", state.session_id, exc)

    # ── Compression ───────────────────────────────────────────────────────────
    def _compress_if_needed(self, state: ConversationState) -> None:
        """
        If the turn history exceeds conversation_max_turns, summarise the
        oldest turn into compressed_context and drop it.  The summary is
        deterministic (no LLM) — it appends a one-line digest of that turn.
        """
        max_turns = self._settings.conversation_max_turns
        while len(state.turns) > max_turns:
            oldest = state.turns.pop(0)
            _summary_part = (
                f" | rows: {oldest.result_summary}" if oldest.result_summary else ""
            )
            summary_line = (
                f"[turn {oldest.turn_id}] Q: {oldest.user_question[:120]} "
                f"→ tables: {', '.join(oldest.tables_used or [])}{_summary_part}"
            )
            prior = state.compressed_context or ""
            state.compressed_context = (prior + "\n" + summary_line).strip()
            logger.debug("Compressed turn %d from session %s.", oldest.turn_id, state.session_id)
