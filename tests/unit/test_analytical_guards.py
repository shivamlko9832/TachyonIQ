"""Regression tests for semantic and investigation correctness gates."""

from uada.models.intent import (
    AnalyticalIntent,
    QuestionType,
    RelativePeriod,
    TimeComparison,
    TimeRange,
    TimeRangeType,
)
from uada.models.investigation import InvestigationPlan, PlannedTask, TaskType
from uada.pipeline.investigation_agent import InvestigationAgent
from uada.pipeline.orchestrator import PipelineOrchestrator


def _comparison_intent(
    *,
    time_range: TimeRange | None = None,
    time_comparison: TimeComparison | None = None,
    question_type: QuestionType = QuestionType.COMPARISON,
) -> AnalyticalIntent:
    return AnalyticalIntent(
        question_type=question_type,
        measures=["revenue"],
        time_range=time_range,
        time_comparison=time_comparison,
        references_prior_turn=question_type == QuestionType.FOLLOW_UP_EXTEND,
        raw_question="Compare this to last period",
    )


def test_incomplete_time_comparison_requires_clarification() -> None:
    intent = _comparison_intent(
        time_range=TimeRange(
            range_type=TimeRangeType.RELATIVE,
            relative_period=RelativePeriod.THIS_MONTH,
        )
    )

    clarification = PipelineOrchestrator._comparison_clarification(
        intent, "Compare this to last period"
    )

    assert clarification is not None
    assert "Which two periods" in clarification


def test_complete_time_comparison_can_execute() -> None:
    intent = _comparison_intent(
        time_range=TimeRange(
            range_type=TimeRangeType.RELATIVE,
            relative_period=RelativePeriod.THIS_MONTH,
        ),
        time_comparison=TimeComparison(
            comparison_period=RelativePeriod.LAST_MONTH,
            comparison_label="Last month",
        ),
    )

    clarification = PipelineOrchestrator._comparison_clarification(
        intent, "This month vs last month"
    )

    assert clarification is None


def test_segment_comparison_does_not_require_time_windows() -> None:
    intent = _comparison_intent()

    assert PipelineOrchestrator._comparison_clarification(intent, "Compare regions") is None


def test_follow_up_time_comparison_is_also_guarded() -> None:
    intent = _comparison_intent(question_type=QuestionType.FOLLOW_UP_EXTEND)

    clarification = PipelineOrchestrator._comparison_clarification(
        intent, "Compare it with last year"
    )

    assert clarification is not None


def _task(task_id: str, *dependencies: str) -> PlannedTask:
    return PlannedTask(
        task_id=task_id,
        task_type=TaskType.SQL_QUERY,
        description=f"Run {task_id}",
        depends_on=list(dependencies),
    )


def test_investigation_plan_rejects_unknown_dependency() -> None:
    plan = InvestigationPlan(tasks=[_task("t1", "missing")], rationale="test")

    error = InvestigationAgent._validate_plan(plan)

    assert error is not None
    assert "unknown dependencies" in error


def test_investigation_plan_rejects_dependency_cycle() -> None:
    plan = InvestigationPlan(
        tasks=[_task("t1", "t2"), _task("t2", "t1")],
        rationale="test",
    )

    assert InvestigationAgent._validate_plan(plan) == (
        "Investigation plan contains a dependency cycle."
    )


def test_investigation_plan_accepts_valid_dag() -> None:
    plan = InvestigationPlan(
        tasks=[_task("t1"), _task("t2", "t1")],
        rationale="test",
    )

    assert InvestigationAgent._validate_plan(plan) is None
