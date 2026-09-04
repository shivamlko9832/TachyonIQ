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

    async def generate(self, plan: QueryPlan) -> str:
        """
        Generate SQL implementing `plan`.

        Returns:
            The cleaned SQL string (markdown fences stripped).

        Raises:
            ValueError: The model returned empty output.
        """
        message = f"Query plan:\n{json.dumps(plan.to_generator_context(), indent=2)}"
        result = await self.agent.run(message)
        sql = self._clean_output(result.output)
        logger.info("SQL generated (%d chars).", len(sql))
        return sql

    async def repair(self, sql: str, error: QueryExecutionError, plan: QueryPlan) -> str:
        """
        Ask the model to fix SQL that failed during execution.

        Args:
            sql: The SQL that failed.
            error: The execution error (`repair_hint` is fed to the model).
            plan: The original QueryPlan, for context.

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
        result = await self.agent.run(message)
        repaired = self._clean_output(result.output)
        logger.info("SQL repaired (%d chars).", len(repaired))
        return repaired

    def _clean_output(self, raw: str) -> str:
        cleaned = _strip_code_fences(raw)
        if not cleaned:
            raise ValueError("SQL Generator returned empty output.")
        return cleaned
