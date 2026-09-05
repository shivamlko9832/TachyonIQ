"""
Explainability Builder (P4-A-2, GAP-18)
=========================================
Deterministic. Constructs an ExplainabilityContext from a QueryPlan and
AnalysedResult without any LLM call.

Extracts:
  - SQL breakdown (tables, filters, aggregations, GROUP BY dimensions)
  - Calculation steps in plain English
  - Assumptions (NULL handling, truncation, time-range filters)
  - Chart rationale (why a particular chart type was selected)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from uada.models.result import ExplainabilityContext

if TYPE_CHECKING:
    from uada.models.result import AnalysedResult, VegaLiteSpec, VisualisationFallback
    from uada.models.query_plan import QueryPlan  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)

# Regex patterns for SQL parsing (case-insensitive)
_RE_FROM = re.compile(
    r"\bFROM\s+([\w\"\`\.]+(?:\s*,\s*[\w\"\`\.]+)*)", re.IGNORECASE
)
_RE_JOIN = re.compile(
    r"\bJOIN\s+([\w\"\`\.]+)", re.IGNORECASE
)
_RE_WHERE = re.compile(
    r"\bWHERE\b(.+?)(?:\bGROUP\b|\bHAVING\b|\bORDER\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_RE_HAVING = re.compile(
    r"\bHAVING\b(.+?)(?:\bORDER\b|\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)
_RE_GROUP_BY = re.compile(
    r"\bGROUP\s+BY\s+(.+?)(?:\bHAVING\b|\bORDER\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_RE_ORDER_BY = re.compile(
    r"\bORDER\s+BY\s+(.+?)(?:\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)
_RE_LIMIT = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
_RE_AGG = re.compile(
    r"\b(SUM|COUNT|AVG|MIN|MAX|MEDIAN|STDDEV|VARIANCE|COUNT DISTINCT|COUNT\s*\(\s*DISTINCT)\s*\(",
    re.IGNORECASE,
)


def _clean(s: str) -> str:
    """Strip extra whitespace and trailing punctuation from a SQL fragment."""
    return re.sub(r"\s+", " ", s).strip().rstrip(",;")


class ExplainabilityBuilder:
    """
    Builds an :class:`~uada.models.result.ExplainabilityContext` from a
    parsed QueryPlan and an AnalysedResult.

    Usage::

        ctx = ExplainabilityBuilder().build(query_plan, analysed_result, viz)
    """

    def build(
        self,
        sql: str,
        analysed_result: "AnalysedResult",
        viz: "VegaLiteSpec | VisualisationFallback | None" = None,
        is_truncated: bool = False,
        truncated_at: int | None = None,
    ) -> ExplainabilityContext:
        """
        Build an ExplainabilityContext from the executed SQL and results.

        Parameters
        ----------
        sql:
            The exact SQL string that was executed.
        analysed_result:
            Post-analysis result (for numeric summaries, row count etc.)
        viz:
            Primary visualisation, if any (used to derive chart rationale).
        is_truncated:
            True if the result was capped at a row limit.
        truncated_at:
            The row limit that caused truncation.
        """
        tables = self._extract_tables(sql)
        filters = self._extract_filters(sql)
        aggregations = self._extract_aggregations(sql)
        group_by = self._extract_group_by(sql)
        order_by = self._extract_order_by(sql)
        limit = self._extract_limit(sql)

        assumptions = self._build_assumptions(
            aggregations=aggregations,
            is_truncated=is_truncated,
            truncated_at=truncated_at,
            limit=limit,
            sql=sql,
        )

        calculation_steps = self._build_calculation_steps(
            tables=tables,
            filters=filters,
            group_by=group_by,
            aggregations=aggregations,
            order_by=order_by,
            limit=limit,
        )

        sql_breakdown = self._build_sql_breakdown(
            tables=tables,
            filters=filters,
            group_by=group_by,
            aggregations=aggregations,
        )

        chart_rationale = self._build_chart_rationale(viz, analysed_result)

        return ExplainabilityContext(
            sql_breakdown=sql_breakdown,
            tables_referenced=tables,
            filters_applied=filters,
            aggregations=aggregations,
            assumptions=assumptions,
            chart_rationale=chart_rationale,
            calculation_steps=calculation_steps,
        )

    # ── SQL Extraction helpers ──────────────────────────────────────────────

    def _extract_tables(self, sql: str) -> list[str]:
        tables: list[str] = []
        for m in _RE_FROM.finditer(sql):
            for t in m.group(1).split(","):
                name = _clean(t.strip('"\'`'))
                if name and name.upper() not in ("SELECT", "WHERE", "WITH"):
                    tables.append(name)
        for m in _RE_JOIN.finditer(sql):
            name = _clean(m.group(1).strip('"\'`'))
            if name not in tables:
                tables.append(name)
        return tables

    def _extract_filters(self, sql: str) -> list[str]:
        filters: list[str] = []
        m = _RE_WHERE.search(sql)
        if m:
            clause = _clean(m.group(1))
            # Split on AND/OR at top level (simplified)
            parts = re.split(r"\bAND\b|\bOR\b", clause, flags=re.IGNORECASE)
            for p in parts:
                p = _clean(p)
                if p and len(p) > 2:
                    filters.append(p)
        m2 = _RE_HAVING.search(sql)
        if m2:
            clause = _clean(m2.group(1))
            filters.append(f"HAVING {clause}")
        return filters[:8]  # cap to keep things readable

    def _extract_aggregations(self, sql: str) -> list[str]:
        seen: set[str] = set()
        aggs: list[str] = []
        for m in _RE_AGG.finditer(sql):
            fn = m.group(1).upper().replace("  ", " ").replace("( ", "(")
            if fn not in seen:
                seen.add(fn)
                aggs.append(fn)
        return aggs

    def _extract_group_by(self, sql: str) -> list[str]:
        m = _RE_GROUP_BY.search(sql)
        if not m:
            return []
        raw = _clean(m.group(1))
        return [_clean(c) for c in raw.split(",") if _clean(c)]

    def _extract_order_by(self, sql: str) -> list[str]:
        m = _RE_ORDER_BY.search(sql)
        if not m:
            return []
        raw = _clean(m.group(1))
        return [_clean(c) for c in raw.split(",") if _clean(c)][:4]

    def _extract_limit(self, sql: str) -> int | None:
        m = _RE_LIMIT.search(sql)
        return int(m.group(1)) if m else None

    # ── Plain-English builders ──────────────────────────────────────────────

    def _build_assumptions(
        self,
        aggregations: list[str],
        is_truncated: bool,
        truncated_at: int | None,
        limit: int | None,
        sql: str,
    ) -> list[str]:
        assumptions: list[str] = []

        # Aggregate null handling
        if any(a in ("SUM", "AVG", "MIN", "MAX") for a in aggregations):
            assumptions.append(
                "NULL values are automatically excluded from aggregate functions "
                "(SUM, AVG, MIN, MAX ignore NULLs by SQL standard)."
            )
        if any(a.startswith("COUNT") for a in aggregations):
            assumptions.append(
                "COUNT(*) counts all rows including NULLs; "
                "COUNT(column) excludes NULL values in that column."
            )

        # Row truncation
        if is_truncated and truncated_at:
            assumptions.append(
                f"Result truncated at {truncated_at:,} rows. "
                "Statistics and aggregates reflect only the returned rows."
            )
        elif limit:
            assumptions.append(
                f"Query includes LIMIT {limit:,} — result may not represent the full dataset."
            )

        # Date/time filter assumption
        if re.search(r"\b(YEAR|MONTH|DATE|BETWEEN)\b", sql, re.IGNORECASE):
            assumptions.append(
                "A date/time filter is applied — results reflect the specified time period only."
            )

        # DISTINCT
        if re.search(r"\bDISTINCT\b", sql, re.IGNORECASE):
            assumptions.append("DISTINCT keyword applied — duplicate rows are excluded.")

        return assumptions

    def _build_calculation_steps(
        self,
        tables: list[str],
        filters: list[str],
        group_by: list[str],
        aggregations: list[str],
        order_by: list[str],
        limit: int | None,
    ) -> list[str]:
        steps: list[str] = []
        step = 1

        if tables:
            steps.append(
                f"Step {step}: Read data from {_oxford(tables)}."
            )
            step += 1

        if filters:
            steps.append(
                f"Step {step}: Apply filter(s): {'; '.join(filters[:3])}."
            )
            step += 1

        if group_by:
            steps.append(
                f"Step {step}: Group rows by {_oxford(group_by)}."
            )
            step += 1

        if aggregations:
            steps.append(
                f"Step {step}: Compute {_oxford(aggregations)} for each group."
            )
            step += 1

        if order_by:
            steps.append(
                f"Step {step}: Sort results by {_oxford(order_by)}."
            )
            step += 1

        if limit:
            steps.append(f"Step {step}: Return the top {limit:,} rows.")
            step += 1

        if not steps:
            steps.append("Step 1: Execute query and return all matching rows.")

        return steps

    def _build_sql_breakdown(
        self,
        tables: list[str],
        filters: list[str],
        group_by: list[str],
        aggregations: list[str],
    ) -> str:
        parts: list[str] = []
        if tables:
            parts.append(f"Tables: {', '.join(tables)}")
        if aggregations:
            parts.append(f"Aggregations: {', '.join(aggregations)}")
        if group_by:
            parts.append(f"Group by: {', '.join(group_by)}")
        if filters:
            parts.append(f"Filters: {len(filters)} condition(s)")
        return " | ".join(parts) if parts else "Simple SELECT query"

    def _build_chart_rationale(
        self,
        viz: object,
        analysed_result: "AnalysedResult",
    ) -> str | None:
        if viz is None:
            return None
        try:
            mark = getattr(viz, "mark", None) or (
                viz.spec.get("mark") if hasattr(viz, "spec") else None  # type: ignore[union-attr]
            )
            if not mark:
                return None

            mark_str = mark if isinstance(mark, str) else mark.get("type", "")
            rationale_map = {
                "bar": "Bar chart selected: comparing discrete categories on a numeric measure.",
                "line": "Line chart selected: visualising trend or change over a time dimension.",
                "point": "Scatter chart selected: exploring correlation between two numeric measures.",
                "arc": "Pie/donut chart selected: showing proportional composition of a whole.",
                "area": "Area chart selected: emphasising cumulative magnitude over time.",
                "rect": "Heatmap selected: showing density or magnitude across two dimensions.",
                "tick": "Tick/strip plot selected: distribution of a single numeric variable.",
                "boxplot": "Box plot selected: summarising distribution with quartiles and outliers.",
            }
            return rationale_map.get(mark_str.lower(), f"{mark_str.title()} chart selected.")
        except Exception:  # noqa: BLE001
            return None


def _oxford(items: list[str]) -> str:
    """Format a list with an Oxford comma."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"
