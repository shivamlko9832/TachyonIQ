"""
Saved Analyses Routes (P3-4)
==============================
REST endpoints for saving, listing, retrieving, and deleting analyses.

POST   /analyses                — save an analysis
GET    /analyses                — list saved analyses (optional filters)
GET    /analyses/{id}          — retrieve a single saved analysis (full response)
DELETE /analyses/{id}          — delete a saved analysis
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/analyses", tags=["analyses"])
logger = logging.getLogger(__name__)


# ── Dependency ────────────────────────────────────────────────────────────────

def _get_store(request: Request):
    store = getattr(request.app.state, "saved_analysis_store", None)
    if store is None:
        from uada.db.saved_analysis_store import SavedAnalysisStore
        # Lazy-initialise with a default SQLite file (avoids explicit startup step)
        store = SavedAnalysisStore()
        request.app.state.saved_analysis_store = store
    return store


def _owner_scope(request: Request) -> str | None:
    policy = getattr(request.state, "connection_policy", None)
    if policy is None:
        return "anonymous"
    if getattr(policy, "role", "viewer") == "admin":
        return None
    return getattr(policy, "user_id", "anonymous")


# ── Request / Response models ─────────────────────────────────────────────────

class SaveAnalysisRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="Human-readable name.")
    question: str = Field(..., min_length=1, description="The original user question.")
    sql_text: str | None = Field(default=None, description="The SQL that was executed.")
    response: dict[str, Any] = Field(..., description="The full UADAResponse as a JSON object.")
    connection_id: str | None = Field(default=None, description="Database connection ID.")
    tags: list[str] = Field(default_factory=list, description="Optional list of string tags.")


class SaveAnalysisResponse(BaseModel):
    id: str
    name: str
    message: str = "Analysis saved successfully."


class AnalysisSummary(BaseModel):
    """Lightweight representation returned by the list endpoint."""

    id: str
    name: str
    question: str
    connection_id: str | None
    tags: list[str]
    created_at: str
    updated_at: str


class AnalysisDetail(AnalysisSummary):
    """Full record including the stored UADAResponse."""

    sql_text: str | None
    response: dict[str, Any]


class ListAnalysesResponse(BaseModel):
    total: int
    analyses: list[AnalysisSummary]


class DeleteAnalysisResponse(BaseModel):
    id: str
    message: str = "Analysis deleted successfully."


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", response_model=SaveAnalysisResponse, status_code=201)
async def save_analysis(
    body: SaveAnalysisRequest,
    request: Request,
    store=Depends(_get_store),
) -> SaveAnalysisResponse:
    """Save a query result as a named analysis for later replay."""
    import json

    analysis_id = store.save(
        name=body.name,
        question=body.question,
        sql_text=body.sql_text,
        response=body.response,
        connection_id=body.connection_id,
        tags=body.tags,
        owner_id=_owner_scope(request),
    )
    return SaveAnalysisResponse(id=analysis_id, name=body.name)


@router.get("", response_model=ListAnalysesResponse)
async def list_analyses(
    request: Request,
    connection_id: str | None = Query(default=None, description="Filter by connection ID."),
    tag: str | None = Query(default=None, description="Filter by tag (single tag match)."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    store=Depends(_get_store),
) -> ListAnalysesResponse:
    """List saved analyses, newest first.  Optionally filter by connection or tag."""
    owner_id = _owner_scope(request)
    rows = store.list(
        connection_id=connection_id,
        tag=tag,
        limit=limit,
        offset=offset,
        owner_id=owner_id,
    )
    total = store.count(connection_id=connection_id, owner_id=owner_id)
    summaries = [
        AnalysisSummary(
            id=r["id"],
            name=r["name"],
            question=r["question"],
            connection_id=r.get("connection_id"),
            tags=r.get("tags", []),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return ListAnalysesResponse(total=total, analyses=summaries)


@router.get("/{analysis_id}", response_model=AnalysisDetail)
async def get_analysis(
    analysis_id: str,
    request: Request,
    store=Depends(_get_store),
) -> AnalysisDetail:
    """Retrieve the full saved analysis including the UADAResponse payload."""
    import json

    row = store.get(analysis_id, owner_id=_owner_scope(request))
    if row is None:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id!r} not found.")

    response_data = row.get("response", {})
    if isinstance(response_data, str):
        try:
            response_data = json.loads(response_data)
        except Exception:
            response_data = {}

    return AnalysisDetail(
        id=row["id"],
        name=row["name"],
        question=row["question"],
        sql_text=row.get("sql_text"),
        response=response_data,
        connection_id=row.get("connection_id"),
        tags=row.get("tags", []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.delete("/{analysis_id}", response_model=DeleteAnalysisResponse)
async def delete_analysis(
    analysis_id: str,
    request: Request,
    store=Depends(_get_store),
) -> DeleteAnalysisResponse:
    """Permanently delete a saved analysis."""
    deleted = store.delete(analysis_id, owner_id=_owner_scope(request))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id!r} not found.")
    return DeleteAnalysisResponse(id=analysis_id)
