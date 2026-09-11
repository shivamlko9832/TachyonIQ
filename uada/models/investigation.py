"""
Investigation Agent data models.

Defines the state machine, budget controls, and evidence structures used by
InvestigationAgent for COMPLEX and VERY_COMPLEX queries (ARCHITECTURE_V2 §5).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentState(str, Enum):
    """11-state investigation state machine (ARCHITECTURE_V2 §5.1)."""

    OBSERVE = "observe"
    UNDERSTAND = "understand"
    PLAN = "plan"
    ACT = "act"
    RETRY = "retry"
    OBSERVE_RESULT = "observe_result"
    CRITIQUE = "critique"
    REPLAN = "replan"
    RESPOND = "respond"
    VERIFY = "verify"
    ESCALATE = "escalate"
    FAIL = "fail"

    @property
    def is_terminal(self) -> bool:
        return self in (AgentState.ESCALATE, AgentState.FAIL)


class TaskType(str, Enum):
    SQL_QUERY = "sql_query"
    STATISTICS = "statistics"
    FORECAST = "forecast"
    CORRELATION = "correlation"


@dataclass
class InvestigationTask:
    """A single unit of work within an investigation."""

    task_id: str
    task_type: TaskType
    description: str
    sql_hint: str | None = None
    dependencies: list[str] = field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 2
    completed: bool = False
    failed: bool = False
    failure_reason: str | None = None


@dataclass
class EvidenceNode:
    """One piece of evidence collected during investigation."""

    task_id: str
    task_type: TaskType
    description: str
    row_count: int
    confidence: float  # 0.0–1.0
    summary: str  # Terse LLM-safe description (never raw data)
    sql_executed: str | None = None


@dataclass
class InvestigationBudget:
    """Hard and soft limits (ARCHITECTURE_V2 §5.2)."""

    # Hard limits — never exceeded
    max_steps: int = 8
    timeout_seconds: float = 30.0
    budget_tokens: int = 50_000
    max_tool_calls: int = 20
    max_sql_queries: int = 10
    max_replans: int = 3

    # Soft limits — trigger warnings / escalation consideration
    confidence_threshold: float = 0.7
    evidence_sufficiency: float = 0.8

    # Retry config
    max_retries_per_tool: int = 2
    retry_backoff_seconds: float = 0.5

    # Escalation conditions
    escalate_on_contradiction: bool = True
    escalate_on_causal_claims: bool = True
    escalate_on_low_sample: bool = True
    minimum_sample_size: int = 30

    @classmethod
    def for_tier(cls, tier: str) -> "InvestigationBudget":
        """Return a budget calibrated for the given complexity tier."""
        if tier == "very_complex":
            return cls(
                max_steps=20,
                timeout_seconds=120.0,
                budget_tokens=150_000,
                max_tool_calls=40,
                max_sql_queries=20,
                max_replans=3,
            )
        # COMPLEX defaults
        return cls()

    def is_exhausted(self, state: "InvestigationState") -> bool:
        return (
            state.steps >= self.max_steps
            or state.elapsed_seconds >= self.timeout_seconds
            or state.tokens_used >= self.budget_tokens
            or state.replans >= self.max_replans
        )


@dataclass
class InvestigationState:
    """Mutable runtime state tracked across state machine transitions."""

    current: AgentState = AgentState.OBSERVE
    steps: int = 0
    replans: int = 0
    tool_calls: int = 0
    sql_queries: int = 0
    tokens_used: int = 0
    started_at: float = field(default_factory=time.monotonic)
    evidence: list[EvidenceNode] = field(default_factory=list)
    pending_tasks: list[InvestigationTask] = field(default_factory=list)
    completed_tasks: list[InvestigationTask] = field(default_factory=list)
    current_task: InvestigationTask | None = None
    current_task_result: Any = None  # (normalised_sql, raw_result, confidence)
    escalation_reason: str | None = None
    fail_reason: str | None = None
    synthesized_answer: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def average_confidence(self) -> float:
        if not self.evidence:
            return 0.0
        return sum(e.confidence for e in self.evidence) / len(self.evidence)


class InvestigationResult(BaseModel):
    """Final output from InvestigationAgent, consumed by PipelineOrchestrator."""

    state: AgentState
    steps_taken: int
    replans: int
    elapsed_seconds: float
    evidence: list[dict] = Field(default_factory=list)
    escalation_reason: str | None = None
    fail_reason: str | None = None
    synthesized_answer: str | None = None
    confidence: float = 0.0


# ──────────────────────── PydanticAI LLM output models ────────────────────────


class PlannedTask(BaseModel):
    """One task in the investigation plan returned by the Planner LLM."""

    task_id: str = Field(description="Short unique ID e.g. 't1'")
    task_type: TaskType = Field(description="Type of analytical task")
    description: str = Field(
        description="What this task should determine", max_length=200
    )
    sql_hint: str | None = Field(
        default=None,
        description="Draft SQL or table/column names to query, if known",
    )
    depends_on: list[str] = Field(
        default_factory=list, description="task_ids this task depends on"
    )


class InvestigationPlan(BaseModel):
    """Output from the Planner LLM (PLAN state)."""

    tasks: list[PlannedTask] = Field(min_length=1, max_length=8)
    rationale: str = Field(
        description="Why this decomposition answers the question", max_length=300
    )


class SQLHint(BaseModel):
    """Output from the SQL-hint generator (ACT state)."""

    sql: str = Field(description="Complete, executable SQL query")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence that this SQL is correct"
    )
    explanation: str = Field(
        description="What this query returns in one sentence", max_length=200
    )


class CritiqueDecision(BaseModel):
    """Output from the Critic LLM (CRITIQUE state)."""

    verdict: str = Field(
        description="One of: SUFFICIENT | INSUFFICIENT | CONTRADICTION"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    gaps: list[str] = Field(default_factory=list, max_length=5)
    contradiction_detected: bool = False
    rationale: str = Field(max_length=300)
    suggested_next_tasks: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Natural-language descriptions of new tasks (when INSUFFICIENT)",
    )


class SynthesisOutput(BaseModel):
    """Output from the synthesis LLM (RESPOND state)."""

    answer: str = Field(
        description="Clear natural-language answer to the original question"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    key_findings: list[str] = Field(default_factory=list, max_length=5)
    caveats: list[str] = Field(default_factory=list, max_length=3)
