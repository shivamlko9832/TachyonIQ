"""
Unit tests for uada.analytics.explainability (P4-A-2).

Fully deterministic — no LLM, no database.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from uada.analytics.explainability import ExplainabilityBuilder
from uada.models.result import ExplainabilityContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_SQL = "SELECT SUM(amount) FROM orders WHERE region = 'EMEA'"
_GROUP_SQL   = (
    "SELECT region, product, SUM(revenue) AS total "
    "FROM sales "
    "WHERE year = 2024 "
    "GROUP BY region, product "
    "ORDER BY total DESC "
    "LIMIT 10"
)
_JOIN_SQL = (
    "SELECT c.name, SUM(o.amount) "
    "FROM customers c "
    "JOIN orders o ON c.id = o.customer_id "
    "WHERE o.status = 'complete' "
    "GROUP BY c.name"
)


def _mock_analysed():
    m = MagicMock()
    m.row_count = 20
    m.numeric_summaries = []
    m.outliers = []
    m.key_finding = "Revenue totalled £100k."
    return m


def _mock_bar_viz():
    m = MagicMock()
    m.mark = "bar"
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExplainabilityBuilder:

    def test_returns_explainability_context(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed())
        assert isinstance(ctx, ExplainabilityContext)

    def test_extracts_table_from_simple_sql(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed())
        assert "orders" in ctx.tables_referenced

    def test_extracts_tables_from_join_sql(self):
        ctx = ExplainabilityBuilder().build(_JOIN_SQL, _mock_analysed())
        table_names = [t.lower() for t in ctx.tables_referenced]
        assert "customers" in " ".join(table_names) or any("c" in t for t in table_names)

    def test_extracts_aggregations_sum(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed())
        assert any("SUM" in a for a in ctx.aggregations)

    def test_extracts_aggregations_group_sql(self):
        ctx = ExplainabilityBuilder().build(_GROUP_SQL, _mock_analysed())
        assert any("SUM" in a for a in ctx.aggregations)

    def test_extracts_filters_where(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed())
        assert len(ctx.filters_applied) >= 1
        assert any("EMEA" in f for f in ctx.filters_applied)

    def test_extracts_group_by(self):
        ctx = ExplainabilityBuilder().build(_GROUP_SQL, _mock_analysed())
        # GROUP BY region, product should appear in sql_breakdown
        assert "region" in ctx.sql_breakdown.lower() or len(ctx.calculation_steps) > 0

    def test_sql_breakdown_non_empty(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed())
        assert isinstance(ctx.sql_breakdown, str)
        assert len(ctx.sql_breakdown) > 0

    def test_calculation_steps_non_empty(self):
        ctx = ExplainabilityBuilder().build(_GROUP_SQL, _mock_analysed())
        assert len(ctx.calculation_steps) >= 2

    def test_calculation_steps_sequential(self):
        ctx = ExplainabilityBuilder().build(_GROUP_SQL, _mock_analysed())
        for i, step in enumerate(ctx.calculation_steps, start=1):
            assert f"Step {i}:" in step

    def test_assumptions_include_null_handling_for_sum(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed())
        combined = " ".join(ctx.assumptions).lower()
        assert "null" in combined

    def test_assumptions_include_limit(self):
        ctx = ExplainabilityBuilder().build(_GROUP_SQL, _mock_analysed())
        combined = " ".join(ctx.assumptions).lower()
        assert "limit" in combined or "10" in combined

    def test_assumptions_include_date_filter(self):
        sql = "SELECT SUM(revenue) FROM sales WHERE year = 2024"
        ctx = ExplainabilityBuilder().build(sql, _mock_analysed())
        combined = " ".join(ctx.assumptions).lower()
        assert "date" in combined or "time" in combined or "period" in combined

    def test_chart_rationale_bar(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed(), viz=_mock_bar_viz())
        assert ctx.chart_rationale is not None
        assert "bar" in ctx.chart_rationale.lower()

    def test_chart_rationale_none_when_no_viz(self):
        ctx = ExplainabilityBuilder().build(_SIMPLE_SQL, _mock_analysed(), viz=None)
        assert ctx.chart_rationale is None

    def test_truncation_assumption_included(self):
        ctx = ExplainabilityBuilder().build(
            _SIMPLE_SQL, _mock_analysed(),
            is_truncated=True, truncated_at=1000,
        )
        combined = " ".join(ctx.assumptions).lower()
        assert "truncat" in combined or "1,000" in combined or "1000" in combined

    def test_simple_select_no_crash(self):
        sql = "SELECT * FROM products"
        ctx = ExplainabilityBuilder().build(sql, _mock_analysed())
        assert ctx is not None
        assert len(ctx.calculation_steps) >= 1
