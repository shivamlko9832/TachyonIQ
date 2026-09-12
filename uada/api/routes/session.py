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
    policy = getattr(request.state, "connection_policy", None)
    owner_id = (
        "anonymous"
        if policy is None
        else None
        if getattr(policy, "role", "viewer") == "admin"
        else getattr(policy, "user_id", "anonymous")
    )
    try:
        await orchestrator.conversation_store.delete(session_id, owner_id=owner_id)
    except PermissionError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found.") from exc
    return DeleteSessionResponse(deleted=True)
