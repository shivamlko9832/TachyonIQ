"""
Conversation Store (pipeline step 1 dependency)
==================================================
In-memory session store for the POC. Loaded at the start of every turn
and saved at the end (even on failure), per the Conversation Manager step
of the pipeline.

NOTE for production: replace the dict with Redis or PostgreSQL. The
interface (load/save/delete) stays the same -- only the storage backend
changes, so callers never need to change.
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
    In-memory ConversationState store, keyed by session_id.

    `settings` and `database_id` aren't in the phase spec's bare
    `__init__(self)` pseudocode, but `save()`'s compression threshold
    (`settings.conversation_max_turns`) and a new session's required
    `database_id` field have to come from somewhere -- constructor
    injection matches every other pipeline component's dependency style.
    """

    def __init__(self, settings: Settings, database_id: str = "default") -> None:
        self._settings = settings
        self._database_id = database_id
        self._sessions: dict[str, ConversationState] = {}

    async def load(self, session_id: str) -> ConversationState:
        """Return the session's state, creating a fresh one if it doesn't exist."""
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing

        now = datetime.now(tz=UTC)
        state = ConversationState(
            session_id=session_id,
            database_id=self._database_id,
            created_at=now,
            last_updated=now,
        )
        self._sessions[session_id] = state
        logger.info("New conversation session '%s' created.", session_id)
        return state

    async def save(self, state: ConversationState) -> None:
        """
        Persist `state`, compressing turns beyond `conversation_max_turns`
        into `compressed_context` first.
        """
        while state.turn_count > self._settings.conversation_max_turns:
            self._compress_oldest_turn(state)

        state.last_updated = datetime.now(tz=UTC)
        self._sessions[state.session_id] = state

    async def delete(self, session_id: str) -> None:
        """Remove a session's state, if present."""
        self._sessions.pop(session_id, None)

    def _compress_oldest_turn(self, state: ConversationState) -> None:
        oldest = state.turns.pop(0)
        summary = self._summarize_turn(oldest)
        state.compressed_context = (
            f"{state.compressed_context} {summary}" if state.compressed_context else summary
        )

    def _summarize_turn(self, turn: ConversationTurn) -> str:
        parts = [f"Turn {turn.turn_id}: '{turn.user_question}'"]
        if turn.result_summary:
            parts.append(f"-> {turn.result_summary}")
        return " ".join(parts)
