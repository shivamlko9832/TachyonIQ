"""Unit tests for the grounded insight generator."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from pydantic_ai.models.test import TestModel

from uada.models.result import GeneratedInsights
from uada.pipeline.insight_generator import InsightGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_settings():
    s = MagicMock()
    s.llm_model = "test"
    s.llm_max_retries = 1
    return s


def _mock_analysed(
    row_count: int = 20,
    key_finding: str = "Revenue was £100k.",
    findings_bullets: list[str] | None = None,
    drivers: list[str] | None = None,
):
    from uada.models.result import NumericSummary, TrendAnalysis, TrendDirection

    a = MagicMock()
    a.query_result.row_count = row_count
    a.key_finding = key_finding
    a.key_findings_bullets = findings_bullets or ["Total revenue: £100k."]
    a.drivers = drivers or ["Seasonal demand increase."]
    a.numeric_summaries = [
        NumericSummary(column="revenue", min=10.0, max=500.0, mean=250.0, sum=5000.0, null_count=0)
    ]
    a.trend_analysis = [
        TrendAnalysis(column="revenue", direction=TrendDirection.INCREASING, change_pct=12.5)
    ]
    a.outliers = []
    a.anomaly_result = None
    a.forecast_result = None
    return a


def _mock_dq(score: float = 95.0, issues: list | None = None):
    from uada.models.result import DataQualityReport
    return DataQualityReport(
        overall_quality_score=score,
        summary="No issues." if score >= 95 else "Some issues detected.",
        issues=issues or [],
    )


def _insight_args() -> dict:
    """TestModel output args matching GeneratedInsights schema."""
    return {
        "key_findings": ["Revenue totalled £5,000 with a 12.5% increase."],
        "drivers": ["Seasonal demand uplift drove the revenue increase."],
        "recommendations": ["Drill down by region to identify the top performer."],
        "data_quality_notes": [],
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInsightGenerator:

    def test_returns_generated_insights(self):
        gen = InsightGenerator(_mock_settings())
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed())
            )
        assert isinstance(result, GeneratedInsights)

    def test_key_findings_populated(self):
        gen = InsightGenerator(_mock_settings())
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed())
            )
        assert len(result.key_findings) >= 1

    def test_drivers_populated(self):
        gen = InsightGenerator(_mock_settings())
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed())
            )
        assert len(result.drivers) >= 1

    def test_recommendations_populated(self):
        gen = InsightGenerator(_mock_settings())
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed())
            )
        assert len(result.recommendations) >= 1

    def test_confidence_valid_value(self):
        gen = InsightGenerator(_mock_settings())
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed())
            )
        assert result.confidence in ("high", "medium", "low")

    def test_data_quality_notes_list(self):
        gen = InsightGenerator(_mock_settings())
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed(), data_quality=_mock_dq())
            )
        assert isinstance(result.data_quality_notes, list)

    def test_fallback_on_llm_failure(self):
        """If the LLM agent throws, generate() returns a safe fallback."""
        gen = InsightGenerator(_mock_settings())
        # Override with a model that always errors
        class _FailModel:
            async def request(self, *a, **kw):
                raise RuntimeError("simulated LLM failure")

        # Patch the agent's internal model to raise
        async def _run():
            original = gen.agent._model
            gen.agent._model = _FailModel()  # type: ignore[assignment]
            try:
                return await gen.generate("Q?", _mock_analysed())
            finally:
                gen.agent._model = original

        result = asyncio.run(_run())
        assert isinstance(result, GeneratedInsights)
        assert len(result.key_findings) >= 1  # fallback has at least one finding
        assert result.confidence == "low"

    def test_generates_with_data_quality_context(self):
        """DataQualityReport is accepted without error."""
        from uada.models.result import ColumnQualityIssue
        issue = ColumnQualityIssue(
            column="revenue",
            issue_type="high_nulls",
            severity="high",
            detail="50% nulls in revenue column.",
        )
        dq = _mock_dq(score=50.0, issues=[issue])
        gen = InsightGenerator(_mock_settings())
        args = {
            **_insight_args(),
            "data_quality_notes": [
                "revenue has 50% nulls — interpret with caution."
            ],
        }
        with gen.agent.override(model=TestModel(custom_output_args=args)):
            import asyncio
            result = asyncio.run(
                gen.generate("What was revenue?", _mock_analysed(), data_quality=dq)
            )
        assert isinstance(result, GeneratedInsights)

    def test_statistical_report_marks_grounded_findings_verified(self):
        gen = InsightGenerator(_mock_settings())
        report = {
            "sample_size": 20,
            "descriptive": {"revenue": {"sum": 5000.0}},
            "tests": {"trend": {"revenue": {"change_pct": 12.5}}},
            "provenance": {"result_sha256": "abc123", "deterministic": True},
        }
        with gen.agent.override(model=TestModel(custom_output_args=_insight_args())):
            result = asyncio.run(
                gen.generate(
                    "What was revenue?",
                    _mock_analysed(),
                    statistical_analysis=report,
                )
            )
        assert result.evidence_verified is True
        assert result.key_findings == ["Revenue totalled £5,000 with a 12.5% increase."]

    def test_statistical_report_removes_fabricated_numeric_finding(self):
        gen = InsightGenerator(_mock_settings())
        output = {
            **_insight_args(),
            "key_findings": ["Revenue was £999,999."],
        }
        report = {
            "sample_size": 20,
            "descriptive": {"revenue": {"sum": 5000.0}},
            "provenance": {"result_sha256": "abc123", "deterministic": True},
        }
        with gen.agent.override(model=TestModel(custom_output_args=output)):
            result = asyncio.run(
                gen.generate(
                    "What was revenue?",
                    _mock_analysed(),
                    statistical_analysis=report,
                )
            )
        assert result.evidence_verified is True
        assert all("999,999" not in finding for finding in result.key_findings)
        assert any("Removed 1" in note for note in result.verification_notes)
