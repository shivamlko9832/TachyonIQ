"""Deterministic, validated time-series forecasting.

The language model decides that a forecast is requested; this module owns every
numeric operation after that decision. Candidate models are compared on a
chronological holdout, the winner is refit on all observations, and every
forecast carries uncertainty, validation error, assumptions, and a data hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from statistics import NormalDist
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd

    from uada.models.result import ForecastResult

logger = logging.getLogger(__name__)

_MIN_HW_ROWS = 8
_MIN_ARIMA_ROWS = 18
_MIN_LINEAR_ROWS = 3

FitResult = tuple[list[float], list[float], list[float], float]


def _critical_value(confidence_level: float, degrees_freedom: int | None = None) -> float:
    """Return a two-sided critical value, preferring Student's t for small samples."""
    alpha = 1.0 - confidence_level
    if degrees_freedom and degrees_freedom > 0:
        try:
            from scipy.stats import t

            return float(t.ppf(1.0 - alpha / 2.0, degrees_freedom))
        except Exception:  # pragma: no cover - optional runtime dependency
            pass
    return NormalDist().inv_cdf(1.0 - alpha / 2.0)


def _linear_forecast(
    series: list[float], periods: int, confidence_level: float = 0.95
) -> FitResult:
    """OLS trend forecast with prediction intervals that widen with horizon."""
    n = len(series)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = (
        sum((xs[i] - mean_x) * (series[i] - mean_y) for i in range(n)) / sxx
        if sxx
        else 0.0
    )
    intercept = mean_y - slope * mean_x
    residuals = [series[i] - (slope * xs[i] + intercept) for i in range(n)]
    sse = sum(residual**2 for residual in residuals)
    rmse = math.sqrt(sse / n)
    residual_std = math.sqrt(sse / max(n - 2, 1))
    critical = _critical_value(confidence_level, n - 2)

    forecasts: list[float] = []
    lowers: list[float] = []
    uppers: list[float] = []
    for step in range(1, periods + 1):
        future_x = n + step - 1
        forecast = slope * future_x + intercept
        leverage = 1.0 + 1.0 / n
        if sxx:
            leverage += ((future_x - mean_x) ** 2) / sxx
        prediction_error = residual_std * math.sqrt(leverage)
        forecasts.append(forecast)
        lowers.append(forecast - critical * prediction_error)
        uppers.append(forecast + critical * prediction_error)
    return forecasts, lowers, uppers, rmse


class Forecaster:
    """Select and validate a short-horizon model for one or more time series."""

    def forecast(
        self,
        df: pd.DataFrame,
        time_column: str,
        value_column: str,
        periods: int = 4,
        *,
        confidence_level: float = 0.95,
    ) -> ForecastResult:
        from uada.models.result import ForecastPoint, ForecastResult

        if periods < 1:
            raise ValueError("Forecast periods must be at least one.")
        if not 0.5 < confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0.5 and 1.0.")
        if time_column not in df.columns or value_column not in df.columns:
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=0,
                forecast_periods=periods,
                confidence_level=confidence_level,
                skipped=True,
                skip_reason="Required columns not found in DataFrame.",
            )

        ordered = df[[time_column, value_column]].dropna().sort_values(time_column)
        try:
            series = [float(value) for value in ordered[value_column].tolist()]
        except (TypeError, ValueError):
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=len(ordered),
                forecast_periods=periods,
                confidence_level=confidence_level,
                skipped=True,
                skip_reason=f"Column '{value_column}' contains non-numeric values.",
            )

        labels = [str(label) for label in ordered[time_column].tolist()]
        n = len(series)
        if n < _MIN_LINEAR_ROWS:
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=n,
                forecast_periods=periods,
                confidence_level=confidence_level,
                skipped=True,
                skip_reason=f"Insufficient observations ({n} < {_MIN_LINEAR_ROWS}).",
            )

        method, scores, validation = self._select_model(series, confidence_level)
        forecasts, lowers, uppers, fit_rmse = self._fit_named(
            method, series, periods, confidence_level
        )
        future_labels, frequency = self._extend_time_labels(labels, periods)
        points = [
            ForecastPoint(
                period=future_labels[index],
                forecast=round(forecasts[index], 4),
                lower_ci=round(lowers[index], 4),
                upper_ci=round(uppers[index], 4),
            )
            for index in range(periods)
        ]
        assumptions = [
            "Observations are ordered, equally spaced time buckets.",
            "The historical measurement definition remains stable over the forecast horizon.",
            "Intervals describe model uncertainty and do not represent a causal scenario.",
        ]
        logger.info(
            "Forecast selected %s from %s using chronological holdout RMSE; n=%d horizon=%d",
            method,
            sorted(scores),
            n,
            periods,
        )
        return ForecastResult(
            method=method,
            forecast_column=value_column,
            time_column=time_column,
            historical_periods=n,
            forecast_periods=periods,
            points=points,
            model_fit_rmse=round(fit_rmse, 4),
            confidence_level=confidence_level,
            frequency=frequency,
            training_start=labels[0] if labels else None,
            training_end=labels[-1] if labels else None,
            validation_rmse=round(validation["rmse"], 4),
            validation_mae=round(validation["mae"], 4),
            validation_mape=(
                round(validation["mape"], 4) if math.isfinite(validation["mape"]) else None
            ),
            candidate_scores={name: round(score, 4) for name, score in scores.items()},
            assumptions=assumptions,
            data_fingerprint=self._fingerprint(labels, series),
            skipped=False,
        )

    def forecast_grouped(
        self,
        df: pd.DataFrame,
        time_column: str,
        value_column: str,
        group_columns: list[str],
        periods: int = 4,
        *,
        confidence_level: float = 0.95,
    ) -> ForecastResult:
        """Fit an independently validated model per requested dimension member."""
        from uada.models.result import ForecastPoint, ForecastResult, ForecastSeries

        valid_groups = [column for column in group_columns if column in df.columns]
        if not valid_groups:
            return self.forecast(
                df, time_column, value_column, periods, confidence_level=confidence_level
            )

        series_results: list[ForecastSeries] = []
        point_totals: dict[str, list[float]] = {}
        method_counts: dict[str, int] = {}
        fingerprints: list[str] = []
        group_key: str | list[str] = valid_groups[0] if len(valid_groups) == 1 else valid_groups
        for key, group_frame in df.groupby(group_key, dropna=False):
            key_values = key if isinstance(key, tuple) else (key,)
            group_label = ", ".join(
                f"{column}={value}"
                for column, value in zip(valid_groups, key_values, strict=True)
            )
            result = self.forecast(
                group_frame,
                time_column,
                value_column,
                periods,
                confidence_level=confidence_level,
            )
            if result.skipped:
                continue
            method_counts[result.method] = method_counts.get(result.method, 0) + 1
            if result.data_fingerprint:
                fingerprints.append(result.data_fingerprint)
            grouped_points: list[ForecastPoint] = []
            for point in result.points:
                grouped_point = point.model_copy(update={"group": group_label})
                grouped_points.append(grouped_point)
                totals = point_totals.setdefault(point.period, [0.0, 0.0, 0.0])
                totals[0] += point.forecast
                totals[1] += point.lower_ci
                totals[2] += point.upper_ci
            series_results.append(
                ForecastSeries(
                    group=group_label,
                    method=result.method,
                    historical_periods=result.historical_periods,
                    points=grouped_points,
                    validation_rmse=result.validation_rmse,
                )
            )

        if not series_results:
            return ForecastResult(
                method="none",
                forecast_column=value_column,
                time_column=time_column,
                historical_periods=0,
                forecast_periods=periods,
                confidence_level=confidence_level,
                group_columns=valid_groups,
                skipped=True,
                skip_reason="No dimension member had enough observations for forecasting.",
            )

        aggregate_points = [
            ForecastPoint(
                period=period,
                forecast=round(values[0], 4),
                lower_ci=round(values[1], 4),
                upper_ci=round(values[2], 4),
            )
            for period, values in sorted(point_totals.items())
        ]
        observed_labels = sorted({str(value) for value in df[time_column].dropna().tolist()})
        _, frequency = self._extend_time_labels(observed_labels, periods)
        return ForecastResult(
            method=max(method_counts, key=method_counts.get),
            forecast_column=value_column,
            time_column=time_column,
            historical_periods=len(df),
            forecast_periods=periods,
            points=aggregate_points,
            confidence_level=confidence_level,
            frequency=frequency,
            candidate_scores={name: float(count) for name, count in method_counts.items()},
            assumptions=[
                "Each dimension member is modelled independently.",
                "The aggregate forecast is the sum of member forecasts and interval bounds.",
            ],
            data_fingerprint=hashlib.sha256("|".join(sorted(fingerprints)).encode()).hexdigest(),
            group_columns=valid_groups,
            series=series_results,
            skipped=False,
        )

    def _select_model(
        self, series: list[float], confidence_level: float
    ) -> tuple[str, dict[str, float], dict[str, float]]:
        n = len(series)
        if n < _MIN_HW_ROWS:
            fitted = self._rolling_validation("linear_trend", series, confidence_level)
            return "linear_trend", {"linear_trend": fitted["rmse"]}, fitted

        holdout = min(6, max(2, n // 5))
        training = series[:-holdout]
        actual = series[-holdout:]
        candidates: dict[str, Callable[[], FitResult]] = {
            "linear_trend": lambda: _linear_forecast(training, holdout, confidence_level),
            "holt_winters": lambda: self._fit_holt_winters(training, holdout, confidence_level),
        }
        if len(training) >= _MIN_ARIMA_ROWS:
            candidates["arima"] = lambda: self._fit_arima(
                training, holdout, confidence_level
            )

        scores: dict[str, float] = {}
        metrics: dict[str, dict[str, float]] = {}
        for name, fit_candidate in candidates.items():
            try:
                predicted = fit_candidate()[0]
                metric = self._error_metrics(actual, predicted)
                if math.isfinite(metric["rmse"]):
                    scores[name] = metric["rmse"]
                    metrics[name] = metric
            except Exception as exc:  # noqa: BLE001
                logger.debug("Forecast candidate %s rejected: %s", name, exc)

        if not scores:
            metric = self._rolling_validation("linear_trend", series, confidence_level)
            return "linear_trend", {"linear_trend": metric["rmse"]}, metric
        selected = min(scores, key=scores.get)
        return selected, scores, metrics[selected]

    def _rolling_validation(
        self, method: str, series: list[float], confidence_level: float
    ) -> dict[str, float]:
        if len(series) <= _MIN_LINEAR_ROWS:
            return {"rmse": 0.0, "mae": 0.0, "mape": 0.0}
        holdout = min(3, max(1, len(series) // 4))
        training = series[:-holdout]
        actual = series[-holdout:]
        predicted = self._fit_named(method, training, holdout, confidence_level)[0]
        return self._error_metrics(actual, predicted)

    @staticmethod
    def _error_metrics(actual: list[float], predicted: list[float]) -> dict[str, float]:
        errors = [actual[i] - predicted[i] for i in range(min(len(actual), len(predicted)))]
        if not errors:
            return {"rmse": float("inf"), "mae": float("inf"), "mape": float("inf")}
        rmse = math.sqrt(sum(error**2 for error in errors) / len(errors))
        mae = sum(abs(error) for error in errors) / len(errors)
        percentage_errors = [
            abs(errors[index] / actual[index])
            for index in range(len(errors))
            if actual[index] != 0
        ]
        mape = (
            100.0 * sum(percentage_errors) / len(percentage_errors)
            if percentage_errors
            else float("inf")
        )
        return {"rmse": rmse, "mae": mae, "mape": mape}

    def _fit_named(
        self,
        method: str,
        series: list[float],
        periods: int,
        confidence_level: float,
    ) -> FitResult:
        if method == "holt_winters":
            try:
                return self._fit_holt_winters(series, periods, confidence_level)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Holt-Winters refit failed; using OLS: %s", exc)
        elif method == "arima":
            try:
                return self._fit_arima(series, periods, confidence_level)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ARIMA refit failed; using OLS: %s", exc)
        return _linear_forecast(series, periods, confidence_level)

    @staticmethod
    def _fit_holt_winters(
        series: list[float], periods: int, confidence_level: float
    ) -> FitResult:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        fit = ExponentialSmoothing(
            series, trend="add", seasonal=None, initialization_method="estimated"
        ).fit(optimized=True, disp=False)
        forecasts = [float(value) for value in fit.forecast(periods)]
        fitted = [float(value) for value in fit.fittedvalues]
        residuals = [series[index] - fitted[index] for index in range(len(series))]
        rmse = math.sqrt(sum(value**2 for value in residuals) / len(residuals))
        critical = _critical_value(confidence_level, max(len(series) - 2, 1))
        lowers = [
            value - critical * rmse * math.sqrt(step)
            for step, value in enumerate(forecasts, start=1)
        ]
        uppers = [
            value + critical * rmse * math.sqrt(step)
            for step, value in enumerate(forecasts, start=1)
        ]
        return forecasts, lowers, uppers, rmse

    @staticmethod
    def _fit_arima(
        series: list[float], periods: int, confidence_level: float
    ) -> FitResult:
        from statsmodels.tsa.arima.model import ARIMA

        candidate_orders = [(0, 1, 0), (1, 1, 0), (0, 1, 1), (1, 1, 1), (2, 1, 0), (0, 1, 2)]
        best_fit = None
        best_aic = float("inf")
        for order in candidate_orders:
            try:
                fitted = ARIMA(series, order=order).fit()
                if math.isfinite(float(fitted.aic)) and float(fitted.aic) < best_aic:
                    best_fit = fitted
                    best_aic = float(fitted.aic)
            except Exception:
                continue
        if best_fit is None:
            raise ValueError("No ARIMA candidate converged.")
        forecast = best_fit.get_forecast(steps=periods)
        values = [float(value) for value in forecast.predicted_mean.tolist()]
        interval = forecast.conf_int(alpha=1.0 - confidence_level)
        if hasattr(interval, "iloc"):
            lowers = [float(value) for value in interval.iloc[:, 0].tolist()]
            uppers = [float(value) for value in interval.iloc[:, 1].tolist()]
        else:
            lowers = [float(row[0]) for row in interval]
            uppers = [float(row[1]) for row in interval]
        return values, lowers, uppers, math.sqrt(float(best_fit.mse))

    @staticmethod
    def _fingerprint(labels: list[str], series: list[float]) -> str:
        payload = json.dumps(list(zip(labels, series, strict=True)), separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _extend_time_labels(labels: list[str], periods: int) -> tuple[list[str], str]:
        """Extend calendar buckets without drifting across month boundaries."""
        if not labels:
            return [f"T+{index + 1}" for index in range(periods)], "unknown"
        try:
            last_year = int(labels[-1])
            if len(labels[-1]) == 4:
                return [str(last_year + index + 1) for index in range(periods)], "year"
        except ValueError:
            pass
        try:
            import pandas as pd

            parsed = pd.to_datetime(labels, errors="raise")
            inferred = pd.infer_freq(parsed) if len(parsed) >= 3 else None
            if inferred:
                future = pd.date_range(start=parsed[-1], periods=periods + 1, freq=inferred)[1:]
                return [timestamp.date().isoformat() for timestamp in future], inferred
            last = parsed[-1]
            previous = parsed[-2] if len(parsed) >= 2 else None
            if all(timestamp.day == 1 for timestamp in parsed):
                future = [last + pd.DateOffset(months=index + 1) for index in range(periods)]
                return [timestamp.date().isoformat() for timestamp in future], "MS"
            if previous is not None:
                delta = last - previous
                future = [last + delta * (index + 1) for index in range(periods)]
                return [timestamp.date().isoformat() for timestamp in future], str(delta)
        except Exception:  # noqa: BLE001
            pass
        return [f"{labels[-1]}+{index + 1}" for index in range(periods)], "unknown"
