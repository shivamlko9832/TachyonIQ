"""Deterministic statistical analysis over validated query results.

This is the numerical proof layer between database execution and narration.
It never calls an LLM and never reads application demo constants. Every value
in the report is derived from the exact rows returned by the validated SQL.
"""

from __future__ import annotations

import hashlib
import json
import math
from statistics import NormalDist
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uada.models.intent import AnalyticalIntent
    from uada.models.query_plan import QueryPlan
    from uada.models.result import AnalysedResult, ForecastResult


class StatisticalEngine:
    """Compute descriptive and inferential evidence selected by analytical intent."""

    VERSION = "deterministic-statistics-v1"

    def analyse(
        self,
        analysed: AnalysedResult,
        intent: AnalyticalIntent,
        plan: QueryPlan,
    ) -> dict[str, Any]:
        import pandas as pd

        result = analysed.query_result
        frame = pd.DataFrame(result.rows, columns=result.column_names)
        numeric_columns: list[str] = []
        for column in frame.columns:
            converted = pd.to_numeric(frame[column], errors="coerce")
            if int(converted.notna().sum()) > 0:
                frame[column] = converted
                numeric_columns.append(str(column))

        operations = list(dict.fromkeys(plan.analysis_operations or ["descriptive"]))
        report: dict[str, Any] = {
            "engine": self.VERSION,
            "operations": operations,
            "analysis_profile": plan.analysis_profile,
            "confidence_level": plan.confidence_level,
            "sample_size": int(len(frame)),
            "columns_analyzed": result.column_names,
            "descriptive": self._descriptive(frame, numeric_columns, plan.confidence_level),
            "aggregates": self._semantic_aggregates(frame, plan),
            "tests": {},
            "anomalies": {},
            "provenance": {
                "source_rows": int(result.row_count),
                "executed_sql_sha256": hashlib.sha256(
                    (result.executed_sql or "").encode()
                ).hexdigest(),
                "result_sha256": self._frame_fingerprint(result.column_names, result.rows),
                "database_dialect": result.database_dialect,
                "is_truncated": result.is_truncated,
                "deterministic": True,
            },
            "methods": [],
            "assumptions": [
                "Statistics use only rows returned by the validated read-only query.",
                "Missing numeric values are excluded per statistic and reported as null counts.",
            ],
            "limitations": [],
        }

        if plan.order_by and plan.dimensions:
            report["ranking"] = self._ranking(frame, plan)
            report["methods"].append("governed metric ranking over returned groups")

        requested = set(operations)
        time_column = analysed.time_column
        if time_column and time_column in frame.columns:
            if requested.intersection({"trend", "regression", "executive_summary", "forecast"}):
                report["tests"]["trend"] = self._trend_tests(
                    frame, time_column, numeric_columns, plan.confidence_level
                )
                report["methods"].extend(
                    ["ordinary least squares trend", "Mann-Kendall monotonic trend test"]
                )
            if requested.intersection({"anomaly", "executive_summary", "forecast"}):
                report["anomalies"] = self._robust_anomalies(
                    frame, time_column, numeric_columns
                )
                report["methods"].append("median absolute deviation anomaly detection")

        if requested.intersection({"correlation", "driver_analysis"}):
            report["tests"]["correlation"] = self._correlations(
                frame, numeric_columns, plan.confidence_level
            )
            report["methods"].extend(
                [
                    "Pearson correlation with two-sided significance test",
                    "Benjamini-Hochberg false-discovery-rate control",
                ]
            )

        if requested.intersection({"regression", "driver_analysis"}):
            dependent = plan.measures[0].output_alias if plan.measures else None
            predictors = [
                measure.output_alias
                for measure in plan.measures[1:]
                if measure.output_alias in numeric_columns
            ]
            regression = self._regression(
                frame,
                dependent if dependent in numeric_columns else None,
                predictors,
                plan.confidence_level,
            )
            if regression:
                report["tests"]["regression"] = regression
                report["methods"].append(
                    "multivariable ordinary least squares with HC3 robust standard errors"
                )

        if plan.is_comparison:
            report["comparison"] = self._comparison(frame, numeric_columns)
            report["methods"].append("absolute and relative period delta")

        if "distribution" in requested:
            report["tests"]["distribution"] = self._distribution_tests(
                frame, numeric_columns, plan.confidence_level
            )
            report["methods"].append("D'Agostino-Pearson omnibus normality test")

        if "distribution" in requested or "descriptive" in requested:
            report["methods"].append("sample descriptive statistics and t confidence interval")

        if result.is_truncated:
            report["limitations"].append(
                "The database result was truncated; statistics describe the returned "
                "population only."
            )
        if len(frame) < 3:
            report["limitations"].append(
                "Fewer than three observations limit inferential and variability analysis."
            )
        return self._clean(report)

    def attach_forecast(
        self,
        report: dict[str, Any],
        forecast: ForecastResult | None,
        latest_actual: float | None,
    ) -> dict[str, Any]:
        if forecast is None:
            return report
        updated = dict(report)
        updated["forecast"] = forecast.model_dump(mode="json")
        updated["forecast_horizon"] = forecast.forecast_periods
        updated["forecast_method"] = forecast.method
        updated["latest_actual"] = latest_actual
        if forecast.points and latest_actual not in (None, 0):
            updated["first_forecast_change_pct"] = (
                (forecast.points[0].forecast - float(latest_actual)) / abs(float(latest_actual))
            ) * 100.0
        updated.setdefault("methods", []).append(
            "chronological holdout model selection by validation RMSE"
        )
        updated.setdefault("assumptions", []).extend(forecast.assumptions)
        return self._clean(updated)

    def narrative(
        self,
        report: dict[str, Any],
        intent: AnalyticalIntent,
        plan: QueryPlan,
    ) -> str | None:
        forecast = report.get("forecast")
        if isinstance(forecast, dict) and forecast.get("points"):
            points = forecast["points"]
            first = points[0]
            level = float(forecast.get("confidence_level", 0.95)) * 100
            validation = forecast.get("validation_rmse")
            group_note = ""
            if forecast.get("group_columns"):
                group_note = (
                    f" It modelled {len(forecast.get('series', []))} dimension members "
                    "independently and aggregated their projections."
                )
            validation_note = (
                f" Chronological holdout RMSE was {self._format_number(validation)}."
                if validation is not None
                else ""
            )
            return (
                f"Forecast {forecast['forecast_column']} for {len(points)} "
                f"{self._frequency_label(forecast.get('frequency'), len(points))} using the "
                f"{str(forecast.get('method', '')).replace('_', ' ')} model selected by "
                f"holdout error. The first estimate is {self._format_number(first['forecast'])} "
                f"with a {level:.0f}% prediction interval of "
                f"{self._format_number(first['lower_ci'])} to "
                f"{self._format_number(first['upper_ci'])}.{validation_note}{group_note}"
            )

        if plan.analysis_profile == "executive_business_review":
            aggregates = report.get("aggregates", {})
            parts: list[str] = []
            labels = {
                "scorecard_revenue": "recognized revenue",
                "scorecard_revenue_target": "revenue target",
                "scorecard_profit": "profit",
                "scorecard_profit_margin_pct": "average profit margin",
                "scorecard_active_customers": "latest active customers",
                "scorecard_new_accounts": "new accounts",
                "scorecard_churned_accounts": "churned accounts",
                "scorecard_support_tickets": "support tickets",
                "scorecard_weighted_csat": "average CSAT",
                "scorecard_marketing_spend": "marketing spend",
            }
            for key, label in labels.items():
                item = aggregates.get(key)
                if isinstance(item, dict) and item.get("value") is not None:
                    parts.append(f"{label} {self._format_number(item['value'])}")
            trend = report.get("tests", {}).get("trend", {})
            significant = [
                value
                for value in trend.values()
                if isinstance(value, dict) and value.get("significant")
            ]
            evidence = (
                f" {len(significant)} metric trend(s) are statistically significant at "
                f"{float(report.get('confidence_level', 0.95)) * 100:.0f}% confidence."
                if significant
                else " No metric trend met the configured significance threshold."
            )
            if parts:
                return "Executive analysis: " + "; ".join(parts) + "." + evidence

        ranking = report.get("ranking", {})
        if isinstance(ranking, dict) and ranking.get("rows"):
            top = ranking["rows"][0]
            dimension = ranking.get("dimension", "group")
            ordered_by = ranking.get("ordered_by", "metric")
            evidence_parts = [
                f"{name.replace('_', ' ')} {self._format_number(value)}"
                for name, value in top.items()
                if name != dimension and value is not None
            ]
            evidence = "; ".join(evidence_parts)
            return (
                f"{top.get(dimension)} ranks highest by {ordered_by.replace('_', ' ')}"
                f" among the {report.get('sample_size', 0)} returned groups"
                f". Supporting observed measures: {evidence}."
            )

        comparison = report.get("comparison", {})
        if comparison:
            first = next(iter(comparison.values()))
            if isinstance(first, dict):
                return (
                    f"The latest period is {self._format_number(first.get('latest'))}, "
                    f"a {self._format_number(first.get('change_pct'))}% change from "
                    f"{self._format_number(first.get('comparison'))}."
                )
        return None

    @staticmethod
    def _descriptive(frame: Any, columns: list[str], confidence_level: float) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for column in columns:
            series = frame[column].dropna().astype(float)
            count = int(series.count())
            if not count:
                continue
            mean = float(series.mean())
            std = float(series.std(ddof=1)) if count > 1 else 0.0
            sem = std / math.sqrt(count) if count else 0.0
            critical = StatisticalEngine._t_critical(confidence_level, count - 1)
            q1, median, q3 = [float(value) for value in series.quantile([0.25, 0.5, 0.75])]
            result[column] = {
                "count": count,
                "null_count": int(frame[column].isna().sum()),
                "sum": float(series.sum()),
                "mean": mean,
                "median": median,
                "stddev": std,
                "variance": float(series.var(ddof=1)) if count > 1 else 0.0,
                "min": float(series.min()),
                "max": float(series.max()),
                "q1": q1,
                "q3": q3,
                "iqr": q3 - q1,
                "coefficient_of_variation_pct": abs(std / mean * 100) if mean else None,
                "standard_error": sem,
                "mean_confidence_interval": [mean - critical * sem, mean + critical * sem],
            }
        return result

    @staticmethod
    def _semantic_aggregates(frame: Any, plan: QueryPlan) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for measure in plan.measures:
            column = measure.output_alias
            if column not in frame.columns:
                continue
            series = frame[column].dropna().astype(float)
            if series.empty:
                continue
            additivity = measure.additivity.lower()
            if additivity == "additive":
                value, method = float(series.sum()), "sum"
            elif additivity == "semi_additive":
                value, method = float(series.iloc[-1]), "latest_time_bucket"
            else:
                value, method = float(series.mean()), "mean_across_returned_buckets"
            result[column] = {
                "value": value,
                "method": method,
                "additivity": additivity,
                "observations": int(series.count()),
            }
        return result

    @staticmethod
    def _ranking(frame: Any, plan: QueryPlan) -> dict[str, Any]:
        """Capture the exact governed ranking rows used by the narrative."""
        dimension = plan.dimensions[0].output_alias
        ordered_by = plan.order_by[0].sql_expression
        columns = [
            column
            for column in [dimension, *(measure.output_alias for measure in plan.measures)]
            if column in frame.columns
        ]
        presentation_limit = 10
        rows = frame[columns].head(presentation_limit).to_dict(orient="records")
        return {
            "dimension": dimension,
            "ordered_by": ordered_by,
            "direction": plan.order_by[0].direction.lower(),
            "population_observations": int(len(frame)),
            "displayed_rows": min(presentation_limit, int(len(frame))),
            "rows": rows,
        }

    @staticmethod
    def _trend_tests(
        frame: Any, time_column: str, numeric_columns: list[str], confidence_level: float
    ) -> dict[str, Any]:
        import pandas as pd
        from scipy.stats import kendalltau, linregress, t

        ordered = frame.sort_values(time_column)
        output: dict[str, Any] = {}
        for column in numeric_columns:
            if column == time_column:
                continue
            values = pd.to_numeric(ordered[column], errors="coerce").dropna()
            if len(values) < 3:
                continue
            x = list(range(len(values)))
            ols = linregress(x, values.astype(float).tolist())
            critical = float(t.ppf(1 - (1 - confidence_level) / 2, len(values) - 2))
            tau, kendall_p = kendalltau(x, values)
            output[column] = {
                "observations": int(len(values)),
                "slope_per_bucket": float(ols.slope),
                "intercept": float(ols.intercept),
                "r_squared": float(ols.rvalue**2),
                "p_value": float(ols.pvalue),
                "slope_confidence_interval": [
                    float(ols.slope - critical * ols.stderr),
                    float(ols.slope + critical * ols.stderr),
                ],
                "significant": bool(ols.pvalue < 1 - confidence_level),
                "mann_kendall_tau": float(tau),
                "mann_kendall_p_value": float(kendall_p),
            }
        StatisticalEngine._apply_fdr(output, "p_value", confidence_level)
        StatisticalEngine._apply_fdr(
            output, "mann_kendall_p_value", confidence_level, output_key="mann_kendall_q_value"
        )
        return output

    @staticmethod
    def _robust_anomalies(
        frame: Any, time_column: str, numeric_columns: list[str]
    ) -> dict[str, Any]:
        import numpy as np
        import pandas as pd

        output: dict[str, Any] = {}
        for column in numeric_columns:
            if column == time_column:
                continue
            series = pd.to_numeric(frame[column], errors="coerce")
            valid = series.dropna().astype(float)
            if len(valid) < 5:
                continue
            median = float(valid.median())
            mad = float(np.median(np.abs(valid - median)))
            if mad:
                scores = 0.6745 * (valid - median) / mad
                indices = scores[abs(scores) > 3.5].index
                method = "modified_z_score_mad"
            else:
                q1, q3 = [float(value) for value in valid.quantile([0.25, 0.75])]
                iqr = q3 - q1
                indices = valid[(valid < q1 - 1.5 * iqr) | (valid > q3 + 1.5 * iqr)].index
                scores = valid * 0.0
                method = "iqr_fence"
            output[column] = {
                "method": method,
                "count": int(len(indices)),
                "rows": [
                    {
                        "row_index": int(index),
                        "period": str(frame.loc[index, time_column]),
                        "value": float(frame.loc[index, column]),
                        "score": float(scores.loc[index]),
                    }
                    for index in indices[:20]
                ],
            }
        return output

    @staticmethod
    def _correlations(
        frame: Any, columns: list[str], confidence_level: float
    ) -> dict[str, Any]:
        from scipy.stats import pearsonr

        output: dict[str, Any] = {}
        for index, first in enumerate(columns):
            for second in columns[index + 1 :]:
                paired = frame[[first, second]].dropna()
                if len(paired) < 3 or paired[first].nunique() < 2 or paired[second].nunique() < 2:
                    continue
                coefficient, p_value = pearsonr(paired[first], paired[second])
                output[f"{first}:{second}"] = {
                    "coefficient": float(coefficient),
                    "p_value": float(p_value),
                    "observations": int(len(paired)),
                }
        StatisticalEngine._apply_fdr(output, "p_value", confidence_level)
        return output

    @staticmethod
    def _distribution_tests(
        frame: Any, columns: list[str], confidence_level: float
    ) -> dict[str, Any]:
        """Test marginal normality only when the test has enough observations."""
        from scipy.stats import kurtosis, normaltest, skew

        output: dict[str, Any] = {}
        alpha = 1 - confidence_level
        for column in columns:
            values = frame[column].dropna().astype(float)
            if len(values) < 8 or values.nunique() < 2:
                continue
            statistic, p_value = normaltest(values)
            output[column] = {
                "observations": int(len(values)),
                "skewness": float(skew(values, bias=False)),
                "excess_kurtosis": float(kurtosis(values, fisher=True, bias=False)),
                "dagostino_k2": float(statistic),
                "p_value": float(p_value),
                "reject_normality": bool(p_value < alpha),
                "alpha": alpha,
            }
        StatisticalEngine._apply_fdr(output, "p_value", confidence_level)
        return output

    @staticmethod
    def _regression(
        frame: Any,
        dependent: str | None,
        predictors: list[str],
        confidence_level: float,
    ) -> dict[str, Any]:
        """Fit an explanatory OLS model and expose assumption diagnostics."""
        if not dependent or not predictors:
            return {}

        import statsmodels.api as sm
        from statsmodels.stats.diagnostic import het_breuschpagan
        from statsmodels.stats.outliers_influence import variance_inflation_factor
        from statsmodels.stats.stattools import durbin_watson, jarque_bera

        columns = [dependent, *predictors]
        data = frame[columns].dropna().astype(float)
        predictors = [name for name in predictors if data[name].nunique() > 1]
        if not predictors or data[dependent].nunique() < 2:
            return {}
        data = data[[dependent, *predictors]]
        minimum_rows = max(8, len(predictors) + 3)
        if len(data) < minimum_rows:
            return {
                "status": "insufficient_sample",
                "observations": int(len(data)),
                "minimum_required": minimum_rows,
                "dependent": dependent,
                "predictors": predictors,
                "causal_interpretation": False,
            }

        design = sm.add_constant(data[predictors], has_constant="add")
        base_model = sm.OLS(data[dependent], design).fit()
        robust_model = base_model.get_robustcov_results(cov_type="HC3")
        interval = robust_model.conf_int(alpha=1 - confidence_level)
        coefficients: dict[str, Any] = {}
        names = list(design.columns)
        for index, name in enumerate(names):
            coefficients[str(name)] = {
                "estimate": float(robust_model.params[index]),
                "robust_standard_error": float(robust_model.bse[index]),
                "t_statistic": float(robust_model.tvalues[index]),
                "p_value": float(robust_model.pvalues[index]),
                "confidence_interval": [
                    float(interval[index][0]),
                    float(interval[index][1]),
                ],
            }
        StatisticalEngine._apply_fdr(
            {key: value for key, value in coefficients.items() if key != "const"},
            "p_value",
            confidence_level,
        )

        jb_stat, jb_p, residual_skew, residual_kurtosis = jarque_bera(base_model.resid)
        bp_stat, bp_p, _, _ = het_breuschpagan(base_model.resid, design)
        vif = {
            str(name): float(variance_inflation_factor(design.values, index))
            for index, name in enumerate(names)
            if name != "const"
        }
        return {
            "status": "fitted",
            "dependent": dependent,
            "predictors": predictors,
            "observations": int(base_model.nobs),
            "r_squared": float(base_model.rsquared),
            "adjusted_r_squared": float(base_model.rsquared_adj),
            "f_statistic": float(base_model.fvalue),
            "f_p_value": float(base_model.f_pvalue),
            "coefficients": coefficients,
            "diagnostics": {
                "durbin_watson": float(durbin_watson(base_model.resid)),
                "jarque_bera": float(jb_stat),
                "jarque_bera_p_value": float(jb_p),
                "residual_skewness": float(residual_skew),
                "residual_kurtosis": float(residual_kurtosis),
                "breusch_pagan": float(bp_stat),
                "breusch_pagan_p_value": float(bp_p),
                "variance_inflation_factors": vif,
            },
            "covariance_estimator": "HC3",
            "causal_interpretation": False,
            "interpretation": (
                "Associations are conditional on the included predictors and do not establish "
                "causation."
            ),
        }

    @staticmethod
    def _apply_fdr(
        results: dict[str, Any],
        p_key: str,
        confidence_level: float,
        *,
        output_key: str = "q_value",
    ) -> None:
        """Attach Benjamini-Hochberg adjusted p-values in place."""
        candidates = [
            (name, float(value[p_key]))
            for name, value in results.items()
            if isinstance(value, dict) and value.get(p_key) is not None
        ]
        if not candidates:
            return
        ranked = sorted(candidates, key=lambda item: item[1])
        adjusted: dict[str, float] = {}
        running = 1.0
        total = len(ranked)
        for rank_index in range(total - 1, -1, -1):
            name, p_value = ranked[rank_index]
            rank = rank_index + 1
            running = min(running, p_value * total / rank)
            adjusted[name] = min(1.0, running)
        alpha = 1 - confidence_level
        for name, q_value in adjusted.items():
            results[name][output_key] = q_value
            results[name][f"{output_key}_significant"] = bool(q_value < alpha)

    @staticmethod
    def _comparison(frame: Any, columns: list[str]) -> dict[str, Any]:
        if "comparison_period" not in frame.columns or len(frame) < 2:
            return {}
        output: dict[str, Any] = {}
        for column in columns:
            if column == "comparison_period":
                continue
            values = frame[column].dropna().astype(float)
            if len(values) < 2:
                continue
            latest = float(values.iloc[0])
            comparison = float(values.iloc[1])
            output[column] = {
                "latest": latest,
                "comparison": comparison,
                "absolute_change": latest - comparison,
                "change_pct": (
                    (latest - comparison) / abs(comparison) * 100 if comparison else None
                ),
            }
        return output

    @staticmethod
    def _frame_fingerprint(columns: list[str], rows: list[list[Any]]) -> str:
        payload = json.dumps({"columns": columns, "rows": rows}, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _t_critical(confidence_level: float, degrees_freedom: int) -> float:
        if degrees_freedom > 0:
            try:
                from scipy.stats import t

                return float(t.ppf(1 - (1 - confidence_level) / 2, degrees_freedom))
            except Exception:  # pragma: no cover
                pass
        return NormalDist().inv_cdf(1 - (1 - confidence_level) / 2)

    @staticmethod
    def _format_number(value: Any) -> str:
        if value is None:
            return "unavailable"
        number = float(value)
        if abs(number) >= 1_000_000:
            return f"{number:,.0f}"
        if abs(number) >= 100:
            return f"{number:,.1f}"
        return f"{number:,.2f}"

    @staticmethod
    def _frequency_label(frequency: Any, periods: int) -> str:
        labels = {
            "MS": "month",
            "M": "month",
            "ME": "month",
            "QS": "quarter",
            "Q": "quarter",
            "YS": "year",
            "Y": "year",
            "D": "day",
            "W": "week",
        }
        unit = labels.get(str(frequency), "future period")
        return f"{unit}{'' if periods == 1 else 's'}"

    @classmethod
    def _clean(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._clean(item) for item in value]
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
