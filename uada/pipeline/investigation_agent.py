"""
Investigation Agent — 11-state async state machine for COMPLEX/VERY_COMPLEX queries.

ARCHITECTURE: ARCHITECTURE_V2 §5 — Investigation Agent State Machine.

State transition diagram:
  [*] --> OBSERVE
  OBSERVE    --> UNDERSTAND | ESCALATE
  UNDERSTAND --> PLAN       | ESCALATE
  PLAN       --> ACT        | ESCALATE
  ACT        --> OBSERVE_RESULT | RETRY | FAIL (security violation)
  RETRY      --> ACT        | REPLAN
  OBSERVE_RESULT --> ACT    | CRITIQUE
  CRITIQUE   --> RESPOND    | REPLAN | ESCALATE
  REPLAN     --> ACT        | ESCALATE
  RESPOND    --> VERIFY
  VERIFY     --> [done]     | ESCALATE

CRITICAL INVARIANT (preserved):
  All SQL execution is routed through the `sql_executor` callable supplied
  by PipelineOrchestrator, which runs SQLValidator.validate() BEFORE every
  execution attempt.  A SecurityViolation raised by the executor halts the
  investigation immediately with no retry (FAIL state).  This module never
  executes SQL directly and never bypasses the validator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent

from uada.models.investigation import (
    AgentState,
    CritiqueDecision,
    EvidenceNode,
    InvestigationBudget,
    InvestigationPlan,
    InvestigationResult,
    InvestigationState,
    InvestigationTask,
    SQLHint,
    SynthesisOutput,
    TaskType,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from uada.config import Settings

logger = logging.getLogger(__name__)

# ──────────────────────────────── System prompts ──────────────────────────────

_PLANNER_PROMPT = """\
You are the Investigation Planner for a conversational analytics system.
Given a complex analytical question, a schema summary, and the extracted intent
type, decompose the question into a minimal ordered list of SQL sub-tasks that,
together, fully answer the question.

RULES:
1. Use the fewest tasks needed — prefer 2–4 well-scoped tasks over many small ones.
2. Each task must map to exactly one independently executable SQL query.
3. Use SQL_QUERY as the task_type for all tasks unless the question explicitly
   requests statistics, forecasting, or correlation analysis.
4. sql_hint should contain draft SQL or the relevant table/column names to guide
   generation.  Leave it null when you have no useful hint.
5. Set depends_on when a task needs filter values that come from a prior task's
   results.
6. Never plan tasks that require JOINs across more than 4 tables.
7. When torn between more or fewer tasks, choose fewer.
"""

_SQL_HINT_PROMPT = """\
You are a SQL generation assistant for a conversational analytics system.
Given a task description, schema information, and summaries of prior evidence,
generate a complete, executable SQL query.

RULES:
1. Generate SELECT-only queries.  Never write INSERT, UPDATE, DELETE, DROP,
   CREATE, ALTER, TRUNCATE, EXEC, CALL, or any DDL/DML statement.
2. Always include a LIMIT clause.  Default 500; never exceed 10,000.
3. Use only table and column names explicitly mentioned in the schema summary.
4. For aggregations: COUNT, SUM, AVG, MIN, MAX only — no user-defined functions.
5. For date/time filters: use the schema's date column — never NOW(), GETDATE(),
   CURRENT_TIMESTAMP, or other server-side time functions.
6. Set confidence 0.9 when the schema mentions the exact columns needed;
   set 0.6 when you are inferring column names.
"""

_CRITIC_PROMPT = """\
You are the Evidence Critic for a conversational analytics system.
Assess whether the collected evidence is sufficient to fully answer the original
analytical question.

VERDICT values (choose exactly one):
- SUFFICIENT    — Evidence fully answers the question with acceptable confidence.
- INSUFFICIENT  — Evidence partially answers but has clear, identifiable gaps.
- CONTRADICTION — Two or more evidence pieces give conflicting factual answers.

RULES:
1. If any STATISTICS-type evidence has row_count < 30, lean toward INSUFFICIENT.
2. If any evidence node has confidence < 0.5, lean toward INSUFFICIENT.
3. If the question asks for a comparison (A vs B) and only one side is present,
   mark INSUFFICIENT and list the missing side in 'gaps'.
4. When in doubt, prefer INSUFFICIENT over SUFFICIENT (conservative).
5. In 'suggested_next_tasks', describe up to 3 natural-language tasks that would
   close the gaps — only when the verdict is INSUFFICIENT.
6. Mark contradiction_detected=true only when you see numerically incompatible
   figures for the same metric from different queries.
"""

_SYNTHESIS_PROMPT = """\
You are the final synthesizer for a conversational analytics system.
Given the original analytical question and all collected evidence summaries,
generate a clear, confident, and accurate natural-language answer.

RULES:
1. Answer in 2–5 sentences.  Be specific: cite numbers, percentages, or dates
   that appear in the evidence summaries.
2. Do not mention SQL, databases, table names, or any technical detail.
3. State caveats in the 'caveats' list if the evidence is partial or uncertain.
4. Do not invent numbers.  Only use values present in the evidence summaries.
5. If evidence is insufficient, honestly state what was found and what is unknown.
"""


class SecurityViolation(Exception):
    """Raised when the sql_executor's validator rejects the SQL (non-retriable)."""


class InvestigationAgent:
    """
    Async state machine for COMPLEX and VERY_COMPLEX analytical questions.

    Decomposes the user's question into sub-tasks, executes them through the
    validated SQL pipeline, critiques the evidence, and synthesises a final answer.

    Parameters
    ----------
    settings:
        Application settings (LLM model names, retry counts, etc.).
    sql_executor:
        Async callable ``(sql: str, description: str)`` →
        ``(normalised_sql: str, raw_result: Any)``.
        **Must** call ``SQLValidator.validate()`` before every execution attempt
        and raise ``SecurityViolation`` on any violation.  This invariant is
        enforced by ``PipelineOrchestrator._execute_sql_for_investigation()``.
    schema_summary:
        Human-readable schema description injected into LLM prompts.
        The orchestrator updates this per request via ``set_schema_summary()``.
    """

    def __init__(
        self,
        settings: "Settings",
        sql_executor: "Callable[[str, str], Awaitable[tuple[str, Any]]]",
        schema_summary: str = "",
    ) -> None:
        self._settings = settings
        self._sql_executor = sql_executor
        self._schema_summary = schema_summary

        model = settings.llm_model

        self._planner: Agent[None, InvestigationPlan] = Agent(
            model,
            output_type=InvestigationPlan,
            system_prompt=_PLANNER_PROMPT,
            retries=1,
            defer_model_check=True,
        )
        self._sql_hint_agent: Agent[None, SQLHint] = Agent(
            model,
            output_type=SQLHint,
            system_prompt=_SQL_HINT_PROMPT,
            retries=2,
            defer_model_check=True,
        )
        self._critic: Agent[None, CritiqueDecision] = Agent(
            model,
            output_type=CritiqueDecision,
            system_prompt=_CRITIC_PROMPT,
            retries=1,
            defer_model_check=True,
        )
        self._synthesiser: Agent[None, SynthesisOutput] = Agent(
            model,
            output_type=SynthesisOutput,
            system_prompt=_SYNTHESIS_PROMPT,
            retries=1,
            defer_model_check=True,
        )

    def set_schema_summary(self, summary: str) -> None:
        """Update the schema context used by all LLM prompts for this request."""
        self._schema_summary = summary

    # ─────────────────────────────── Public API ───────────────────────────────

    async def run(
        self,
        question: str,
        intent_type: str,
        complexity_tier: str,
        progress_cb: "Callable[[str, str], None] | None" = None,
    ) -> InvestigationResult:
        """
        Run the investigation state machine.

        Parameters
        ----------
        question:
            Original user question.
        intent_type:
            From ``AnalyticalIntent.question_type.value``.
        complexity_tier:
            ``"complex"`` or ``"very_complex"``; determines budget limits.
        progress_cb:
            Optional callback ``(stage_name, status)`` for SSE progress events.

        Returns
        -------
        InvestigationResult
            Final result.  Check ``synthesized_answer`` for success;
            ``escalation_reason`` / ``fail_reason`` indicate failure modes.
        """
        budget = InvestigationBudget.for_tier(complexity_tier)
        ist = InvestigationState()

        try:
            return await self._run_state_machine(
                question, intent_type, budget, ist, progress_cb
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("InvestigationAgent unhandled error: %s", exc)
            ist.current = AgentState.FAIL
            ist.fail_reason = f"Unhandled error: {exc}"
            return self._build_result(ist)

    # ──────────────────────────── State machine ───────────────────────────────

    async def _run_state_machine(
        self,
        question: str,
        intent_type: str,
        budget: InvestigationBudget,
        ist: InvestigationState,
        cb: "Callable[[str, str], None] | None",
    ) -> InvestigationResult:
        """Drive state transitions until a terminal state is reached."""

        _HANDLED = frozenset(
            [
                AgentState.OBSERVE, AgentState.UNDERSTAND, AgentState.PLAN,
                AgentState.ACT, AgentState.RETRY, AgentState.OBSERVE_RESULT,
                AgentState.CRITIQUE, AgentState.REPLAN, AgentState.RESPOND,
                AgentState.VERIFY,
            ]
        )

        def _cb(stage: str, status: str) -> None:
            if cb:
                try:
                    cb(stage, status)
                except Exception:  # noqa: BLE001
                    pass

        while ist.current not in (AgentState.ESCALATE, AgentState.FAIL):
            if budget.is_exhausted(ist):
                ist.escalation_reason = (
                    f"Budget exhausted: steps={ist.steps}, "
                    f"elapsed={ist.elapsed_seconds:.1f}s, replans={ist.replans}"
                )
                ist.current = AgentState.ESCALATE
                break

            if ist.current not in _HANDLED:
                ist.current = AgentState.FAIL
                ist.fail_reason = f"Unexpected state: {ist.current}"
                break

            state = ist.current

            if state == AgentState.OBSERVE:
                _cb("investigation_observe", "running")
                await self._state_observe(ist, question)
                _cb("investigation_observe", "done")

            elif state == AgentState.UNDERSTAND:
                _cb("investigation_understand", "running")
                await self._state_understand(ist)
                _cb("investigation_understand", "done")

            elif state == AgentState.PLAN:
                _cb("investigation_plan", "running")
                await self._state_plan(ist, question, intent_type, budget)
                _cb("investigation_plan", "done")

            elif state == AgentState.ACT:
                _cb("investigation_act", "running")
                await self._state_act(ist, budget)
                _cb("investigation_act", "done")
                ist.steps += 1

            elif state == AgentState.RETRY:
                _cb("investigation_retry", "running")
                await self._state_retry(ist, budget)
                _cb("investigation_retry", "done")

            elif state == AgentState.OBSERVE_RESULT:
                await self._state_observe_result(ist)

            elif state == AgentState.CRITIQUE:
                _cb("investigation_critique", "running")
                await self._state_critique(ist, question, budget)
                _cb("investigation_critique", "done")

            elif state == AgentState.REPLAN:
                _cb("investigation_replan", "running")
                await self._state_replan(ist, budget)
                _cb("investigation_replan", "done")
                ist.replans += 1

            elif state == AgentState.RESPOND:
                _cb("investigation_respond", "running")
                await self._state_respond(ist, question)
                _cb("investigation_respond", "done")

            elif state == AgentState.VERIFY:
                await self._state_verify(ist, budget)

        return self._build_result(ist)

    # ──────────────────────────── State handlers ──────────────────────────────

    async def _state_observe(
        self, ist: InvestigationState, question: str
    ) -> None:
        """OBSERVE: validate the question is non-trivial and can be investigated."""
        if not question or len(question.strip()) < 3:
            ist.current = AgentState.ESCALATE
            ist.escalation_reason = "Question too short or empty to investigate."
            return
        ist.current = AgentState.UNDERSTAND

    async def _state_understand(self, ist: InvestigationState) -> None:
        """UNDERSTAND: confirm schema context is available (resolved by orchestrator)."""
        if not self._schema_summary:
            logger.warning(
                "InvestigationAgent: empty schema_summary; proceeding with limited context."
            )
        ist.current = AgentState.PLAN

    async def _state_plan(
        self,
        ist: InvestigationState,
        question: str,
        intent_type: str,
        budget: InvestigationBudget,
    ) -> None:
        """PLAN: decompose the question into investigation tasks via LLM."""
        max_tasks = min(budget.max_steps // 2, 5)
        prompt = (
            f"Question: {question}\n"
            f"Intent type: {intent_type}\n"
            f"Schema: {self._schema_summary}\n"
            f"Max tasks: {max_tasks}"
        )
        try:
            result = await self._planner.run(prompt)
            plan: InvestigationPlan = result.output
            ist.pending_tasks = [
                InvestigationTask(
                    task_id=t.task_id,
                    task_type=t.task_type,
                    description=t.description,
                    sql_hint=t.sql_hint,
                    dependencies=t.depends_on,
                )
                for t in plan.tasks
            ]
            logger.info(
                "InvestigationAgent PLAN: %d tasks — %s",
                len(ist.pending_tasks),
                [t.task_id for t in ist.pending_tasks],
            )
            if ist.pending_tasks:
                ist.current = AgentState.ACT
            else:
                ist.current = AgentState.ESCALATE
                ist.escalation_reason = "Planner returned an empty task list."
        except Exception as exc:  # noqa: BLE001
            logger.warning("InvestigationAgent PLAN failed: %s", exc)
            ist.current = AgentState.ESCALATE
            ist.escalation_reason = f"Planning failed: {exc}"

    async def _state_act(
        self,
        ist: InvestigationState,
        budget: InvestigationBudget,
    ) -> None:
        """ACT: execute the next dependency-ready task via the validated executor."""
        completed_ids = {t.task_id for t in ist.completed_tasks}
        task = next(
            (
                t
                for t in ist.pending_tasks
                if not t.completed
                and not t.failed
                and all(dep in completed_ids for dep in t.dependencies)
            ),
            None,
        )

        if task is None:
            # All tasks done or dependency-blocked
            ist.current = AgentState.OBSERVE_RESULT
            return

        if ist.sql_queries >= budget.max_sql_queries:
            ist.escalation_reason = (
                f"SQL query budget exhausted ({budget.max_sql_queries} limit)."
            )
            ist.current = AgentState.ESCALATE
            return

        ist.current_task = task
        task.attempts += 1

        evidence_ctx = "\n".join(
            f"- [{e.task_id}] {e.description}: {e.summary} ({e.row_count} rows)"
            for e in ist.evidence
        ) or "No prior evidence."

        prompt = (
            f"Task: {task.description}\n"
            f"Hint: {task.sql_hint or 'none'}\n"
            f"Schema: {self._schema_summary}\n"
            f"Prior evidence:\n{evidence_ctx}"
        )

        try:
            hint_result = await self._sql_hint_agent.run(prompt)
            hint: SQLHint = hint_result.output
            logger.info(
                "InvestigationAgent ACT task=%s sql_confidence=%.2f",
                task.task_id, hint.confidence,
            )

            # CRITICAL: delegate to sql_executor — it enforces SQLValidator.
            # Never execute SQL directly in this class.
            normalised_sql, raw_result = await self._sql_executor(
                hint.sql, task.description
            )
            ist.sql_queries += 1
            ist.tool_calls += 1
            ist.current_task_result = (normalised_sql, raw_result, hint.confidence)
            ist.current = AgentState.OBSERVE_RESULT

        except SecurityViolation as exc:
            # Security violations → FAIL immediately, no retry (CRITICAL INVARIANT)
            logger.error(
                "InvestigationAgent SECURITY VIOLATION task=%s: %s",
                task.task_id, exc,
            )
            ist.current = AgentState.FAIL
            ist.fail_reason = f"Security violation in task {task.task_id}: {exc}"

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "InvestigationAgent ACT task=%s attempt=%d error: %s",
                task.task_id, task.attempts, exc,
            )
            ist.current = AgentState.RETRY

    async def _state_retry(
        self,
        ist: InvestigationState,
        budget: InvestigationBudget,
    ) -> None:
        """RETRY: retry the failed task or move to REPLAN when budget exhausted."""
        task = ist.current_task
        if task is None:
            ist.current = AgentState.REPLAN
            return

        if task.attempts >= task.max_attempts:
            logger.info(
                "InvestigationAgent RETRY task=%s exhausted after %d attempt(s).",
                task.task_id, task.attempts,
            )
            task.failed = True
            task.failure_reason = "Max retry attempts reached"
            if task in ist.pending_tasks:
                ist.pending_tasks.remove(task)
            ist.current_task = None

            has_more = any(
                not t.completed and not t.failed for t in ist.pending_tasks
            )
            ist.current = AgentState.ACT if has_more else AgentState.REPLAN
        else:
            await asyncio.sleep(budget.retry_backoff_seconds)
            ist.current = AgentState.ACT

    async def _state_observe_result(self, ist: InvestigationState) -> None:
        """OBSERVE_RESULT: collect the executed task result into the evidence list."""
        task = ist.current_task
        result_tuple = ist.current_task_result

        if task is not None and result_tuple is not None:
            normalised_sql, raw_result, confidence = result_tuple
            row_count = getattr(raw_result, "row_count", 0)
            summary = self._summarise_result(raw_result, task.description)

            ist.evidence.append(
                EvidenceNode(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    description=task.description,
                    row_count=row_count,
                    confidence=confidence,
                    summary=summary,
                    sql_executed=normalised_sql,
                )
            )
            task.completed = True
            ist.completed_tasks.append(task)
            if task in ist.pending_tasks:
                ist.pending_tasks.remove(task)

        ist.current_task = None
        ist.current_task_result = None

        remaining = [
            t for t in ist.pending_tasks if not t.completed and not t.failed
        ]
        ist.current = AgentState.ACT if remaining else AgentState.CRITIQUE

    async def _state_critique(
        self,
        ist: InvestigationState,
        question: str,
        budget: InvestigationBudget,
    ) -> None:
        """CRITIQUE: evaluate evidence sufficiency; route to RESPOND / REPLAN / ESCALATE."""
        if not ist.evidence:
            ist.current = AgentState.ESCALATE
            ist.escalation_reason = "No evidence collected — cannot answer the question."
            return

        evidence_summary = "\n".join(
            f"- [{e.task_id}] {e.description}: {e.summary} "
            f"({e.row_count} rows, confidence {e.confidence:.2f})"
            for e in ist.evidence
        )
        prompt = (
            f"Original question: {question}\n\n"
            f"Evidence collected:\n{evidence_summary}"
        )

        try:
            result = await self._critic.run(prompt)
            decision: CritiqueDecision = result.output
            logger.info(
                "InvestigationAgent CRITIQUE verdict=%s confidence=%.2f gaps=%s",
                decision.verdict, decision.confidence, decision.gaps,
            )

            if decision.contradiction_detected and budget.escalate_on_contradiction:
                ist.current = AgentState.ESCALATE
                ist.escalation_reason = (
                    f"Contradictory evidence detected: {decision.rationale}"
                )
                return

            if (
                decision.verdict == "SUFFICIENT"
                and decision.confidence >= budget.confidence_threshold
            ):
                ist.current = AgentState.RESPOND
                return

            # INSUFFICIENT — can we replan?
            if ist.replans >= budget.max_replans:
                logger.info(
                    "InvestigationAgent CRITIQUE: replan budget exhausted; proceeding to RESPOND."
                )
                ist.current = AgentState.RESPOND
                return

            # Add suggested tasks and replan
            for i, desc in enumerate(decision.suggested_next_tasks):
                ist.pending_tasks.append(
                    InvestigationTask(
                        task_id=f"replan_{ist.replans}_{i}",
                        task_type=TaskType.SQL_QUERY,
                        description=desc,
                    )
                )

            ist.current = (
                AgentState.REPLAN if decision.suggested_next_tasks else AgentState.RESPOND
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "InvestigationAgent CRITIQUE failed: %s; proceeding to RESPOND.", exc
            )
            ist.current = AgentState.RESPOND

    async def _state_replan(
        self,
        ist: InvestigationState,
        budget: InvestigationBudget,
    ) -> None:
        """REPLAN: resume execution with the new tasks added during CRITIQUE."""
        if ist.replans >= budget.max_replans:
            ist.current = AgentState.ESCALATE
            ist.escalation_reason = (
                f"Max replans ({budget.max_replans}) reached without sufficient evidence."
            )
            return
        # New pending tasks were added in CRITIQUE; resume ACT
        ist.current = AgentState.ACT

    async def _state_respond(
        self, ist: InvestigationState, question: str
    ) -> None:
        """RESPOND: synthesise a final natural-language answer from all evidence."""
        evidence_ctx = "\n".join(
            f"[{e.task_id}] {e.description}:\n  {e.summary} ({e.row_count} rows)"
            for e in ist.evidence
        ) or "No evidence available."

        prompt = f"Question: {question}\n\nEvidence:\n{evidence_ctx}"

        try:
            result = await self._synthesiser.run(prompt)
            synthesis: SynthesisOutput = result.output
            ist.synthesized_answer = synthesis.answer
            logger.info(
                "InvestigationAgent RESPOND: confidence=%.2f findings=%d caveats=%d",
                synthesis.confidence,
                len(synthesis.key_findings),
                len(synthesis.caveats),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "InvestigationAgent RESPOND synthesis failed: %s; using evidence fallback.", exc
            )
            # Fallback: concatenate evidence summaries (never raw data)
            ist.synthesized_answer = "  ".join(e.summary for e in ist.evidence[:3])

        ist.current = AgentState.VERIFY

    async def _state_verify(
        self, ist: InvestigationState, budget: InvestigationBudget
    ) -> None:
        """VERIFY: lightweight evidence-graph checks before delivering the answer."""
        if not ist.synthesized_answer:
            ist.current = AgentState.ESCALATE
            ist.escalation_reason = "Synthesis produced an empty answer."
            return

        # Warn on small-sample statistical evidence (escalate_on_low_sample = warning only,
        # not a hard failure — partial evidence is better than escalation)
        if budget.escalate_on_low_sample:
            small = [
                e
                for e in ist.evidence
                if e.task_type == TaskType.STATISTICS
                and e.row_count < budget.minimum_sample_size
            ]
            if small:
                logger.warning(
                    "InvestigationAgent VERIFY: %d STATISTICS evidence node(s) have "
                    "row_count < %d (minimum_sample_size).",
                    len(small), budget.minimum_sample_size,
                )

        # Verification passed — enter ESCALATE as the "clean-done" sentinel.
        # escalation_reason is NOT set; _build_result checks synthesized_answer
        # to distinguish clean completion from an actual escalation.
        ist.current = AgentState.ESCALATE
        ist.escalation_reason = None  # clean exit, not a real escalation

    # ─────────────────────────────── Helpers ──────────────────────────────────

    @staticmethod
    def _summarise_result(raw_result: Any, task_description: str) -> str:
        """Build a terse, LLM-safe summary of a query result.

        Raw row data is NEVER passed to the LLM — only column names, row count,
        and task context (ARCHITECTURE_V2 §20 — Security Architecture).
        """
        row_count = getattr(raw_result, "row_count", 0)
        columns = getattr(raw_result, "columns", [])
        col_names = [
            c if isinstance(c, str) else getattr(c, "name", str(c)) for c in columns
        ]
        if row_count == 0:
            return f"No rows returned for: {task_description}"
        truncated_cols = col_names[:6]
        suffix = "..." if len(col_names) > 6 else ""
        return f"{row_count} rows; columns: {', '.join(truncated_cols)}{suffix}"

    def _build_result(self, ist: InvestigationState) -> InvestigationResult:
        return InvestigationResult(
            state=ist.current,
            steps_taken=ist.steps,
            replans=ist.replans,
            elapsed_seconds=ist.elapsed_seconds,
            evidence=[
                {
                    "task_id": e.task_id,
                    "description": e.description,
                    "summary": e.summary,
                    "row_count": e.row_count,
                    "confidence": e.confidence,
                }
                for e in ist.evidence
            ],
            escalation_reason=ist.escalation_reason,
            fail_reason=ist.fail_reason,
            synthesized_answer=ist.synthesized_answer,
            confidence=ist.average_confidence(),
        )
