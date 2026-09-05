"""
Unit tests for uada.analytics.anomaly (P3-2).

No LLM, no real database — only pandas + scikit-learn.
"""

from __future__ import annotations

import pytest
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 20, with_outlier: bool = False) -> pd.DataFrame:
    """Return a clean numeric DataFrame with optional obvious outlier."""
    data = {"a": list(range(n)), "b": [v * 2.0 for v in range(n)]}
    if with_outlier:
        data["a"][n - 1] = 9999
        data["b"][n - 1] = 9999.0
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAnomalyDetector:
    """Tests for AnomalyDetector.detect()."""

    def test_returns_anomaly_result_type(self):
        from uada.analytics.anomaly import AnomalyDetector
        from uada.models.result import AnomalyResult

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a", "b"])
        assert isinstance(result, AnomalyResult)

    def test_success_not_skipped(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a", "b"])
        assert result.skipped is False

    def test_method_is_isolation_forest(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a", "b"])
        assert result.method == "isolation_forest"

    def test_skips_when_fewer_than_min_rows(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(5)
        result = AnomalyDetector().detect(df, ["a", "b"])
        assert result.skipped is True
        assert "Insufficient rows" in result.skip_reason

    def test_skips_when_no_numeric_columns(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = pd.DataFrame({"name": ["alice"] * 20})
        result = AnomalyDetector().detect(df, [])
        assert result.skipped is True

    def test_obvious_outlier_is_flagged(self):
        """A point 9999 std-deviations away should always be an anomaly."""
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(21, with_outlier=True)
        result = AnomalyDetector().detect(df, ["a", "b"])
        assert result.skipped is False
        assert result.anomaly_count >= 1
        # The last row (index 20) should be flagged
        outlier_rows = [r for r in result.anomaly_rows if r.is_anomaly]
        assert any(r.row_index == 20 for r in outlier_rows)

    def test_anomaly_rows_contain_all_input_rows(self):
        from uada.analytics.anomaly import AnomalyDetector

        n = 20
        df = _make_df(n)
        result = AnomalyDetector().detect(df, ["a", "b"])
        # Every input row should appear in anomaly_rows
        assert len(result.anomaly_rows) == n

    def test_column_deviations_present(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a", "b"])
        for row in result.anomaly_rows:
            assert "a" in row.column_deviations
            assert "b" in row.column_deviations

    def test_feature_columns_stored(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a", "b"])
        assert result.feature_columns == ["a", "b"]

    def test_contamination_parameter_respected(self):
        """Higher contamination → more anomalies flagged."""
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(40)
        r_low = AnomalyDetector().detect(df, ["a", "b"], contamination=0.01)
        r_high = AnomalyDetector().detect(df, ["a", "b"], contamination=0.3)
        assert r_high.anomaly_count >= r_low.anomaly_count

    def test_anomaly_score_is_float(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a", "b"])
        for row in result.anomaly_rows:
            assert isinstance(row.anomaly_score, float)

    def test_single_column_works(self):
        from uada.analytics.anomaly import AnomalyDetector

        df = _make_df(20)
        result = AnomalyDetector().detect(df, ["a"])
        assert result.skipped is False
        assert result.feature_columns == ["a"]
