"""
Complexity Router (Step 2 — V2 Architecture)
=============================================
Classifies an incoming analytical question into one of four tiers:
SIMPLE / ANALYTICAL / COMPLEX / VERY_COMPLEX.

Uses a two-stage approach:
1. Fast keyword heuristics for unambiguous cases — no LLM call needed.
2. PydanticAI Agent (``settings.llm_router_model``, defaults to the same
   model as the main pipeline) for everything else.

The router is intentionally lightweight and never blocks the pipeline:
any LLM failure falls back to ANALYTICAL — the safe middle-ground — so
the pipeline always continues.

Downstream uses of the tier (wired in subsequent steps):
- Step 2 (this step): tier attached to UADAResponse for observability.
- Step 3 (Investigation Agent): COMPLEX/VERY_COMPLEX engage the state machine.
- SQL generation hint: VERY_COMPLEX triggers CTE-first prompt augmentation.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pydantic_ai import Agent

from uada.models.complexity import ComplexityDecision, ComplexityTier

if TYPE_CHECKING:
    from uada.config import Settings

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are a SQL complexity classifier embedded in an analytics pipeline.

Given a natural-language analytical question and a brief schema summary,
classify the question into exactly one of four complexity tiers.

TIER DEFINITIONS
================
SIMPLE
  - Point lookups or direct row filters; single table, no aggregation.
  - No JOINs, no GROUP BY, no window functions.
  - Examples: "What was revenue for customer A?", "List orders from Jan 2024."

ANALYTICAL
  - Standard aggregations: SUM/COUNT/AVG with GROUP BY and/or ORDER BY.
  - One primary table; at most one simple JOIN.
  - No window functions, no CTEs, no multi-hop logic.
  - Examples: "Total revenue by region", "Top 5 products by units sold."

COMPLEX
  - Requires 2+ table JOINs, OR SQL window functions (RANK/LAG/LEAD/etc.),
    OR CTEs, OR multi-metric comparisons in a single pass.
  - Time comparisons that need a self-join or window (MoM, QoQ).
  - Examples: "Month-over-month growth by category", "Rank products by sales."

VERY_COMPLEX
  - Multi-hop: results of one aggregation feed into another.
  - Correlated subqueries, self-joins, complex CTE chains.
  - Year-over-year same-period comparisons, rolling aggregates, cohort analysis.
  - Examples: "Products with above-average growth Q4 vs prior year Q4."

RULES
=====
1. Classify from the QUESTION alone, not assumptions about the schema size.
2. When torn between adjacent tiers, choose the LOWER one.
3. Provide 1-3 key signals (exact words or phrases from the question).
4. Keep rationale under 250 characters.
5. Set estimated_join_count and requires_window_function accurately.
""".strip()

# ── Fast-path heuristics (skip LLM for unambiguous cases) ─────────────────────

_VERY_COMPLEX_RX: list[re.Pattern[str]] = [
    re.compile(r"\byear.?over.?year\b", re.I),
    re.compile(r"\byoy\b", re.I),
    re.compile(r"\bsame\s+period\s+(?:last|prior)\s+year\b", re.I),
    re.compile(r"\bcohort\b", re.I),
    re.compile(r"\brolling\s+\d+\b", re.I),
    re.compile(r"\babove.?average\b", re.I),
    re.compile(r"\bcompared?\s+to\s+(?:prior|previous|last)\s+(?:year|quarter|month)\b", re.I),
    re.compile(r"\bself.?join\b", re.I),
]

_SIMPLE_TRIGGERS: list[re.Pattern[str]] = [
    re.compile(r"^(?:what|show me|give me|find|get)\s+(?:is|are|the|me)?\s*", re.I),
    re.compile(r"\blist\s+all\b", re.I),
    re.compile(r"\bshow\s+me\s+(?:the\s+)?\w+\s+for\b", re.I),
]
_COMPLEX_DISQUALIFIERS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:total|sum|average|avg|mean|count|by\s+(?!date|time|id)|"
        r"group|rank|growth|trend|compare|versus|vs\.?|percent(?:age)?|"
        r"ratio|rolling|window|lag|lead|partition)\b",
        re.I,
    ),
]


def _fast_path_tier(question: str) -> ComplexityTier | None:
    """
    Return a tier immediately when keyword signals are unambiguous.
    Returns ``None`` to delegate to the LLM classifier.
    """
    # VERY_COMPLEX: multi-hop / YoY keywords are highly reliable
    if any(rx.search(question) for rx in _VERY_COMPLEX_RX):
        return ComplexityTier.VERY_COMPLEX

    # SIMPLE: short lookup phrasing with no aggregation signals
    looks_simple = any(rx.search(question) for rx in _SIMPLE_TRIGGERS)
    has_aggregation = any(rx.search(question) for rx in _COMPLEX_DISQUALIFIERS)
    if looks_simple and not has_aggregation and len(question) < 90:
        return ComplexityTier.SIMPLE

    return None  # delegate to LLM


class ComplexityRouter:
    """
    Classifies questions into SIMPLE / ANALYTICAL / COMPLEX / VERY_COMPLEX.

    Instantiate once at app startup and reuse across requests.  The
    PydanticAI agent is created lazily-on-first-use via ``defer_model_check``
    so startup is never blocked by model availability.
    """

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        # Use the dedicated router model if configured, else fall back to the
        # primary LLM model (same quality, still lightweight classification).
        router_model = settings.llm_router_model or settings.llm_model
        self._agent: Agent[None, ComplexityDecision] = Agent(
            router_model,
            output_type=ComplexityDecision,
            system_prompt=_SYSTEM_PROMPT,
            retries=1,           # single retry — fast path must stay fast
            defer_model_check=True,
        )

    async def classify(
        self,
        question: str,
        schema_summary: str,
        intent_type: str = "ANALYTICAL_QUERY",
    ) -> ComplexityDecision:
        """
        Classify *question* into a :class:`ComplexityTier`.

        Parameters
        ----------
        question:
            The raw natural-language question from the user.
        schema_summary:
            Brief description of relevant tables/columns (e.g.
            ``"orders(12 cols), customers(8 cols)"``).
        intent_type:
            The QuestionType value from IntentExtractor (for context).

        Returns
        -------
        ComplexityDecision
            Always a valid decision; never raises.  ANALYTICAL is the
            safe fallback when the LLM is unavailable or fails.
        """
        # Stage 1: fast keyword heuristics
        fast_tier = _fast_path_tier(question)
        if fast_tier is not None:
            logger.debug(
                "ComplexityRouter fast-path → %s | q=%r",
                fast_tier.value, question[:80],
            )
            return ComplexityDecision(
                tier=fast_tier,
                rationale=f"Keyword heuristic match for '{fast_tier.value}'.",
                key_signals=["heuristic"],
                estimated_join_count=0 if fast_tier == ComplexityTier.SIMPLE else 1,
                requires_window_function=fast_tier == ComplexityTier.VERY_COMPLEX,
            )

        # Stage 2: LLM classification
        prompt = (
            f"QUESTION: {question}\n"
            f"INTENT_TYPE: {intent_type}\n"
            f"SCHEMA CONTEXT: {schema_summary}"
        )
        try:
            result = await self._agent.run(prompt)
            decision = result.output
            logger.info(
                "ComplexityRouter: tier=%s joins=%d window=%s | q=%r",
                decision.tier.value,
                decision.estimated_join_count,
                decision.requires_window_function,
                question[:80],
            )
            return decision
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ComplexityRouter LLM failed (fallback=ANALYTICAL): %s", exc
            )
            return ComplexityDecision(
                tier=ComplexityTier.ANALYTICAL,
                rationale="LLM classifier unavailable; defaulting to ANALYTICAL.",
                key_signals=["fallback"],
                estimated_join_count=0,
                requires_window_function=False,
            )
