"""Ask-a-question endpoint -- the primary entry point into the pipeline."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from uada.models.result import UADAResponse

if TYPE_CHECKING:
    from uada.pipeline.orchestrator import PipelineOrchestrator

router = APIRouter()

# Loaded once at request time so tests that monkey-patch settings still work.
def _get_max_length() -> int:
    try:
        from uada.config import settings
        return settings.prompt_max_length
    except Exception:
        return 4096


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, description="The natural-language question.")
    session_id: str | None = Field(
        default=None, description="Existing session to continue, or None to start a new one."
    )
    connection_id: str | None = Field(
        default=None,
        description="Optional connection ID for multi-tenant use. "
                    "Selects which database connection to query.",
    )

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        # Enforce prompt_max_length from settings.
        max_len = _get_max_length()
        if len(v) > max_len:
            raise ValueError(
                f"Question exceeds maximum length of {max_len} characters "
                f"(got {len(v)})."
            )
        # Strip leading/trailing whitespace — don't pass raw whitespace to LLM.
        v = v.strip()
        if not v:
            raise ValueError("Question must contain non-whitespace content.")
        return v


@router.post("/query", response_model=UADAResponse)
async def post_query(request: Request, body: QueryRequest) -> UADAResponse:
    """
    Run ``body.question`` through the full pipeline.

    A missing ``session_id`` starts a new conversation session. The
    response is always HTTP 200: success/failure is signalled by
    ``UADAResponse.is_success`` / ``.error``, per PipelineOrchestrator.run()
    which never raises.
    """
    orchestrator: PipelineOrchestrator = request.app.state.orchestrator
    session_id = body.session_id or str(uuid.uuid4())
    user_id: str | None = getattr(request.state, "user_id", None)
    return await orchestrator.run(body.question, session_id, user_id=user_id)


@router.get("/query/stream")
async def get_query_stream(
    request: Request,
    question: str,
    session_id: str | None = None,
) -> Any:
    """
    SSE streaming version of POST /query.

    Emits ``progress`` events as each pipeline stage starts and completes,
    then a final ``result`` event containing the complete UADAResponse JSON.
    The response is HTTP 200 with Content-Type: text/event-stream; the client
    should use the EventSource API or an SSE-capable fetch wrapper.

    Requires ``sse-starlette`` to be installed (``pip install sse-starlette``).
    Returns HTTP 503 if the package is unavailable.
    """
    try:
        from sse_starlette.sse import EventSourceResponse
    except ImportError:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"detail": "SSE streaming requires 'sse-starlette'. Install it with: pip install sse-starlette"},
        )

    # Enforce prompt_max_length for the streaming path too.
    max_len = _get_max_length()
    if len(question) > max_len:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=422,
            content={"detail": f"Question exceeds maximum length of {max_len} characters."},
        )
    question = question.strip()
    if not question:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"detail": "Question must not be empty."})

    orchestrator: PipelineOrchestrator = request.app.state.orchestrator
    sid = session_id or str(uuid.uuid4())
    user_id: str | None = getattr(request.state, "user_id", None)

    async def _generator():
        async for event in orchestrator.stream_run(question, sid, user_id=user_id):
            yield event

    return EventSourceResponse(_generator())
