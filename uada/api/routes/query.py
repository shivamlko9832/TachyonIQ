"""Ask-a-question endpoint -- the primary entry point into the pipeline."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from uada.models.result import UADAResponse

if TYPE_CHECKING:
    from uada.pipeline.orchestrator import PipelineOrchestrator

router = APIRouter()


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, description="The natural-language question.")
    session_id: str | None = Field(
        default=None, description="Existing session to continue, or None to start a new one."
    )


@router.post("/query", response_model=UADAResponse)
async def post_query(request: Request, body: QueryRequest) -> UADAResponse:
    """
    Run `body.question` through the full pipeline.

    A missing `session_id` starts a new conversation session. The
    response is always HTTP 200: success/failure is signalled by
    `UADAResponse.is_success` / `.error`, per PipelineOrchestrator.run()
    which never raises.
    """
    orchestrator: PipelineOrchestrator = request.app.state.orchestrator
    session_id = body.session_id or str(uuid.uuid4())
    return await orchestrator.run(body.question, session_id)
