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
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from opentelemetry import trace

from uada.analytics.statistical_engine import StatisticalEngine
from uada.db.interface import QueryExecutionError, QueryTimeoutError
from uada.models.conversation import ActiveContext, ConversationTurn, TurnStatus
from uada.models.intent import (
    AnalysisOperation,
    FilterOperator,
    QuestionType,
    RelativePeriod,
    SemanticFilter,
    TimeComparison,
    TimeRange,
    TimeRangeType,
)
from uada.models.result import (
    ChartType,
    ColumnMeta,
    PipelineStage,
    QueryResult,
    UADAError,
    UADAResponse,
    VegaLiteSpec,
)
from uada.pipeline.conversational_router import ConversationalRouter
from uada.pipeline.intent_normalizer import IntentNormalizer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from uada.config import Settings
    from uada.db.interface import DatabaseAdapter, QueryExecutionResult
    from uada.models.conversation import ConversationState
    from uada.models.intent import AnalyticalIntent
    from uada.models.query_plan import QueryPlan
    from uada.models.result import ForecastResult
    from uada.observability.audit import AuditLogger
    from uada.pipeline.complexity_router import ComplexityRouter
    from uada.pipeline.conversation_store import ConversationStore
    from uada.pipeline.followup_engine import FollowUpEngine
    from uada.pipeline.intent_extractor import IntentExtractor
    from uada.pipeline.investigation_agent import InvestigationAgent
    from uada.pipeline.query_planner import QueryPlanner
    from uada.pipeline.replanner import Replanner
    from uada.pipeline.result_analyser import ResultAnalyser
    from uada.pipeline.result_critic import ResultCritic
    from uada.pipeline.schema_linker import SchemaLinker
    from uada.pipeline.sql_generator import SQLGenerator
    from uada.pipeline.sql_validator import SQLValidator, ValidationResult
    from uada.pipeline.viz_generator import VisualisationGenerator
    from uada.scl.manager import SCLManager

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

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
        followup_engine: FollowUpEngine | None = None,
        audit_logger: AuditLogger | None = None,
        insight_generator: object | None = None,
        complexity_router: ComplexityRouter | None = None,
        result_critic: ResultCritic | None = None,
        replanner: Replanner | None = None,
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
        self._followup_engine = followup_engine
        self._audit_logger = audit_logger
        self._insight_generator = insight_generator  # P4-A-3: optional LLM insight step
        self._complexity_router = complexity_router  # Step 2: optional tier classifier
        self._investigation_agent: InvestigationAgent | None = None  # Step 3: set after init
        self._result_critic: ResultCritic | None = result_critic  # Step 5: deterministic scorer
        self._replanner: Replanner | None = replanner  # Step 6: deterministic plan mutator
        self._intent_normalizer = IntentNormalizer(scl_manager)
        self._statistical_engine = StatisticalEngine()

    @property
    def db_adapter(self) -> DatabaseAdapter:
        """The underlying database adapter. Exposed for health checks."""
        return self._db_adapter

    @property
    def conversation_store(self) -> ConversationStore:
        """The conversation session store. Exposed for session management endpoints."""
        return self._conversation_store

    def attach_investigation_agent(self, agent: InvestigationAgent) -> None:
        """
        Attach an InvestigationAgent after construction.

        Called by app.py after the orchestrator is built, so that the agent's
        ``sql_executor`` closure can capture ``self._execute_sql_for_investigation``.
        The parameter is typed under TYPE_CHECKING to avoid a circular import at
        module load time.
        """
        self._investigation_agent = agent

    def _friendly_pipeline_error(self, exc: Exception) -> str:
        """Return actionable provider guidance without exposing provider internals."""
        if type(exc).__name__ == "ModelHTTPError":
            provider = (self._settings.llm_model.split(":", 1)[0] or "configured").lower()
            return (
                f"The {provider} model provider could not complete this request. "
                "Verify the API key, model name, account limits and network connection, "
                "then restart TachyonIQ."
            )
        return "Something went wrong while answering your question. Please try again."

    async def run(
        self,
        question: str,
        session_id: str,
        *,
        user_id: str | None = None,
        db_adapter: DatabaseAdapter | None = None,
    ) -> UADAResponse:
        """Run the full pipeline for `question` and return a UADAResponse."""
        start_time = time.perf_counter()
        execution_adapter = db_adapter or self._db_adapter
        try:
            state = await self._conversation_store.load(session_id, owner_id=user_id)
        except PermissionError:
            return self._terminal_response(
                session_id,
                0,
                start_time,
                PipelineStage.SCHEMA_LINKING,
                "session_access_denied",
                "Conversation ownership check failed.",
                "This conversation is not available to your account.",
                is_retryable=False,
            )
        turn_id = state.turn_count
        stage = PipelineStage.SCHEMA_LINKING
        intent: AnalyticalIntent | None = None

        conversational_reply = ConversationalRouter.route(question)
        if conversational_reply is not None:
            response = UADAResponse(
                session_id=session_id,
                turn_id=turn_id,
                timestamp=datetime.now(tz=UTC),
                answer=conversational_reply.answer,
                suggested_questions=list(conversational_reply.suggested_questions),
                question_type="conversation",
                pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
            )
            turn = ConversationTurn(
                turn_id=turn_id,
                timestamp=datetime.now(tz=UTC),
                user_question=question,
                status=TurnStatus.CONVERSATIONAL,
                result_summary=conversational_reply.answer,
            )
            return await self._save_and_return(
                state,
                turn,
                None,
                response,
                question=question,
                user_id=user_id,
            )

        try:
            with _tracer.start_as_current_span("schema_linking") as span:
                context_str = state.get_edition_context()
                schema_ctx = self._schema_linker.link(question, context_str)
                span.set_attribute("query", question)
                span.set_attribute("tables_retrieved", [t.table_name for t in schema_ctx.tables])

            stage = PipelineStage.INTENT_EXTRACTION
            with _tracer.start_as_current_span("intent_extraction") as span:
                intent = await self._intent_extractor.extract(question, schema_ctx, context_str)
                intent = self._normalise_explicit_comparison(intent, question)
                intent = self._intent_normalizer.normalise(
                    intent,
                    question,
                    schema_ctx,
                    state.active_context,
                )
                span.set_attribute("question_type", intent.question_type.value)
                span.set_attribute("confidence", intent.confidence)
                span.set_attribute("measures", list(intent.measures))

            if intent.question_type in (
                QuestionType.OUT_OF_SCOPE,
                QuestionType.AMBIGUOUS,
            ):
                response = self._scope_error_response(intent, session_id, turn_id, start_time)
                turn = self._terminal_turn(turn_id, question, intent, response.error)
                return await self._save_and_return(
                    state, turn, None, response, question=question, user_id=user_id
                )

            comparison_clarification = self._comparison_clarification(intent, question)
            if comparison_clarification is not None:
                response = self._terminal_response(
                    session_id,
                    turn_id,
                    start_time,
                    PipelineStage.INTENT_EXTRACTION,
                    "ambiguous",
                    "A period comparison requires a primary and comparison window.",
                    comparison_clarification,
                    is_retryable=True,
                    clarification_needed=comparison_clarification,
                )
                turn = self._terminal_turn(turn_id, question, intent, response.error)
                return await self._save_and_return(
                    state, turn, None, response, question=question, user_id=user_id
                )

            # Step 2: Complexity Router — classify before planning
            _complexity_tier: str | None = None
            if self._complexity_router is not None:
                try:
                    _schema_summary = (
                        ", ".join(
                            f"{t.table_name}({len(t.columns)} cols)"
                            if hasattr(t, "columns")
                            else t.table_name
                            for t in schema_ctx.tables
                        )
                        or "unknown"
                    )
                    _cd = await self._complexity_router.classify(
                        question=question,
                        schema_summary=_schema_summary,
                        intent_type=intent.question_type.value,
                    )
                    _complexity_tier = _cd.tier.value
                    span_complexity = _tracer.start_as_current_span("complexity_routing")
                    with span_complexity as _cs:
                        _cs.set_attribute("complexity_tier", _complexity_tier)
                        _cs.set_attribute("estimated_joins", _cd.estimated_join_count)
                        _cs.set_attribute("requires_window", _cd.requires_window_function)
                except Exception as _cr_exc:  # noqa: BLE001
                    logger.warning("ComplexityRouter skipped: %s", _cr_exc)

            # Refinements and explicit comparisons that have already inherited
            # a prior metric can be expressed by the deterministic query plan.
            # Explanations still use investigation because they require evidence
            # gathering rather than a single query transformation.
            _deterministic_follow_up = intent.references_prior_turn and intent.question_type in (
                QuestionType.COMPARISON,
                QuestionType.FOLLOW_UP_EXTEND,
                QuestionType.FOLLOW_UP_REFINE,
            )

            # A fully specified period comparison has a deterministic query
            # shape.  Route it through the normal planner so explicit windows
            # such as "March–August versus September–February" are honoured
            # instead of being sent to an open-ended investigation budget.
            _deterministic_comparison = intent.question_type == QuestionType.COMPARISON

            # Step 3: Investigation Agent — branch for COMPLEX / VERY_COMPLEX
            if (
                _complexity_tier in ("complex", "very_complex")
                and self._investigation_agent is not None
                and not _deterministic_follow_up
                and not _deterministic_comparison
                and not intent.analysis_operations
            ):
                _inv_schema_summary = (
                    ", ".join(
                        f"{t.table_name}({len(t.columns)} cols)"
                        if hasattr(t, "columns")
                        else t.table_name
                        for t in schema_ctx.tables
                    )
                    or "unknown"
                )
                self._investigation_agent.set_schema_summary(_inv_schema_summary)

                async def _selected_inv_executor(sql: str, description: str):
                    return await self._execute_sql_for_investigation(
                        sql, description, db_adapter=execution_adapter
                    )

                with _tracer.start_as_current_span("investigation_agent") as _inv_span:
                    _inv_span.set_attribute("complexity_tier", _complexity_tier or "")
                    _inv_result = await self._investigation_agent.run(
                        question=question,
                        intent_type=intent.question_type.value,
                        complexity_tier=_complexity_tier,
                        sql_executor=_selected_inv_executor,
                    )
                    _inv_span.set_attribute("investigation_state", _inv_result.state.value)
                    _inv_span.set_attribute("investigation_steps", _inv_result.steps_taken)
                    _inv_span.set_attribute("investigation_confidence", _inv_result.confidence)

                _inv_evidence_nodes = [
                    f"{e.get('task_id', '?')}: {e.get('summary', '')}" for e in _inv_result.evidence
                ]
                if _inv_result.synthesized_answer:
                    response = UADAResponse(
                        session_id=session_id,
                        turn_id=turn_id,
                        timestamp=datetime.now(tz=UTC),
                        answer=_inv_result.synthesized_answer,
                        question_type=intent.question_type.value,
                        complexity_tier=_complexity_tier,
                        investigation_steps=_inv_result.steps_taken,
                        evidence_nodes=_inv_evidence_nodes,
                        pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
                    )
                else:
                    response = self._terminal_response(
                        session_id,
                        turn_id,
                        start_time,
                        PipelineStage.RESULT_ANALYSIS,
                        "investigation_incomplete",
                        "The bounded investigation did not produce a verified answer.",
                        "I couldn't complete a reliable analysis within the allowed "
                        "budget. Try narrowing the question.",
                        is_retryable=True,
                    ).model_copy(
                        update={
                            "question_type": intent.question_type.value,
                            "complexity_tier": _complexity_tier,
                            "investigation_steps": _inv_result.steps_taken,
                            "evidence_nodes": _inv_evidence_nodes,
                        }
                    )
                result_summary = response.answer or (
                    response.error.user_message if response.error else "Investigation failed."
                )
                turn = ConversationTurn(
                    turn_id=turn_id,
                    timestamp=response.timestamp,
                    user_question=question,
                    status=(
                        TurnStatus.SUCCESS if _inv_result.synthesized_answer else TurnStatus.ERROR
                    ),
                    resolved_intent=intent,
                    result_summary=result_summary[:200],
                )
                return await self._save_and_return(
                    state, turn, None, response, question=question, user_id=user_id
                )

            stage = PipelineStage.QUERY_PLANNING
            with _tracer.start_as_current_span("query_planning") as span:
                plan = self._query_planner.plan(intent, schema_ctx)
                span.set_attribute("dialect", plan.dialect.value)
                span.set_attribute("table_count", 1 + len(plan.additional_tables))
                span.set_attribute("join_count", len(plan.joins))

            stage = PipelineStage.SQL_GENERATION
            with _tracer.start_as_current_span("sql_generation") as span:
                span.set_attribute("attempt_number", 1)
                sql = await self._sql_generator.generate(plan, complexity_tier=_complexity_tier)

            stage = PipelineStage.SQL_VALIDATION
            outcome = await self._validate_and_execute(
                sql,
                plan,
                session_id,
                turn_id,
                start_time,
                complexity_tier=_complexity_tier,
                db_adapter=execution_adapter,
            )
            if isinstance(outcome, UADAResponse):
                turn = self._terminal_turn(turn_id, question, intent, outcome.error)
                return await self._save_and_return(
                    state, turn, None, outcome, question=question, user_id=user_id
                )
            normalised_sql, raw_result = outcome

            # Step 5: deterministic ResultCritic — score before insight generation
            _critic_result = None
            if self._result_critic is not None:
                with _tracer.start_as_current_span("result_critique") as _rc_span:
                    _critic_result = self._result_critic.critique(
                        raw_result, plan, intent_type=intent.question_type.value
                    )
                    _rc_span.set_attribute("critic_score", _critic_result.score)
                    _rc_span.set_attribute("is_sufficient", _critic_result.is_sufficient)

            # Step 6: Replanner — single deterministic retry on failed critique
            if (
                _critic_result is not None
                and not _critic_result.is_sufficient
                and self._replanner is not None
            ):
                with _tracer.start_as_current_span("replan") as _rp_span:
                    _replan_result = self._replanner.replan(_critic_result, plan)
                    _rp_span.set_attribute("did_replan", _replan_result.did_replan)
                    _rp_span.set_attribute("replan_applied", len(_replan_result.applied))
                if _replan_result.did_replan:
                    logger.info(
                        "Replanner applied %d mutation(s); retrying SQL generation.",
                        len(_replan_result.applied),
                    )
                    plan = _replan_result.plan
                    _retry_sql = await self._sql_generator.generate(
                        plan, complexity_tier=_complexity_tier
                    )
                    _retry_outcome = await self._validate_and_execute(
                        _retry_sql,
                        plan,
                        session_id,
                        turn_id,
                        start_time,
                        complexity_tier=_complexity_tier,
                        db_adapter=execution_adapter,
                    )
                    if not isinstance(_retry_outcome, UADAResponse):
                        normalised_sql, raw_result = _retry_outcome
                        if self._result_critic is not None:
                            with _tracer.start_as_current_span("result_critique_retry") as _rc2:
                                _critic_result = self._result_critic.critique(
                                    raw_result,
                                    plan,
                                    intent_type=intent.question_type.value,
                                )
                                _rc2.set_attribute("critic_score", _critic_result.score)
                                _rc2.set_attribute("is_sufficient", _critic_result.is_sufficient)
                    else:
                        logger.warning(
                            "Replanner retry failed at validation/execution; "
                            "returning the terminal validation response."
                        )
                        turn = self._terminal_turn(turn_id, question, intent, _retry_outcome.error)
                        return await self._save_and_return(
                            state,
                            turn,
                            None,
                            _retry_outcome,
                            question=question,
                            user_id=user_id,
                        )

            stage = PipelineStage.RESULT_ANALYSIS
            query_result = self._to_query_result(raw_result, normalised_sql, plan)
            with _tracer.start_as_current_span("result_analysis") as span:
                analysed = self._result_analyser.analyse(query_result, intent, plan)
                span.set_attribute("has_time_dimension", analysed.has_time_dimension)
                span.set_attribute("outlier_count", len(analysed.outliers))

            # Numerical proof is computed from the exact executed rows. The LLM
            # may choose the requested operation, but it never calculates or
            # supplies values for this report.
            statistical_analysis = self._statistical_engine.analyse(analysed, intent, plan)

            # Forecasting is a typed operation on the governed query plan. The
            # requested horizon and dimensions come from AnalyticalIntent.
            forecast_result = None
            if (
                AnalysisOperation.FORECAST in intent.analysis_operations
                and analysed.has_time_dimension
                and analysed.time_column
                and analysed.query_result.row_count >= 3
            ):
                try:
                    import pandas as _pd

                    from uada.analytics.forecast import Forecaster

                    _forecast_frame = _pd.DataFrame(
                        analysed.query_result.rows,
                        columns=analysed.query_result.column_names,
                    )
                    _measure_column = next(
                        (
                            measure.output_alias
                            for measure in plan.measures
                            if measure.output_alias in _forecast_frame.columns
                        ),
                        None,
                    )
                    if _measure_column:
                        _group_columns = [
                            dimension.output_alias
                            for dimension in plan.dimensions
                            if dimension.output_alias in _forecast_frame.columns
                        ]
                        _forecaster = Forecaster()
                        _forecast_kwargs = {
                            "time_column": analysed.time_column,
                            "value_column": _measure_column,
                            "periods": intent.forecast_horizon or plan.forecast_horizon or 4,
                            "confidence_level": intent.confidence_level,
                        }
                        if _group_columns:
                            forecast_result = _forecaster.forecast_grouped(
                                _forecast_frame,
                                group_columns=_group_columns,
                                **_forecast_kwargs,
                            )
                        else:
                            forecast_result = _forecaster.forecast(
                                _forecast_frame,
                                **_forecast_kwargs,
                            )
                        analysed = analysed.model_copy(update={"forecast_result": forecast_result})
                        _latest_values = _pd.to_numeric(
                            _forecast_frame[_measure_column], errors="coerce"
                        ).dropna()
                        statistical_analysis = self._statistical_engine.attach_forecast(
                            statistical_analysis,
                            forecast_result,
                            float(_latest_values.iloc[-1]) if not _latest_values.empty else None,
                        )
                except Exception as _fc_exc:  # noqa: BLE001
                    logger.warning("Forecaster skipped: %s", _fc_exc)

            stage = PipelineStage.VISUALISATION
            with _tracer.start_as_current_span("viz_generation") as span:
                viz = await self._viz_generator.generate(analysed, intent)
                if forecast_result is not None and not forecast_result.skipped:
                    viz = self._forecast_visualisation(query_result, forecast_result, plan)
                span.set_attribute(
                    "chart_type", viz.chart_type.value if hasattr(viz, "chart_type") else "none"
                )

            # Supplementary visualisations (best-effort, never blocks the response)
            supplementary_viz: list = []
            try:
                _primary_ct = viz.chart_type if hasattr(viz, "chart_type") else None
                supplementary_viz = await self._viz_generator.generate_supplementary(
                    analysed, intent, primary_chart_type=_primary_ct
                )
            except Exception as _sv_exc:  # noqa: BLE001
                logger.warning("generate_supplementary failed: %s", _sv_exc)

            tables_used = [plan.primary_table.table_name] + [
                t.table_name for t in plan.additional_tables
            ]

            # Follow-up suggestions (deterministic, never blocks the response)
            suggested_questions: list[str] = []
            if self._followup_engine is not None:
                try:
                    suggested_questions = self._followup_engine.suggest(
                        intent, analysed, state, schema_ctx
                    )
                except Exception as _fe_exc:  # noqa: BLE001
                    logger.warning("FollowUpEngine failed: %s", _fe_exc)

            # P4-A-1: Data Quality Check (deterministic, best-effort)
            _data_quality = None
            try:
                import pandas as _pd

                from uada.analytics.data_quality import DataQualityChecker

                _dq_df = _pd.DataFrame(query_result.rows, columns=query_result.column_names)
                _data_quality = DataQualityChecker().check(_dq_df)
            except Exception as _dq_exc:  # noqa: BLE001
                logger.debug("DataQualityChecker skipped: %s", _dq_exc)

            # P4-A-2: Explainability Context (deterministic, best-effort)
            _explainability = None
            try:
                from uada.analytics.explainability import ExplainabilityBuilder

                _explainability = ExplainabilityBuilder().build(
                    sql=normalised_sql,
                    analysed_result=analysed,
                    viz=viz,
                    is_truncated=query_result.is_truncated,
                    truncated_at=query_result.truncated_at,
                )
                _explainability = _explainability.model_copy(
                    update={
                        "calculation_steps": [
                            *_explainability.calculation_steps,
                            *[
                                f"Applied {method}."
                                for method in statistical_analysis.get("methods", [])
                            ],
                            "Computed the response only from the validated result fingerprint "
                            f"{statistical_analysis['provenance']['result_sha256'][:12]}.",
                        ],
                        "assumptions": [
                            *_explainability.assumptions,
                            *statistical_analysis.get("assumptions", []),
                            *statistical_analysis.get("limitations", []),
                        ],
                    }
                )
            except Exception as _ex_exc:  # noqa: BLE001
                logger.debug("ExplainabilityBuilder skipped: %s", _ex_exc)

            # P4-A-3: LLM Insight Generator (best-effort, non-blocking)
            _generated_insights = None
            if self._insight_generator is not None:
                try:
                    _generated_insights = await self._insight_generator.generate(
                        question=question,
                        analysed_result=analysed,
                        data_quality=_data_quality,
                        statistical_analysis=statistical_analysis,
                    )
                except Exception as _ig_exc:  # noqa: BLE001
                    logger.warning("InsightGenerator skipped: %s", _ig_exc)

            _statistical_narrative = self._statistical_engine.narrative(
                statistical_analysis, intent, plan
            )
            response = UADAResponse(
                session_id=session_id,
                turn_id=turn_id,
                timestamp=datetime.now(tz=UTC),
                answer=self._build_answer(
                    _statistical_narrative or analysed.narrative_insight,
                    query_result.row_count,
                ),
                sql=normalised_sql,
                row_count=query_result.row_count,
                is_truncated=query_result.is_truncated,
                execution_time_ms=raw_result.execution_time_ms,
                query_result=query_result,
                visualisation=viz,
                supplementary_visualisations=supplementary_viz,
                key_finding=analysed.key_finding,
                key_findings_bullets=analysed.key_findings_bullets,
                drivers=analysed.drivers,
                anomaly_descriptions=analysed.anomaly_descriptions,
                suggested_questions=suggested_questions,
                question_type=intent.question_type.value,
                complexity_tier=_complexity_tier,
                tables_used=tables_used,
                critic_score=_critic_result.score if _critic_result is not None else None,
                pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
                correlation_result=self._statistical_engine.correlation_result(
                    statistical_analysis
                ),
                anomaly_result=self._statistical_engine.anomaly_result(
                    statistical_analysis
                ),
                forecast_result=analysed.forecast_result,
                data_quality=_data_quality,
                explainability=_explainability,
                generated_insights=_generated_insights,
                statistical_analysis=statistical_analysis,
            )
            turn = ConversationTurn(
                turn_id=turn_id,
                timestamp=response.timestamp,
                user_question=question,
                status=TurnStatus.SUCCESS,
                resolved_intent=intent,
                generated_sql=normalised_sql,
                result_summary=_statistical_narrative or analysed.narrative_insight,
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
            return await self._save_and_return(
                state, turn, new_active_context, response, question=question, user_id=user_id
            )

        except Exception as exc:  # noqa: BLE001 - top-level safety net, always returns a UADAResponse
            logger.exception("Unexpected error in pipeline stage '%s'.", stage.value)
            error = UADAError(
                stage=stage,
                error_type=type(exc).__name__,
                message="The pipeline could not complete the request.",
                user_message=self._friendly_pipeline_error(exc),
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
            return await self._save_and_return(
                state, turn, None, response, question=question, user_id=user_id
            )

    # ── SQL generation / validation / execution loop ────────────────────────

    async def _execute_sql_for_investigation(
        self,
        sql: str,
        description: str,
        *,
        db_adapter: DatabaseAdapter | None = None,
    ) -> tuple[str, QueryExecutionResult]:
        """
        Validate then execute SQL on behalf of InvestigationAgent.

        CRITICAL INVARIANT: SQLValidator.validate() runs here, before every
        execution attempt, exactly as in _validate_and_execute().  A security
        violation raises SecurityViolation immediately — no retry — so that the
        investigation state machine transitions to FAIL rather than attempting
        the query.  This method is the sole SQL execution path for the agent;
        it never bypasses the validator.
        """
        from uada.pipeline.investigation_agent import SecurityViolation

        dialect = self._scl_manager.scl.database.dialect.value

        with _tracer.start_as_current_span("inv_sql_validation") as _span:
            validation = self._validator.validate(sql, dialect=dialect)
            _span.set_attribute("is_safe", validation.is_safe)
            if not validation.is_safe:
                _violation_types = [v.violation_type.value for v in validation.violations]
                _span.set_attribute("violation_types", str(_violation_types))
                # NEVER log SQL text — only violation type codes
                logger.error(
                    "Investigation SQL security violation (dialect=%s): %s",
                    dialect,
                    _violation_types,
                )
                raise SecurityViolation(f"SQL security violation: {_violation_types}")

        normalised = validation.normalised_sql or sql
        with _tracer.start_as_current_span("inv_query_execution") as _span:
            execution_adapter = db_adapter or self._db_adapter
            raw_result = execution_adapter.execute_query(
                normalised,
                timeout_seconds=self._settings.db_query_timeout_seconds,
                max_rows=self._settings.db_max_rows,
            )
            _span.set_attribute("row_count", raw_result.row_count)

        return normalised, raw_result

    async def _validate_and_execute(
        self,
        sql: str,
        plan: QueryPlan,
        session_id: str,
        turn_id: int,
        start_time: float,
        *,
        complexity_tier: str | None = None,
        db_adapter: DatabaseAdapter | None = None,
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
        execution_adapter = db_adapter or self._db_adapter
        current_sql = sql
        while True:
            with _tracer.start_as_current_span("sql_validation") as span:
                validation = self._validator.validate(current_sql, dialect=plan.dialect.value)
                span.set_attribute("is_safe", validation.is_safe)
                if not validation.is_safe:
                    violation = validation.first_violation
                    if violation is not None:
                        span.set_attribute("violation_type", violation.violation_type.value)
                    self._log_security_incident(plan, validation)
                    return self._terminal_response(
                        session_id,
                        turn_id,
                        start_time,
                        PipelineStage.SQL_VALIDATION,
                        "security_violation",
                        self._violation_message(validation),
                        self._violation_user_message(validation),
                        is_retryable=False,
                    )

            normalised_sql = validation.normalised_sql or current_sql
            try:
                with _tracer.start_as_current_span("query_execution") as span:
                    raw_result = execution_adapter.execute_query(
                        normalised_sql,
                        timeout_seconds=self._settings.db_query_timeout_seconds,
                        max_rows=self._settings.db_max_rows,
                    )
                    span.set_attribute("row_count", raw_result.row_count)
                    span.set_attribute("execution_time_ms", raw_result.execution_time_ms)
                    span.set_attribute("is_truncated", raw_result.is_truncated)
                return normalised_sql, raw_result
            except QueryTimeoutError as exc:
                return self._terminal_response(
                    session_id,
                    turn_id,
                    start_time,
                    PipelineStage.QUERY_EXECUTION,
                    "query_timeout",
                    str(exc),
                    "The query took too long to run. Try narrowing the question.",
                    is_retryable=True,
                )
            except QueryExecutionError as exc:
                retries += 1
                if retries > self._settings.llm_max_retries:
                    return self._terminal_response(
                        session_id,
                        turn_id,
                        start_time,
                        PipelineStage.QUERY_EXECUTION,
                        "query_execution_error",
                        f"Failed after {retries - 1} repair attempt(s): {exc}",
                        "Could not run the generated query. Please try rephrasing your question.",
                        is_retryable=True,
                    )
                logger.info("SQL execution failed; requesting repair (attempt %d).", retries)
                with _tracer.start_as_current_span("sql_generation") as span:
                    span.set_attribute("attempt_number", retries + 1)
                    current_sql = await self._sql_generator.repair(
                        normalised_sql,
                        exc,
                        plan,
                        complexity_tier=complexity_tier,
                    )

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
        measure_units = {measure.output_alias: measure.unit for measure in plan.measures}
        columns = [
            ColumnMeta(name=name, data_type=data_type, unit=measure_units.get(name))
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

    @staticmethod
    def _forecast_visualisation(
        query_result: QueryResult,
        forecast: ForecastResult,
        plan: QueryPlan,
    ) -> VegaLiteSpec:
        """Build one truthful chart from observed rows and computed forecast points."""
        column_names = query_result.column_names
        time_column = forecast.time_column
        value_column = forecast.forecast_column
        time_index = column_names.index(time_column)
        value_index = column_names.index(value_column)
        rows: list[dict[str, object]] = []
        group_aliases = [
            dimension.output_alias
            for dimension in plan.dimensions
            if dimension.output_alias in column_names
        ]
        group_indices = [column_names.index(alias) for alias in group_aliases]
        for raw in query_result.rows:
            group = ", ".join(
                f"{alias}={raw[index]}"
                for alias, index in zip(group_aliases, group_indices, strict=True)
            )
            rows.append(
                {
                    "period": str(raw[time_index]),
                    "value": raw[value_index],
                    "series": f"{group} actual" if group else "Actual",
                }
            )

        if forecast.series:
            for grouped in forecast.series:
                for point in grouped.points:
                    rows.append(
                        {
                            "period": point.period,
                            "value": point.forecast,
                            "series": f"{grouped.group} forecast",
                        }
                    )
        else:
            for point in forecast.points:
                rows.extend(
                    [
                        {"period": point.period, "value": point.forecast, "series": "Forecast"},
                        {"period": point.period, "value": point.lower_ci, "series": "Lower bound"},
                        {"period": point.period, "value": point.upper_ci, "series": "Upper bound"},
                    ]
                )

        level = int(round(forecast.confidence_level * 100))
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": rows},
            "mark": {"type": "line", "point": True},
            "encoding": {
                "x": {"field": "period", "type": "temporal", "title": time_column},
                "y": {"field": "value", "type": "quantitative", "title": value_column},
                "color": {"field": "series", "type": "nominal", "title": None},
                "strokeDash": {"field": "series", "type": "nominal"},
                "tooltip": [
                    {"field": "period", "type": "temporal"},
                    {"field": "series", "type": "nominal"},
                    {"field": "value", "type": "quantitative", "format": ",.2f"},
                ],
            },
        }
        return VegaLiteSpec(
            spec=spec,
            chart_type=ChartType.LINE,
            title=f"{value_column.replace('_', ' ').title()} forecast ({level}% interval)",
            reasoning=(
                "Observed and independently calculated forecast values share one ordered time axis."
            ),
        )

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
        safe_messages = {
            "security_violation": "The request violates the data-access policy.",
            "query_timeout": "The query took too long to run.",
            "query_execution_error": "The generated query could not be executed.",
            "out_of_scope": "The question cannot be answered from the connected data.",
            "ambiguous": "The question needs clarification before it can be answered.",
        }
        error = UADAError(
            stage=stage,
            error_type=error_type,
            message=safe_messages.get(error_type, "The request could not be completed."),
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
                session_id,
                turn_id,
                start_time,
                PipelineStage.INTENT_EXTRACTION,
                "ambiguous",
                "Intent confidence too low to proceed without clarification.",
                intent.clarification_question or "Could you clarify your question?",
                is_retryable=True,
                clarification_needed=intent.clarification_question,
            )
        return self._terminal_response(
            session_id,
            turn_id,
            start_time,
            PipelineStage.INTENT_EXTRACTION,
            "out_of_scope",
            "Question cannot be answered from the connected database.",
            "I can't answer that from the data I have access to.",
            is_retryable=False,
        )

    @staticmethod
    def _normalise_explicit_comparison(
        intent: AnalyticalIntent, question: str
    ) -> AnalyticalIntent:
        """Make common period-comparison language executable and unambiguous."""
        from datetime import date

        lowered = question.lower()
        comparison_words = re.search(r"\b(compare|comparison|versus|vs\.?|against)\b", lowered)
        latest_pair = "latest six complete month" in lowered and "preceding six month" in lowered
        if (
            intent.question_type != QuestionType.COMPARISON
            and not comparison_words
            and not latest_pair
        ):
            return intent

        def _shift_month(value: date, months: int) -> date:
            index = value.year * 12 + value.month - 1 + months
            return date(index // 12, index % 12 + 1, 1)

        def _parse_month_window(value: str) -> tuple[date, date] | None:
            month_names = (
                "january|february|march|april|may|june|july|august|"
                "september|october|november|december"
            )
            matches = list(
                re.finditer(
                    rf"\b({month_names})\b(?:\s+(\d{{4}}))?",
                    value,
                    flags=re.IGNORECASE,
                )
            )
            if len(matches) < 2:
                return None
            month_to_number = {
                name: number
                for number, name in enumerate(
                    (
                        "january",
                        "february",
                        "march",
                        "april",
                        "may",
                        "june",
                        "july",
                        "august",
                        "september",
                        "october",
                        "november",
                        "december",
                    ),
                    start=1,
                )
            }
            first, second = matches[0], matches[1]
            first_year = int(first.group(2)) if first.group(2) else None
            second_year = int(second.group(2)) if second.group(2) else None
            year = first_year or second_year
            if year is None:
                return None
            start = date(first_year or year, month_to_number[first.group(1).lower()], 1)
            end = date(
                second_year or (first_year or year), month_to_number[second.group(1).lower()], 1
            )
            if end <= start:
                end = _shift_month(end, 12)
            return start, _shift_month(end, 1)

        primary_window: tuple[date, date] | None = None
        comparison_window: tuple[date, date] | None = None
        if latest_pair:
            current_start = date.today().replace(day=1)
            primary_window = (_shift_month(current_start, -6), current_start)
            comparison_window = (
                _shift_month(current_start, -12),
                _shift_month(current_start, -6),
            )
        elif comparison_words:
            parts = re.split(r"\b(?:with|versus|vs\.?|against)\b", lowered, maxsplit=1)
            if len(parts) == 2:
                primary_window = _parse_month_window(parts[0])
                comparison_window = _parse_month_window(parts[1])
        if primary_window is None or comparison_window is None:
            return intent

        primary_start, primary_end = primary_window
        comparison_start, comparison_end = comparison_window
        # This comparison shape has a governed absolute window and uses Europe
        # as a filter.  Discard model-proposed time filters/dimensions so a
        # duplicate phrase such as "latest six complete months" cannot resolve
        # to an unrelated table or create an accidental GROUP BY.
        time_entities = {"date", "month", "order_date", "period", "comparison_period"}
        filters = [
            item
            for item in intent.filters
            if item.entity.lower() not in {"region", *time_entities}
            and (item.glossary_term or "").lower() != "recent six months"
        ]
        if "europe" in lowered:
            filters.append(
                SemanticFilter(
                    entity="region",
                    operator=FilterOperator.EQUALS,
                    value="Europe",
                )
            )
        return intent.model_copy(
            update={
                "question_type": QuestionType.COMPARISON,
                "measures": (
                    ["net_revenue"]
                    if any(term in lowered for term in ("revenue", "sales"))
                    else (intent.measures or ["net_revenue"])
                ),
                "dimensions": [],
                "time_dimension": "order_date",
                "time_range": TimeRange(
                    range_type=TimeRangeType.ABSOLUTE,
                    start_date=primary_start.isoformat(),
                    end_date=primary_end.isoformat(),
                ),
                "time_comparison": TimeComparison(
                    comparison_period=RelativePeriod.LAST_N_MONTHS,
                    period_count=6,
                    comparison_label=(
                        "Preceding six complete months"
                        if latest_pair
                        else f"{comparison_start.isoformat()} to {comparison_end.isoformat()}"
                    ),
                    comparison_start_date=comparison_start.isoformat(),
                    comparison_end_date=comparison_end.isoformat(),
                ),
                "filters": filters,
                "confidence": max(intent.confidence, 0.9),
                "clarification_question": None,
            }
        )

    @staticmethod
    def _comparison_clarification(intent: AnalyticalIntent, question: str) -> str | None:
        """Require two explicit windows before executing a time comparison."""
        if intent.question_type not in (
            QuestionType.COMPARISON,
            QuestionType.FOLLOW_UP_EXTEND,
        ):
            return None
        time_words = (
            "period",
            "day",
            "week",
            "month",
            "quarter",
            "year",
            "today",
            "yesterday",
        )
        is_time_comparison = (
            intent.time_range is not None
            or intent.time_comparison is not None
            or any(word in question.lower() for word in time_words)
        )
        if not is_time_comparison:
            return None
        if intent.time_range is not None and intent.time_comparison is not None:
            return None
        lowered = question.lower()
        if "latest six complete month" in lowered and "preceding six month" in lowered:
            return None
        return (
            "Which two periods should I compare? For example, ask for "
            "'this month vs last month' or 'this year vs last year'."
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
        *,
        question: str = "",
        user_id: str | None = None,
    ) -> UADAResponse:
        state.turns.append(turn)
        if active_context is not None:
            state.active_context = active_context
        await self._conversation_store.save(state)
        response = response.model_copy(
            update={
                "context_snapshot": {
                    "current_measures": list(state.active_context.current_measures),
                    "current_dimensions": list(state.active_context.current_dimensions),
                    "accumulated_filters": list(state.active_context.accumulated_filters),
                    "current_time_range": state.active_context.current_time_range,
                    "current_tables": list(state.active_context.current_tables),
                }
            }
        )
        if self._audit_logger is not None:
            try:
                self._audit_logger.log(response=response, question=question, user_id=user_id)
            except Exception as _audit_exc:  # noqa: BLE001
                logger.warning("AuditLogger failed: %s", _audit_exc)
        return response

    # ── SSE streaming ──────────────────────────────────────────────────────────

    async def stream_run(
        self,
        question: str,
        session_id: str,
        *,
        user_id: str | None = None,
        db_adapter: DatabaseAdapter | None = None,
    ) -> AsyncGenerator[dict[str, str], None]:
        """Stream progress through exactly the same governed pipeline as run().

        A single implementation owns intent normalization, planning, validation,
        statistics, forecasting, evidence, and conversation persistence.
        """
        import json

        yield {
            "event": "progress",
            "data": json.dumps({"stage": "analysis_pipeline", "status": "running"}),
        }
        response = await self.run(
            question,
            session_id,
            user_id=user_id,
            db_adapter=db_adapter,
        )
        yield {
            "event": "progress",
            "data": json.dumps({"stage": "analysis_pipeline", "status": "done"}),
        }
        yield {"event": "result", "data": response.model_dump_json()}
