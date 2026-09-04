"""
Tests for ConversationStore (uada/pipeline/conversation_store.py).

Pure in-memory logic -- no LLM, no database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from uada.config import Settings
from uada.models.conversation import ConversationTurn, TurnStatus
from uada.pipeline.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


def _turn(turn_id: int, question: str, summary: str | None = None) -> ConversationTurn:
    return ConversationTurn(
        turn_id=turn_id,
        timestamp=datetime.now(tz=UTC),
        user_question=question,
        status=TurnStatus.SUCCESS,
        result_summary=summary,
    )


@pytest.fixture
def store() -> ConversationStore:
    settings = Settings(db_url="sqlite:///:memory:", conversation_max_turns=2)  # type: ignore[call-arg]
    return ConversationStore(settings, database_id="test_db")


class TestLoad:
    async def test_creates_new_session(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        assert state.session_id == "session-1"
        assert state.database_id == "test_db"
        assert state.turns == []

    async def test_returns_same_state_on_repeated_load(self, store: ConversationStore) -> None:
        first = await store.load("session-1")
        first.turns.append(_turn(0, "What was revenue?"))
        second = await store.load("session-1")
        assert second is first
        assert len(second.turns) == 1

    async def test_different_sessions_are_independent(self, store: ConversationStore) -> None:
        a = await store.load("session-a")
        b = await store.load("session-b")
        assert a is not b
        assert a.session_id != b.session_id


class TestSave:
    async def test_save_persists_state(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        state.turns.append(_turn(0, "What was revenue?"))
        await store.save(state)

        reloaded = await store.load("session-1")
        assert len(reloaded.turns) == 1

    async def test_save_updates_last_updated(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        original = state.last_updated
        await store.save(state)
        assert state.last_updated >= original

    async def test_no_compression_under_limit(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        state.turns.append(_turn(0, "Q1"))
        state.turns.append(_turn(1, "Q2"))
        await store.save(state)  # max_turns=2, exactly at limit
        assert len(state.turns) == 2
        assert state.compressed_context is None

    async def test_compresses_oldest_turn_over_limit(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        state.turns.append(_turn(0, "Q1", summary="12 rows"))
        state.turns.append(_turn(1, "Q2"))
        state.turns.append(_turn(2, "Q3"))
        await store.save(state)  # max_turns=2, one over

        assert len(state.turns) == 2
        assert [t.turn_id for t in state.turns] == [1, 2]
        assert state.compressed_context is not None
        assert "Q1" in state.compressed_context
        assert "12 rows" in state.compressed_context

    async def test_compresses_multiple_turns_in_one_save(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        for i in range(5):
            state.turns.append(_turn(i, f"Q{i}"))
        await store.save(state)  # max_turns=2, three over

        assert len(state.turns) == 2
        assert [t.turn_id for t in state.turns] == [3, 4]
        assert state.compressed_context is not None
        assert "Q0" in state.compressed_context
        assert "Q1" in state.compressed_context
        assert "Q2" in state.compressed_context

    async def test_compression_accumulates_across_saves(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        state.turns.append(_turn(0, "Q0"))
        state.turns.append(_turn(1, "Q1"))
        state.turns.append(_turn(2, "Q2"))
        await store.save(state)
        assert state.compressed_context is not None
        first_compression = state.compressed_context

        state.turns.append(_turn(3, "Q3"))
        await store.save(state)
        assert state.compressed_context is not None
        assert state.compressed_context.startswith(first_compression)
        assert "Q1" in state.compressed_context


class TestDelete:
    async def test_delete_removes_session(self, store: ConversationStore) -> None:
        state = await store.load("session-1")
        state.turns.append(_turn(0, "Q1"))
        await store.save(state)

        await store.delete("session-1")

        fresh = await store.load("session-1")
        assert fresh.turns == []

    async def test_delete_unknown_session_is_noop(self, store: ConversationStore) -> None:
        await store.delete("never-existed")
