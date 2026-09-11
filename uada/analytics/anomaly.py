"""
Anomaly Detection Module (P3-2)
=================================
Isolation Forest–based multivariate anomaly detection.

All heavy imports (scikit-learn) are deferred to avoid slowing startup.

Usage::

    from uada.analytics.anomaly import AnomalyDetector
    result = AnomalyDetector().detect(df, numeric_columns)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

# Minimum rows for a reliable Isolation Forest fit
_MIN_ROWS = 10
# Fallback contamination; IQR-based estimate is used when caller passes None
_DEFAULT_CONTAMINATION = 0.05


class AnomalyDetector:
    """
    Multivariate anomaly detection via scikit-learn's Isolation Forest.

    Complements the z-score outlier detection already in ResultAnalyser by
    handling non-linear, multivariate distributions that z-score misses.

    Results are returned as a :class:`uada.models.result.AnomalyResult`.
    """

    @staticmethod
    def _estimate_contamination(subset: "pd.DataFrame") -> float:
        """
        Estimate contamination fraction via Tukey's IQR fences (B-02 fix).

        A row is flagged when ANY column value falls outside
        [Q1 - 1.5·IQR, Q3 + 1.5·IQR].  The fraction of flagged rows,
        clipped to [0.01, 0.20], is returned as the IsolationForest
        contamination parameter.
        """
        import numpy as np

        q1 = subset.quantile(0.25)
        q3 = subset.quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr

        # Row is a candidate outlier if any column escapes its fence
        outside = ((subset < lower) | (subset > upper)).any(axis=1)
        fraction = float(outside.mean())

        # IsolationForest requires contamination in (0, 0.5]; cap at 0.20
        return float(np.clip(fraction, 0.01, 0.20))

    def detect(
        self,
        df: "pd.DataFrame",
        numeric_columns: list[str],
        contamination: float | None = None,
        random_state: int = 42,
    ) -> "AnomalyResult":
        """
        Run Isolation Forest on *numeric_columns* of *df*.

        Returns
        -------
        AnomalyResult
            Always returns a valid object; ``skipped=True`` when data is
            insufficient or scikit-learn is not installed.
        """
        from uada.models.result import AnomalyResult, AnomalyRow

        if not numeric_columns:
            return AnomalyResult(
                skipped=True,
                skip_reason="No numeric columns available.",
            )

        subset = df[numeric_columns].dropna()
        if len(subset) < _MIN_ROWS:
            return AnomalyResult(
                feature_columns=numeric_columns,
                skipped=True,
                skip_reason=f"Insufficient rows ({len(subset)} < {_MIN_ROWS}).",
            )

        try:
            from sklearn.ensemble import IsolationForest
        except ImportError:
            logger.warning("scikit-learn not installed; anomaly detection skipped.")
            return AnomalyResult(
                feature_columns=numeric_columns,
                skipped=True,
                skip_reason="scikit-learn not installed.",
            )

        try:
            # B-02 fix: estimate contamination from data when not specified
            if contamination is None:
                contamination = AnomalyDetector._estimate_contamination(subset)
                logger.debug(
                    "AnomalyDetector: adaptive contamination=%.4f (IQR-based)",
                    contamination,
                )

            X = subset.values
            model = IsolationForest(
                contamination=contamination,
                random_state=random_state,
                n_estimators=100,
            )
            model.fit(X)
            predictions = model.predict(X)        # 1 = normal, -1 = anomaly
            scores = model.decision_function(X)   # higher = more normal

            # Pre-compute per-column means and stds for deviation explanations
            col_means = subset.mean()
            col_stds = subset.std().replace(0, 1)  # avoid div-by-zero

            rows: list[AnomalyRow] = []
            for local_idx, (original_idx, pred, score) in enumerate(
                zip(subset.index, predictions, scores)
            ):
                row_vals = subset.iloc[local_idx]
                deviations = {
                    col: round(float((row_vals[col] - col_means[col]) / col_stds[col]), 3)
                    for col in numeric_columns
                }
                rows.append(AnomalyRow(
                    row_index=int(original_idx),
                    anomaly_score=round(float(score), 4),
                    is_anomaly=(pred == -1),
                    column_deviations=deviations,
                ))

            anomalies = [r for r in rows if r.is_anomaly]
            logger.info(
                "AnomalyDetector: %d/%d rows flagged (contamination=%.2f)",
                len(anomalies), len(rows), contamination,
            )
            return AnomalyResult(
                method="isolation_forest",
                anomaly_rows=rows,
                anomaly_count=len(anomalies),
                contamination=contamination,
                feature_columns=numeric_columns,
                skipped=False,
            )

        except Exception as exc:
            logger.warning("AnomalyDetector failed: %s", exc)
            return AnomalyResult(
                feature_columns=numeric_columns,
                skipped=True,
                skip_reason=str(exc),
            )
