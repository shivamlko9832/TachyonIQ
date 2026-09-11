"""
SQL Generator (pipeline step 5)
==================================
LLM. Converts a fully resolved `QueryPlan` into a SQL string.

The plan is self-contained (metric formulas, join conditions, and time
filters are all pre-expanded by the Query Planner), so this stage's only
job is assembling correct SQL syntax from what the plan already
specifies -- it must never improvise formulas, filters, or tables.

The output is plain SQL text, not a validated Pydantic model: the actual
security boundary is `SQLValidator` (step 6), which runs on whatever this
stage produces before it is ever executed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from pydantic_ai import Agent

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.db.interface import QueryExecutionError
    from uada.models.query_plan import QueryPlan

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are an expert SQL generator for analytical queries.

You receive a structured query plan and must produce a single, valid SQL
SELECT statement that implements it exactly.

Rules:
- Output ONLY the SQL query. No explanation, no markdown, no code fences.
- Use the exact dialect specified in the plan.
- Use fully qualified column references (table.column) to avoid ambiguity.
- Apply the metric formula from the plan exactly -- do not improvise.
- Apply the time filter from the plan exactly -- use the provided SQL fragment.
- Include all joins specified in the plan.
- Include GROUP BY for all non-aggregated columns in SELECT.
- Apply LIMIT if specified. If no LIMIT in the plan, do not add one.
- Use aliases exactly as specified (measure.output_alias, dimension.output_alias).
- For comparison queries, use UNION ALL with period labels.
- Never use subqueries beyond what the plan specifies.
- Never reference tables not in the plan.
""".strip()

# ── CTE-first augmentation ────────────────────────────────────────────────────
# Appended to the user message for complex and very_complex queries.
# Complex multi-join or multi-aggregation SQL is significantly easier to
# validate, repair, and audit when intermediate steps are named CTEs rather
# than nested subqueries.  This guidance does NOT override SYSTEM_PROMPT;
# it adds structural preference on top of it.
_CTE_AUGMENT = """
Structural guidance for this complex query:
- Prefer CTEs (WITH clauses) over nested subqueries for every intermediate step.
- Each CTE should represent exactly one logical operation: a filter, a join
  denormalisation, an aggregation, or a window calculation.
- Name CTEs descriptively after their logical role, e.g. filtered_orders,
  revenue_by_region, ranked_customers.
- The final SELECT reads from the last CTE (or joins several CTEs together)
  rather than referencing raw tables directly where a CTE already computed
  the intermediate result.
- Preferred structure:
    WITH
      step1 AS (
        -- first logical operation
      ),
      step2 AS (
        -- second logical operation, may reference step1
      )
    SELECT ...
    FROM   step_n
    ...
- This structural guidance supplements the plan rules above; all plan
  constraints (exact dialect, aliases, metric formulas, time filters) still
  apply to each CTE and the final SELECT.
""".strip()

# Tiers from either ComplexityRouter or QueryPlanner that warrant CTE structure.
_CTE_TIERS = frozenset({"complex", "very_complex"})


def _needs_cte(plan_complexity: str, caller_tier: str | None) -> bool:
    """Return True when CTE-first augmentation should be added to the prompt.

    Args:
        plan_complexity: ``QueryPlan.estimated_complexity`` — the planner's
            structural estimate of the SQL's inherent complexity
            (``"simple"`` | ``"moderate"`` | ``"complex"``).
        caller_tier: The ComplexityRouter tier supplied by the orchestrator
            (``"simple"`` | ``"analytical"`` | ``"complex"`` |
            ``"very_complex"`` | ``None`` when the router is not wired).

    Returns:
        ``True`` when either source signals a complex query.
    """
    return plan_complexity in _CTE_TIERS or caller_tier in _CTE_TIERS


# ─────────────────────────────────────────────────────────────────────────────

_CODE_FENCE_PATTERN = re.compile(r"^```(?:sql)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    """Strip a ```sql ... ``` or ``` ... ``` wrapper, if the text is wrapped in one."""
    stripped = text.strip()
    match = _CODE_FENCE_PATTERN.match(stripped)
    return match.group(1).strip() if match else stripped


class SQLGenerator:
    """Pipeline step 5: QueryPlan -> SQL string, via an LLM."""

    def __init__(self, settings: Settings) -> None:
        """
        Build the PydanticAI agent for `settings.llm_model`.

        `defer_model_check=True` for the same reason as IntentExtractor:
        constructing Agent(model=settings.llm_model, ...) eagerly resolves
        the model string, and an "ollama:..." model raises immediately
        without OLLAMA_BASE_URL set -- which it never is in tests/CI.
        """
        self._settings = settings
        self.agent: Agent[None, str] = Agent(
            settings.llm_model,
            output_type=str,
            system_prompt=SYSTEM_PROMPT,
            retries=settings.llm_max_retries,
            defer_model_check=True,
        )

    async def generate(
        self,
        plan: QueryPlan,
        *,
        complexity_tier: str | None = None,
    ) -> str:
        """Generate SQL implementing *plan*.

        Args:
            plan: The fully resolved query plan from QueryPlanner.
            complexity_tier: Optional ComplexityRouter tier string
                (``"simple"`` | ``"analytical"`` | ``"complex"`` |
                ``"very_complex"``).  When supplied alongside or instead of
                ``plan.estimated_complexity``, it is used by :func:`_needs_cte`
                to decide whether to append the CTE-first structural guidance.

        Returns:
            The cleaned SQL string (markdown fences stripped).

        Raises:
            ValueError: The model returned empty output.
        """
        message = f"Query plan:\n{json.dumps(plan.to_generator_context(), indent=2)}"
        if _needs_cte(plan.estimated_complexity, complexity_tier):
            message = f"{message}\n\n{_CTE_AUGMENT}"
            logger.debug(
                "CTE-first augmentation applied (plan_complexity=%r, tier=%r).",
                plan.estimated_complexity,
                complexity_tier,
            )
        result = await self.agent.run(message)
        sql = self._clean_output(result.output)
        logger.info("SQL generated (%d chars).", len(sql))
        return sql

    async def repair(
        self,
        sql: str,
        error: QueryExecutionError,
        plan: QueryPlan,
        *,
        complexity_tier: str | None = None,
    ) -> str:
        """Ask the model to fix SQL that failed during execution.

        Args:
            sql: The SQL that failed.
            error: The execution error (``repair_hint`` is fed to the model).
            plan: The original QueryPlan, for context.
            complexity_tier: Optional ComplexityRouter tier — forwarded from
                the orchestrator so that repaired SQL also follows CTE
                structure when the original generation did.

        Returns:
            The cleaned, corrected SQL string.

        Raises:
            ValueError: The model returned empty output.
        """
        message = (
            f"The following SQL failed with error: {error.repair_hint}\n\n"
            f"Failed SQL:\n{sql}\n\n"
            f"Original plan:\n{json.dumps(plan.to_generator_context(), indent=2)}\n\n"
            "Fix the SQL to resolve the error. Output only the corrected SQL."
        )
        if _needs_cte(plan.estimated_complexity, complexity_tier):
            message = f"{message}\n\n{_CTE_AUGMENT}"
            logger.debug(
                "CTE-first augmentation applied to repair (plan_complexity=%r, tier=%r).",
                plan.estimated_complexity,
                complexity_tier,
            )
        result = await self.agent.run(message)
        repaired = self._clean_output(result.output)
        logger.info("SQL repaired (%d chars).", len(repaired))
        return repaired

    def _clean_output(self, raw: str) -> str:
        cleaned = _strip_code_fences(raw)
        if not cleaned:
            raise ValueError("SQL Generator returned empty output.")
        return cleaned
