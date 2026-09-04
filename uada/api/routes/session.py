"""Session management endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class DeleteSessionResponse(BaseModel):
    deleted: bool


@router.delete("/session/{session_id}", response_model=DeleteSessionResponse)
async def delete_session(session_id: str, request: Request) -> DeleteSessionResponse:
    """Delete a conversation session's state. Idempotent -- always reports deleted."""
    orchestrator = request.app.state.orchestrator
    await orchestrator.conversation_store.delete(session_id)
    return DeleteSessionResponse(deleted=True)
