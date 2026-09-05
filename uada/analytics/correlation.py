"""
Correlation Analysis Module (P3-1)
====================================
Standalone pairwise correlation engine.

Supports Pearson (default) and Spearman correlations.
All heavy imports (scipy.stats) are lazy to avoid slowing startup.

Usage::

    from uada.analytics.correlation import CorrelationAnalyser
    result = CorrelationAnalyser().analyse(df, numeric_columns)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

# Minimum rows for a reliable correlation estimate
_MIN_ROWS = 10
# Maximum pairs to include in top_pairs (by |r|)
_TOP_N_PAIRS = 10

_STRENGTH_THRESHOLDS = [
    (0.9, "very strong"),
    (0.7, "strong"),
    (0.5, "moderate"),
    (0.3, "weak"),
    (0.0, "negligible"),
]


def _strength_label(r: float) -> str:
    abs_r = abs(r)
    for threshold, label in _STRENGTH_THRESHOLDS:
        if abs_r >= threshold:
            return label
    return "negligible"


class CorrelationAnalyser:
    """
    Computes a full pairwise correlation matrix and ranks the most
    correlated column pairs.

    Results are returned as a :class:`uada.models.result.CorrelationResult`.
    All scipy imports are deferred so this module loads without scipy installed.
    """

    def analyse(
        self,
        df: "pd.DataFrame",
        numeric_columns: list[str],
        method: str = "pearson",
    ) -> "CorrelationResult":
        """
        Compute pairwise correlations across *numeric_columns* in *df*.

        Parameters
        ----------
        df:
            DataFrame containing the query result rows.
        numeric_columns:
            Column names to include in the correlation matrix.
        method:
            ``"pearson"`` (default) or ``"spearman"``.

        Returns
        -------
        CorrelationResult
            Always returns a valid object; ``skipped=True`` when the data is
            insufficient or an error occurs.
        """
        from uada.models.result import CorrelationResult  # local import avoids circularity

        if len(numeric_columns) < 2:
            return CorrelationResult(method=method, skipped=True)

        subset = df[numeric_columns].dropna()
        if len(subset) < _MIN_ROWS:
            return CorrelationResult(
                method=method,
                skipped=True,
                min_rows_met=False,
            )

        try:
            corr_df = subset.corr(method=method)  # type: ignore[call-overload]
        except Exception as exc:
            logger.warning("CorrelationAnalyser failed: %s", exc)
            return CorrelationResult(method=method, skipped=True)

        # Build full matrix as nested dicts
        matrix: dict[str, dict[str, float]] = {}
        for col_a in numeric_columns:
            if col_a not in corr_df.columns:
                continue
            row: dict[str, float] = {}
            for col_b in numeric_columns:
                if col_b not in corr_df.columns:
                    continue
                val = corr_df.loc[col_a, col_b]
                if not (val != val):  # NaN check without numpy
                    row[col_b] = round(float(val), 4)
            if row:
                matrix[col_a] = row

        # Build top pairs (upper triangle only, sorted by |r|)
        cols = [c for c in numeric_columns if c in corr_df.columns]
        pairs: list[dict[str, object]] = []
        for i, col_a in enumerate(cols):
            for col_b in cols[i + 1:]:
                r = corr_df.loc[col_a, col_b]
                if r != r:  # NaN
                    continue
                pairs.append({
                    "col_a": col_a,
                    "col_b": col_b,
                    "r": round(float(r), 4),
                    "strength": _strength_label(r),
                    "direction": "positive" if r >= 0 else "negative",
                })

        pairs.sort(key=lambda p: abs(float(p["r"])), reverse=True)  # type: ignore[arg-type]
        top_pairs = pairs[:_TOP_N_PAIRS]

        return CorrelationResult(
            method=method,
            matrix=matrix,
            top_pairs=top_pairs,
            min_rows_met=True,
            skipped=False,
        )
