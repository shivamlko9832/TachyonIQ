"""
Forecasting Module (P3-3)
===========================
Time-series forecasting using statsmodels (Holt-Winters Exponential Smoothing
or ARIMA). Falls back to a simple linear trend when statsmodels is unavailable
or the data is too short.

All heavy imports (statsmodels) are deferred.

Usage::

    from uada.analytics.forecast import Forecaster
    result = Forecaster().forecast(df, time_column="period", value_column="revenue", periods=4)
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

# Minimum historical observations for Holt-Winters; ARIMA needs at least 3
_MIN_HW_ROWS = 8
_MIN_ARIMA_ROWS = 5
_MIN_LINEAR_ROWS = 3

# Confidence interval Z-score (95%)
_Z_95 = 1.96


def _linear_forecast(
    series: list[float],
    periods: int,
) -> tuple[list[float], list[float], list[float], float]:
    """
    Minimal linear trend extrapolation, no external dependencies.

    Returns (forecasts, lower_ci, upper_ci, rmse).
    """
    n = len(series)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        slope = 0.0
    else:
        slope = sum((xs[i] - mean_x) * (series[i] - mean_y) for i in range(n)) / denom
    intercept = mean_y - slope * mean_x

    fitted = [slope * x + intercept for x in xs]
    residuals = [series[i] - fitted[i] for i in range(n)]
    mse = sum(r ** 2 for r in residuals) / n
    rmse = math.sqrt(mse)
    std_err = rmse * math.sqrt(1 + 1 / n)

    fcs, lowers, uppers = [], [], []
    for step in range(1, periods + 1):
        fc = slope * (n + step - 1) + intercept
        fcs.append(fc)
        lowers.append(fc - _Z_95 * std_err)
        uppers.append(fc + _Z_95 * std_err)

    return fcs, lowers, uppers, rmse


class Forecaster:
    """
    Produces a short-horizon forecast for a single time-series column.

    Strategy (descending preference):
    1. Holt-Winters Exponential Smoothing (statsmodels) — for ≥ 8 points
    2. ARIMA(1,1,0) (statsmodels) — for ≥ 5 points
    3. Linear trend extrapolation — for ≥ 3 points (no external dep)
    """

    def forecast(
        self,
        df: "pd.DataFrame",
        time_column: str,
        value_column: str,
        periods: int = 4,
    ) -> "ForecastResult":
        """
        Forecast *periods* steps ahead for *value_column* ordered by *time_column*.

        Returns
        -------
        ForecastResult
            Always returns a valid object; ``skipped=True`` when insufficient
            data or the value column is non-numeric.
        """
        from uada.models.result import ForecastPoint, ForecastResult

        if time_column not in df.columns or value_column not in df.columns:
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=0,
                forecast_periods=periods,
                skipped=True,
                skip_reason="Required columns not found in DataFrame.",
            )

        ordered = df[[time_column, value_column]].dropna().sort_values(time_column)
        series_raw = ordered[value_column].tolist()

        # Cast to float; skip if non-numeric
        try:
            series = [float(v) for v in series_raw]
        except (TypeError, ValueError):
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=len(series_raw),
                forecast_periods=periods,
                skipped=True,
                skip_reason=f"Column '{value_column}' contains non-numeric values.",
            )

        n = len(series)
        time_labels = [str(t) for t in ordered[time_column].tolist()]

        if n < _MIN_LINEAR_ROWS:
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=n,
                forecast_periods=periods,
                skipped=True,
                skip_reason=f"Insufficient observations ({n} < {_MIN_LINEAR_ROWS}).",
            )

        method, fcs, lowers, uppers, rmse = self._fit(series, periods, n)

        # Generate future period labels by extending the last time label pattern
        future_labels = self._extend_time_labels(time_labels, periods)

        points = [
            ForecastPoint(
                period=future_labels[i],
                forecast=round(fcs[i], 4),
                lower_ci=round(lowers[i], 4),
                upper_ci=round(uppers[i], 4),
            )
            for i in range(periods)
        ]

        logger.info(
            "Forecaster: method=%s, historical=%d, forecast=%d steps, rmse=%.4f",
            method, n, periods, rmse,
        )
        return ForecastResult(
            method=method,
            forecast_column=value_column,
            time_column=time_column,
            historical_periods=n,
            forecast_periods=periods,
            points=points,
            model_fit_rmse=round(rmse, 4),
            skipped=False,
        )

    # ── Internal fitting strategy ─────────────────────────────────────────────

    def _fit(
        self,
        series: list[float],
        periods: int,
        n: int,
    ) -> tuple[str, list[float], list[float], list[float], float]:
        """Try Holt-Winters → ARIMA → linear, return (method, fcs, lowers, uppers, rmse)."""

        if n >= _MIN_HW_ROWS:
            result = self._try_holt_winters(series, periods)
            if result is not None:
                return ("holt_winters", *result)

        if n >= _MIN_ARIMA_ROWS:
            result = self._try_arima(series, periods)
            if result is not None:
                return ("arima", *result)

        fcs, lowers, uppers, rmse = _linear_forecast(series, periods)
        return "linear_trend", fcs, lowers, uppers, rmse

    def _try_holt_winters(
        self, series: list[float], periods: int
    ) -> tuple[list[float], list[float], list[float], float] | None:
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing

            model = ExponentialSmoothing(
                series,
                trend="add",
                seasonal=None,
                initialization_method="estimated",
            )
            fit = model.fit(optimized=True, disp=False)
            fcs = [float(v) for v in fit.forecast(periods)]
            fitted_vals = fit.fittedvalues.tolist()
            residuals = [series[i] - fitted_vals[i] for i in range(len(series))]
            mse = sum(r ** 2 for r in residuals) / len(residuals)
            rmse = math.sqrt(mse)
            std_err = rmse * math.sqrt(1 + 1 / len(series))
            lowers = [f - _Z_95 * std_err for f in fcs]
            uppers = [f + _Z_95 * std_err for f in fcs]
            return fcs, lowers, uppers, rmse
        except Exception as exc:
            logger.debug("Holt-Winters failed: %s", exc)
            return None

    def _try_arima(
        self, series: list[float], periods: int
    ) -> tuple[list[float], list[float], list[float], float] | None:
        try:
            from statsmodels.tsa.arima.model import ARIMA

            model = ARIMA(series, order=(1, 1, 0))
            fit = model.fit()
            forecast_obj = fit.get_forecast(steps=periods)
            fcs = forecast_obj.predicted_mean.tolist()
            ci = forecast_obj.conf_int(alpha=0.05)
            lowers = ci.iloc[:, 0].tolist()
            uppers = ci.iloc[:, 1].tolist()
            rmse = math.sqrt(float(fit.mse))
            return fcs, lowers, uppers, rmse
        except Exception as exc:
            logger.debug("ARIMA failed: %s", exc)
            return None

    # ── Time label extension ──────────────────────────────────────────────────

    @staticmethod
    def _extend_time_labels(labels: list[str], periods: int) -> list[str]:
        """
        Naively extend the time label sequence by *periods* steps.

        Handles ISO dates (YYYY-MM-DD), year integers, and generic strings.
        """
        if not labels:
            return [f"T+{i+1}" for i in range(periods)]

        # Try numeric year
        try:
            last_year = int(labels[-1])
            return [str(last_year + i + 1) for i in range(periods)]
        except ValueError:
            pass

        # Try ISO date
        try:
            from datetime import date, timedelta

            last_date = date.fromisoformat(labels[-1][:10])
            if len(labels) >= 2:
                try:
                    prev_date = date.fromisoformat(labels[-2][:10])
                    delta = last_date - prev_date
                except ValueError:
                    delta = timedelta(days=30)
            else:
                delta = timedelta(days=30)

            return [(last_date + delta * (i + 1)).isoformat() for i in range(periods)]
        except (ValueError, TypeError):
            pass

        # Fallback: append +1, +2, ...
        return [f"{labels[-1]}+{i+1}" for i in range(periods)]
