"""
Pipeline Orchestrator (drives all 10 pipeline steps)
=======================================================
Wires every pipeline component built in Phases 1-11 into the full
question -> UADAResponse flow, with the security-critical invariant that
`SQLValidator.validate()` runs inside the execution-repair loop, before
*every* execution attempt -- never once, outside the loop, since a
repaired SQL string is new, unvalidated output from the LLM.

Per ConversationState's own docstring ("Loaded at the start of each turn
and saved at the end (even if the turn failed)"), every exit path here --
success, out-of-scope, ambiguous, security rejection, execution error,
timeout, or an unexpected exception -- appends a ConversationTurn and
saves the session. Only a *successful* turn updates `active_context`; a
failed turn is recorded for history but must not corrupt the measures/
dimensions/filters a follow-up question would otherwise inherit.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from uada.db.interface import QueryExecutionError, QueryTimeoutError
from uada.models.conversation import ActiveContext, ConversationTurn, TurnStatus
from uada.models.intent import QuestionType
from uada.models.result import ColumnMeta, PipelineStage, QueryResult, UADAError, UADAResponse

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.db.interface import DatabaseAdapter, QueryExecutionResult
    from uada.models.conversation import ConversationState
    from uada.models.intent import AnalyticalIntent, SemanticFilter, TimeRange
    from uada.models.query_plan import QueryPlan
    from uada.pipeline.conversation_store import ConversationStore
    from uada.pipeline.intent_extractor import IntentExtractor
    from uada.pipeline.query_planner import QueryPlanner
    from uada.pipeline.result_analyser import ResultAnalyser
    from uada.pipeline.schema_linker import SchemaLinker
    from uada.pipeline.sql_generator import SQLGenerator
    from uada.pipeline.sql_validator import SQLValidator, ValidationResult
    from uada.pipeline.viz_generator import VisualisationGenerator
    from uada.scl.manager import SCLManager

logger = logging.getLogger(__name__)

_ERROR_TYPE_TO_TURN_STATUS: dict[str, TurnStatus] = {
    "security_violation": TurnStatus.SECURITY_REJECTED,
    "ambiguous": TurnStatus.CLARIFICATION_REQUESTED,
}


class PipelineOrchestrator:
    """Drives one question through all 10 pipeline steps."""

    def __init__(
        self,
        db_adapter: DatabaseAdapter,
        scl_manager: SCLManager,
        schema_linker: SchemaLinker,
        intent_extractor: IntentExtractor,
        query_planner: QueryPlanner,
        sql_generator: SQLGenerator,
        result_analyser: ResultAnalyser,
        viz_generator: VisualisationGenerator,
        conversation_store: ConversationStore,
        validator: SQLValidator,
        settings: Settings,
    ) -> None:
        self._db_adapter = db_adapter
        self._scl_manager = scl_manager
        self._schema_linker = schema_linker
        self._intent_extractor = intent_extractor
        self._query_planner = query_planner
        self._sql_generator = sql_generator
        self._result_analyser = result_analyser
        self._viz_generator = viz_generator
        self._conversation_store = conversation_store
        self._validator = validator
        self._settings = settings

    @property
    def db_adapter(self) -> DatabaseAdapter:
        """The underlying database adapter. Exposed for health checks."""
        return self._db_adapter

    @property
    def conversation_store(self) -> ConversationStore:
        """The conversation session store. Exposed for session management endpoints."""
        return self._conversation_store

    async def run(self, question: str, session_id: str) -> UADAResponse:
        """Run the full pipeline for `question` and return a UADAResponse."""
        start_time = time.perf_counter()
        state = await self._conversation_store.load(session_id)
        turn_id = state.turn_count
        stage = PipelineStage.SCHEMA_LINKING
        intent: AnalyticalIntent | None = None

        try:
            context_str = state.get_edition_context()
            schema_ctx = self._schema_linker.link(question, context_str)

            stage = PipelineStage.INTENT_EXTRACTION
            intent = await self._intent_extractor.extract(question, schema_ctx, context_str)

            if intent.question_type in (QuestionType.OUT_OF_SCOPE, QuestionType.AMBIGUOUS):
                response = self._scope_error_response(intent, session_id, turn_id, start_time)
                turn = self._terminal_turn(turn_id, question, intent, response.error)
                return await self._save_and_return(state, turn, None, response)

            stage = PipelineStage.QUERY_PLANNING
            plan = self._query_planner.plan(intent, schema_ctx)

            stage = PipelineStage.SQL_GENERATION
            sql = await self._sql_generator.generate(plan)

            stage = PipelineStage.SQL_VALIDATION
            outcome = await self._validate_and_execute(
                sql, plan, session_id, turn_id, start_time
            )
            if isinstance(outcome, UADAResponse):
                turn = self._terminal_turn(turn_id, question, intent, outcome.error)
                return await self._save_and_return(state, turn, None, outcome)
            normalised_sql, raw_result = outcome

            stage = PipelineStage.RESULT_ANALYSIS
            query_result = self._to_query_result(raw_result, normalised_sql, plan)
            analysed = self._result_analyser.analyse(query_result, intent)

            stage = PipelineStage.VISUALISATION
            viz = await self._viz_generator.generate(analysed, intent)

            tables_used = [plan.primary_table.table_name] + [
                t.table_name for t in plan.additional_tables
            ]
            response = UADAResponse(
                session_id=session_id,
                turn_id=turn_id,
                timestamp=datetime.now(tz=UTC),
                answer=self._build_answer(analysed.narrative_insight, query_result.row_count),
                sql=normalised_sql,
                row_count=query_result.row_count,
                is_truncated=query_result.is_truncated,
                execution_time_ms=raw_result.execution_time_ms,
                visualisation=viz,
                key_finding=analysed.key_finding,
                question_type=intent.question_type.value,
                tables_used=tables_used,
                pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
            )
            turn = ConversationTurn(
                turn_id=turn_id,
                timestamp=response.timestamp,
                user_question=question,
                status=TurnStatus.SUCCESS,
                resolved_intent=intent,
                generated_sql=normalised_sql,
                result_summary=analysed.narrative_insight,
                tables_used=tables_used,
                active_measures=list(intent.measures),
                active_dimensions=list(intent.dimensions),
                active_filters=[self._filter_label(f) for f in intent.filters],
                chart_type=viz.chart_type.value if hasattr(viz, "chart_type") else None,
                edition_diff=(
                    intent.follow_up_description if intent.references_prior_turn else None
                ),
            )
            new_active_context = self._update_active_context(
                state.active_context, intent, plan, normalised_sql
            )
            return await self._save_and_return(state, turn, new_active_context, response)

        except Exception as exc:  # noqa: BLE001 - top-level safety net, always returns a UADAResponse
            logger.exception("Unexpected error in pipeline stage '%s'.", stage.value)
            error = UADAError(
                stage=stage,
                error_type=type(exc).__name__,
                message=str(exc),
                user_message=(
                    "Something went wrong while answering your question. Please try again."
                ),
                is_retryable=True,
            )
            response = UADAResponse(
                session_id=session_id,
                turn_id=turn_id,
                timestamp=datetime.now(tz=UTC),
                error=error,
                pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
            )
            turn = self._terminal_turn(turn_id, question, intent, error)
            return await self._save_and_return(state, turn, None, response)

    # ── SQL generation / validation / execution loop ────────────────────────

    async def _validate_and_execute(
        self,
        sql: str,
        plan: QueryPlan,
        session_id: str,
        turn_id: int,
        start_time: float,
    ) -> UADAResponse | tuple[str, QueryExecutionResult]:
        """
        Run the validate-then-execute-then-repair loop.

        Returns either a terminal UADAResponse (security rejection,
        execution error after the retry budget, or timeout) or the
        (normalised_sql, raw_result) pair on success. The security check
        runs on every iteration -- a repaired SQL string is unvalidated
        LLM output like any other, never exempted just because it's a
        second attempt.
        """
        retries = 0
        current_sql = sql
        while True:
            validation = self._validator.validate(current_sql, dialect=plan.dialect.value)
            if not validation.is_safe:
                self._log_security_incident(plan, validation)
                return self._terminal_response(
                    session_id, turn_id, start_time, PipelineStage.SQL_VALIDATION,
                    "security_violation", self._violation_message(validation),
                    self._violation_user_message(validation), is_retryable=False,
                )

            normalised_sql = validation.normalised_sql or current_sql
            try:
                raw_result = self._db_adapter.execute_query(
                    normalised_sql,
                    timeout_seconds=self._settings.db_query_timeout_seconds,
                    max_rows=self._settings.db_max_rows,
                )
                return normalised_sql, raw_result
            except QueryTimeoutError as exc:
                return self._terminal_response(
                    session_id, turn_id, start_time, PipelineStage.QUERY_EXECUTION,
                    "query_timeout", str(exc),
                    "The query took too long to run. Try narrowing the question.",
                    is_retryable=True,
                )
            except QueryExecutionError as exc:
                retries += 1
                if retries > self._settings.llm_max_retries:
                    return self._terminal_response(
                        session_id, turn_id, start_time, PipelineStage.QUERY_EXECUTION,
                        "query_execution_error",
                        f"Failed after {retries - 1} repair attempt(s): {exc}",
                        "Could not run the generated query. Please try rephrasing your question.",
                        is_retryable=True,
                    )
                logger.info("SQL execution failed; requesting repair (attempt %d).", retries)
                current_sql = await self._sql_generator.repair(normalised_sql, exc, plan)

    def _log_security_incident(self, plan: QueryPlan, validation: ValidationResult) -> None:
        # Never log SQL text or values -- violation types/details only.
        logger.warning(
            "SQL security violation on dialect '%s': %s",
            plan.dialect.value,
            [v.violation_type.value for v in validation.violations],
        )

    def _violation_message(self, validation: ValidationResult) -> str:
        violation = validation.first_violation
        return violation.detail if violation else "SQL failed security validation."

    def _violation_user_message(self, validation: ValidationResult) -> str:
        violation = validation.first_violation
        return violation.user_message if violation else "This query cannot be executed."

    def _to_query_result(
        self, raw_result: QueryExecutionResult, sql: str, plan: QueryPlan
    ) -> QueryResult:
        columns = [
            ColumnMeta(name=name, data_type=data_type)
            for name, data_type in zip(
                raw_result.column_names, raw_result.column_types, strict=True
            )
        ]
        return QueryResult(
            columns=columns,
            rows=raw_result.rows,
            row_count=raw_result.row_count,
            is_truncated=raw_result.is_truncated,
            truncated_at=self._settings.db_max_rows if raw_result.is_truncated else None,
            executed_sql=sql,
            execution_time_ms=raw_result.execution_time_ms,
            database_dialect=plan.dialect.value,
        )

    # ── Response builders ────────────────────────────────────────────────────

    def _build_answer(self, narrative_insight: str | None, row_count: int) -> str:
        if narrative_insight:
            return narrative_insight
        row_word = "row" if row_count == 1 else "rows"
        return f"Found {row_count} {row_word}."

    def _terminal_response(
        self,
        session_id: str,
        turn_id: int,
        start_time: float,
        stage: PipelineStage,
        error_type: str,
        message: str,
        user_message: str,
        *,
        is_retryable: bool,
        clarification_needed: str | None = None,
    ) -> UADAResponse:
        error = UADAError(
            stage=stage,
            error_type=error_type,
            message=message,
            user_message=user_message,
            is_retryable=is_retryable,
            clarification_needed=clarification_needed,
        )
        return UADAResponse(
            session_id=session_id,
            turn_id=turn_id,
            timestamp=datetime.now(tz=UTC),
            error=error,
            pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
        )

    def _scope_error_response(
        self, intent: AnalyticalIntent, session_id: str, turn_id: int, start_time: float
    ) -> UADAResponse:
        if intent.question_type == QuestionType.AMBIGUOUS:
            return self._terminal_response(
                session_id, turn_id, start_time, PipelineStage.INTENT_EXTRACTION,
                "ambiguous", "Intent confidence too low to proceed without clarification.",
                intent.clarification_question or "Could you clarify your question?",
                is_retryable=True, clarification_needed=intent.clarification_question,
            )
        return self._terminal_response(
            session_id, turn_id, start_time, PipelineStage.INTENT_EXTRACTION,
            "out_of_scope", "Question cannot be answered from the connected database.",
            "I can't answer that from the data I have access to.",
            is_retryable=False,
        )

    def _terminal_turn(
        self,
        turn_id: int,
        question: str,
        intent: AnalyticalIntent | None,
        error: UADAError | None,
    ) -> ConversationTurn:
        error_type = error.error_type if error else "unknown_error"
        status = _ERROR_TYPE_TO_TURN_STATUS.get(error_type, TurnStatus.ERROR)
        return ConversationTurn(
            turn_id=turn_id,
            timestamp=datetime.now(tz=UTC),
            user_question=question,
            status=status,
            resolved_intent=intent,
            error_type=error_type,
            user_facing_error=error.user_message if error else None,
            clarification_question=error.clarification_needed if error else None,
        )

    # ── Active context ───────────────────────────────────────────────────────

    def _filter_label(self, filt: SemanticFilter) -> str:
        if filt.glossary_term:
            return filt.glossary_term
        return f"{filt.entity} {filt.operator.value} {filt.value}"

    def _time_range_label(self, time_range: TimeRange | None) -> str | None:
        if time_range is None:
            return None
        if time_range.relative_period is not None:
            return time_range.relative_period.value
        if time_range.start_date and time_range.end_date:
            return f"{time_range.start_date} to {time_range.end_date}"
        return None

    def _update_active_context(
        self,
        prior: ActiveContext,
        intent: AnalyticalIntent,
        plan: QueryPlan,
        sql: str,
    ) -> ActiveContext:
        """
        A brand-new (non-follow-up) query resets accumulated context, per
        ActiveContext's own docstring ("The user can reset by starting a
        completely new query"). A follow-up merges into what's already
        active -- an empty measures/dimensions list on the new intent
        means "keep computing what we were already computing".
        """
        tables_used = [plan.primary_table.table_name] + [
            t.table_name for t in plan.additional_tables
        ]
        new_filters = [self._filter_label(f) for f in intent.filters]

        if not intent.references_prior_turn:
            return ActiveContext(
                current_measures=list(intent.measures),
                current_dimensions=list(intent.dimensions),
                accumulated_filters=new_filters,
                current_time_range=self._time_range_label(intent.time_range),
                current_tables=tables_used,
                last_successful_sql=sql,
            )

        accumulated_filters = list(prior.accumulated_filters)
        for label in new_filters:
            if label not in accumulated_filters:
                accumulated_filters.append(label)

        return ActiveContext(
            current_measures=list(intent.measures) or list(prior.current_measures),
            current_dimensions=list(intent.dimensions) or list(prior.current_dimensions),
            accumulated_filters=accumulated_filters,
            current_time_range=(
                self._time_range_label(intent.time_range) or prior.current_time_range
            ),
            current_tables=tables_used,
            last_successful_sql=sql,
        )

    # ── Persistence ──────────────────────────────────────────────────────────

    async def _save_and_return(
        self,
        state: ConversationState,
        turn: ConversationTurn,
        active_context: ActiveContext | None,
        response: UADAResponse,
    ) -> UADAResponse:
        state.turns.append(turn)
        if active_context is not None:
            state.active_context = active_context
        await self._conversation_store.save(state)
        return response
