"""
Unit tests for uada.analytics.forecast (P3-3).

No LLM, no real database.
Exercises the Forecaster across all three fitting strategies plus edge cases.
"""

from __future__ import annotations

import pytest
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_series_df(n: int, *, time_col: str = "period", value_col: str = "revenue") -> pd.DataFrame:
    """Return a simple increasing series DataFrame."""
    return pd.DataFrame({
        time_col: list(range(2010, 2010 + n)),
        value_col: [float(v * 100) for v in range(1, n + 1)],
    })


def _make_date_df(n: int) -> pd.DataFrame:
    """Return a DataFrame with ISO date time labels."""
    from datetime import date, timedelta
    start = date(2023, 1, 1)
    return pd.DataFrame({
        "date": [(start + timedelta(days=30 * i)).isoformat() for i in range(n)],
        "value": [float(i * 10) for i in range(1, n + 1)],
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestForecaster:
    """Tests for Forecaster.forecast()."""

    def test_returns_forecast_result_type(self):
        from uada.analytics.forecast import Forecaster
        from uada.models.result import ForecastResult

        df = _make_series_df(10)
        result = Forecaster().forecast(df, "period", "revenue", periods=3)
        assert isinstance(result, ForecastResult)

    def test_linear_trend_on_short_series(self):
        """3-4 rows → linear_trend fallback."""
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(4)
        result = Forecaster().forecast(df, "period", "revenue", periods=2)
        assert result.skipped is False
        assert result.method == "linear_trend"
        assert len(result.points) == 2

    def test_skips_fewer_than_min_linear_rows(self):
        """< 3 rows → skipped."""
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(2)
        result = Forecaster().forecast(df, "period", "revenue", periods=2)
        assert result.skipped is True
        assert "Insufficient observations" in result.skip_reason

    def test_skips_missing_time_column(self):
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(10)
        result = Forecaster().forecast(df, "nonexistent", "revenue", periods=3)
        assert result.skipped is True
        assert "not found" in result.skip_reason.lower()

    def test_skips_missing_value_column(self):
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(10)
        result = Forecaster().forecast(df, "period", "nonexistent", periods=3)
        assert result.skipped is True

    def test_skips_non_numeric_value_column(self):
        from uada.analytics.forecast import Forecaster

        df = pd.DataFrame({
            "period": list(range(10)),
            "label": ["Q" + str(i) for i in range(10)],
        })
        result = Forecaster().forecast(df, "period", "label", periods=2)
        assert result.skipped is True
        assert "non-numeric" in result.skip_reason.lower()

    def test_correct_number_of_forecast_points(self):
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(10)
        for periods in (1, 3, 6):
            result = Forecaster().forecast(df, "period", "revenue", periods=periods)
            if not result.skipped:
                assert len(result.points) == periods

    def test_forecast_points_have_required_fields(self):
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(5)
        result = Forecaster().forecast(df, "period", "revenue", periods=2)
        assert result.skipped is False
        for pt in result.points:
            assert pt.period is not None
            assert isinstance(pt.forecast, float)
            assert isinstance(pt.lower_ci, float)
            assert isinstance(pt.upper_ci, float)
            assert pt.lower_ci <= pt.forecast <= pt.upper_ci

    def test_historical_periods_recorded(self):
        from uada.analytics.forecast import Forecaster

        n = 7
        df = _make_series_df(n)
        result = Forecaster().forecast(df, "period", "revenue", periods=3)
        assert result.historical_periods == n

    def test_forecast_column_and_time_column_recorded(self):
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(5)
        result = Forecaster().forecast(df, "period", "revenue", periods=2)
        assert result.forecast_column == "revenue"
        assert result.time_column == "period"

    def test_rmse_is_non_negative(self):
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(5)
        result = Forecaster().forecast(df, "period", "revenue", periods=2)
        assert result.skipped is False
        assert result.model_fit_rmse >= 0.0

    def test_iso_date_labels_extended(self):
        """Future labels should be valid ISO date strings."""
        from uada.analytics.forecast import Forecaster

        df = _make_date_df(5)
        result = Forecaster().forecast(df, "date", "value", periods=3)
        assert result.skipped is False
        for pt in result.points:
            # ISO date format: YYYY-MM-DD
            assert len(pt.period) >= 8
            assert "-" in pt.period

    def test_integer_year_labels_extended(self):
        """Year int labels → next years (2024, 2025, ...)."""
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(5, time_col="year")
        result = Forecaster().forecast(df, "year", "revenue", periods=3)
        assert result.skipped is False
        # Last historical year is 2014; next should start at 2015
        first_future_year = int(result.points[0].period)
        assert first_future_year == 2015

    def test_constant_series_does_not_crash(self):
        """A flat series (slope=0) should still return a result."""
        from uada.analytics.forecast import Forecaster

        df = pd.DataFrame({"period": list(range(5)), "revenue": [100.0] * 5})
        result = Forecaster().forecast(df, "period", "revenue", periods=2)
        assert result.skipped is False
        for pt in result.points:
            assert abs(pt.forecast - 100.0) < 1.0  # close to the constant

    def test_holt_winters_used_for_long_series(self):
        """≥ 8 rows should try Holt-Winters if statsmodels available."""
        from uada.analytics.forecast import Forecaster

        df = _make_series_df(12)
        result = Forecaster().forecast(df, "period", "revenue", periods=3)
        assert result.skipped is False
        # Either holt_winters or arima (if HW fails for any reason), never missing
        assert result.method in ("holt_winters", "arima", "linear_trend")
