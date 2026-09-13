"""
LLM accuracy evaluation: runs the full pipeline against
tests/evaluation/datasets/eval_dataset_format.jsonl and measures intent
accuracy, SQL accuracy, and latency, plus an answer-relevancy check via
DeepEval's LLM-as-judge metric.

Requires a live LLM connection (settings.llm_model) and a real,
onboarded client database -- this is NOT part of the default test run.
pyproject.toml's `addopts = "-m 'not llm and not evaluation'"` excludes
it; run it explicitly once your .env points at real infrastructure:

    pytest tests/evaluation/run_accuracy_eval.py -m evaluation -v

DeepEval's AnswerRelevancyMetric uses its own LLM judge (OpenAI by
default; pass `model=` for a different one) independent of
settings.llm_model, so test_answer_relevancy additionally needs an
OpenAI-compatible judge configured.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from uada.models.intent import AnalyticalIntent
    from uada.pipeline.orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.evaluation

DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "eval_dataset_format.jsonl"

_INTENT_ACCURACY_THRESHOLD = 0.8
_SQL_ACCURACY_THRESHOLD = 0.8


@dataclass
class EvalCase:
    """One row of eval_dataset_format.jsonl."""

    id: str
    question: str
    expected_question_type: str
    expected_measures: list[str] = field(default_factory=list)
    expected_dimensions: list[str] = field(default_factory=list)
    expected_sql_contains: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class CaseResult:
    case_id: str
    question: str
    answer: str | None
    intent_correct: bool
    sql_correct: bool
    latency_ms: float
    error: str | None = None


def load_cases(path: Path = DATASET_PATH) -> list[EvalCase]:
    """Load the JSONL accuracy eval dataset."""
    return [
        EvalCase(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def intent_matches(intent: AnalyticalIntent, case: EvalCase) -> bool:
    """Whether the extracted intent matches `case`'s expectations."""
    if intent.question_type.value != case.expected_question_type:
        return False
    if case.expected_measures and set(intent.measures) != set(case.expected_measures):
        return False
    return not (
        case.expected_dimensions and set(intent.dimensions) != set(case.expected_dimensions)
    )


def sql_matches(sql: str | None, case: EvalCase) -> bool:
    """Whether the generated SQL contains every fragment `case` expects."""
    if sql is None:
        return not case.expected_sql_contains
    return all(fragment in sql for fragment in case.expected_sql_contains)


async def run_case(orchestrator: PipelineOrchestrator, case: EvalCase) -> CaseResult:
    """Run one case through the full pipeline and score it."""
    session_id = f"eval-{case.id}"
    start = time.perf_counter()
    response = await orchestrator.run(case.question, session_id)
    latency_ms = (time.perf_counter() - start) * 1000

    state = await orchestrator.conversation_store.load(session_id)
    resolved_intent = state.turns[-1].resolved_intent if state.turns else None
    out_of_scope_match = (
        case.expected_question_type == "out_of_scope"
        and response.error is not None
        and response.error.error_type == "out_of_scope"
    )

    return CaseResult(
        case_id=case.id,
        question=case.question,
        answer=response.answer,
        intent_correct=out_of_scope_match
        or (resolved_intent is not None and intent_matches(resolved_intent, case)),
        sql_correct=sql_matches(response.sql, case),
        latency_ms=latency_ms,
        error=response.error.message if response.error else None,
    )


@pytest.fixture(scope="module")
def orchestrator() -> PipelineOrchestrator:
    """
    The real pipeline, wired from live Settings (.env) -- the same
    bootstrap the API uses in production. Building it here requires a
    resolvable UADA_DB_URL/UADA_SCL_PATH and, at run() time, a reachable
    settings.llm_model.
    """
    from uada.api.app import _bootstrap_orchestrator
    from uada.config import settings

    return _bootstrap_orchestrator(settings)


async def test_accuracy_eval(orchestrator: PipelineOrchestrator) -> None:
    """Intent accuracy, SQL accuracy, and latency across the full dataset."""
    cases = load_cases()
    results = [await run_case(orchestrator, case) for case in cases]

    intent_accuracy = sum(r.intent_correct for r in results) / len(results)
    sql_accuracy = sum(r.sql_correct for r in results) / len(results)
    avg_latency_ms = sum(r.latency_ms for r in results) / len(results)

    intent_correct_count = sum(r.intent_correct for r in results)
    sql_correct_count = sum(r.sql_correct for r in results)
    logger.info(
        "Intent accuracy: %.1f%% (%d/%d)", intent_accuracy * 100, intent_correct_count, len(results)
    )
    logger.info(
        "SQL accuracy: %.1f%% (%d/%d)", sql_accuracy * 100, sql_correct_count, len(results)
    )
    logger.info("Average latency: %.0fms", avg_latency_ms)
    for result in results:
        if not (result.intent_correct and result.sql_correct):
            logger.info(
                "  [%s] intent_correct=%s sql_correct=%s error=%s",
                result.case_id,
                result.intent_correct,
                result.sql_correct,
                result.error,
            )

    assert intent_accuracy >= _INTENT_ACCURACY_THRESHOLD, (
        f"Intent accuracy {intent_accuracy:.0%} below {_INTENT_ACCURACY_THRESHOLD:.0%} threshold."
    )
    assert sql_accuracy >= _SQL_ACCURACY_THRESHOLD, (
        f"SQL accuracy {sql_accuracy:.0%} below {_SQL_ACCURACY_THRESHOLD:.0%} threshold."
    )


async def test_answer_relevancy(orchestrator: PipelineOrchestrator) -> None:
    """
    LLM-as-judge check, via DeepEval, that each answer actually addresses
    its question. Separate from test_accuracy_eval since it needs its own
    judge model configured (OpenAI by default; see module docstring).
    """
    from deepeval import assert_test
    from deepeval.metrics import AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase

    metric = AnswerRelevancyMetric(threshold=0.7)
    cases = [case for case in load_cases() if case.expected_question_type != "out_of_scope"]

    for case in cases:
        result = await run_case(orchestrator, case)
        if result.answer is None:
            pytest.fail(f"[{case.id}] Pipeline returned no answer: {result.error}")
        assert_test(LLMTestCase(input=case.question, actual_output=result.answer), [metric])
