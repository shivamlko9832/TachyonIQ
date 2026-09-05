"""
Data Quality Checker (P4-A-1, GAP-15)
======================================
Deterministic. Runs over a Pandas DataFrame and produces a DataQualityReport
containing null rates, duplicate detection, type consistency checks, and an
overall quality score (0–100).

No LLM, no external I/O. Safe to call after every query execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from uada.models.result import ColumnQualityIssue, DataQualityReport

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_MIN_ROWS = 1
_HIGH_NULL_THRESHOLD = 0.50    # >= 50 % nulls → high severity
_MEDIUM_NULL_THRESHOLD = 0.20  # >= 20 % nulls → medium severity
_HIGH_EMPTY_THRESHOLD = 0.30   # >= 30 % empty strings → medium issue


class DataQualityChecker:
    """
    Pipeline utility: assess data quality of a query result DataFrame.

    Usage::

        report = DataQualityChecker().check(df)
    """

    def check(
        self,
        df: pd.DataFrame,
        columns: list[str] | None = None,
    ) -> DataQualityReport:
        """
        Analyse *df* for data quality issues.

        Parameters
        ----------
        df:
            DataFrame produced from the query result.
        columns:
            Subset of columns to inspect. Defaults to all columns.
        """
        if df is None or len(df) == 0:
            return DataQualityReport(
                skipped=True,
                skip_reason="Empty result set — no quality checks possible.",
            )

        cols = columns if columns else list(df.columns)
        # Only keep columns that actually exist
        cols = [c for c in cols if c in df.columns]

        if not cols:
            return DataQualityReport(
                skipped=True,
                skip_reason="No valid columns to inspect.",
            )

        issues: list[ColumnQualityIssue] = []
        null_rates: dict[str, float] = {}
        empty_string_counts: dict[str, int] = {}

        n = len(df)

        # ── Per-column checks ────────────────────────────────────────────────
        for col in cols:
            series = df[col]
            null_count = int(series.isna().sum())
            null_rate = null_count / n
            null_rates[col] = round(null_rate, 4)

            # All-null column
            if null_rate == 1.0:
                issues.append(ColumnQualityIssue(
                    column=col,
                    issue_type="all_nulls",
                    severity="high",
                    detail=f"Column '{col}' contains only NULL values.",
                ))
            elif null_rate >= _HIGH_NULL_THRESHOLD:
                issues.append(ColumnQualityIssue(
                    column=col,
                    issue_type="high_nulls",
                    severity="high",
                    detail=f"Column '{col}' is {null_rate*100:.1f}% NULL ({null_count}/{n} rows).",
                ))
            elif null_rate >= _MEDIUM_NULL_THRESHOLD:
                issues.append(ColumnQualityIssue(
                    column=col,
                    issue_type="high_nulls",
                    severity="medium",
                    detail=f"Column '{col}' is {null_rate*100:.1f}% NULL ({null_count}/{n} rows).",
                ))

            # Empty string check for string/object columns (pandas 2+ uses StringDtype)
            if pd.api.types.is_string_dtype(series) or pd.api.types.is_object_dtype(series):
                try:
                    empty_count = int((series.dropna().astype(str).str.strip() == "").sum())
                    if empty_count > 0:
                        empty_string_counts[col] = empty_count
                        empty_rate = empty_count / n
                        if empty_rate >= _HIGH_EMPTY_THRESHOLD:
                            issues.append(ColumnQualityIssue(
                                column=col,
                                issue_type="empty_strings",
                                severity="medium",
                                detail=(
                                    f"Column '{col}' has {empty_count} empty/blank string(s) "
                                    f"({empty_rate*100:.1f}% of rows)."
                                ),
                            ))
                except Exception:  # noqa: BLE001
                    pass

            # Type-consistency check: mixed numeric-looking values in string/object columns
            if (pd.api.types.is_string_dtype(series) or pd.api.types.is_object_dtype(series)) and null_rate < 1.0:
                non_null = series.dropna()
                if len(non_null) >= 2:
                    try:
                        pd.to_numeric(non_null, errors="raise")
                    except (ValueError, TypeError):
                        # Check if SOME values are numeric-looking but others aren't
                        coerced = pd.to_numeric(non_null, errors="coerce")
                        numeric_pct = coerced.notna().mean()
                        if 0.1 < numeric_pct < 0.9:
                            issues.append(ColumnQualityIssue(
                                column=col,
                                issue_type="type_mismatch",
                                severity="medium",
                                detail=(
                                    f"Column '{col}' has mixed types: "
                                    f"{numeric_pct*100:.0f}% numeric-looking values "
                                    "mixed with text."
                                ),
                            ))

        # ── Duplicate rows ───────────────────────────────────────────────────
        try:
            dup_mask = df.duplicated(keep="first")
            dup_count = int(dup_mask.sum())
            dup_pct = round(dup_count / n, 4)
            if dup_count > 0:
                severity = "high" if dup_pct >= 0.20 else "low"
                issues.append(ColumnQualityIssue(
                    column="(all columns)",
                    issue_type="duplicate_rows",
                    severity=severity,
                    detail=(
                        f"{dup_count} duplicate row(s) detected "
                        f"({dup_pct*100:.1f}% of result set)."
                    ),
                ))
        except Exception:  # noqa: BLE001
            dup_count, dup_pct = 0, 0.0

        # ── Overall quality score ────────────────────────────────────────────
        # Start at 100, deduct per issue
        score = 100.0
        _deductions = {"high": 25.0, "medium": 10.0, "low": 3.0}
        for issue in issues:
            score -= _deductions.get(issue.severity, 5.0)
        score = max(0.0, round(score, 1))

        # ── Summary sentence ─────────────────────────────────────────────────
        if not issues:
            summary = f"No data quality issues detected across {len(cols)} column(s)."
        else:
            high_count = sum(1 for i in issues if i.severity == "high")
            med_count = sum(1 for i in issues if i.severity == "medium")
            parts = []
            if high_count:
                parts.append(f"{high_count} high-severity")
            if med_count:
                parts.append(f"{med_count} medium-severity")
            low_count = len(issues) - high_count - med_count
            if low_count:
                parts.append(f"{low_count} low-severity")
            summary = f"{len(issues)} issue(s) found: {', '.join(parts)}. Quality score: {score}/100."

        return DataQualityReport(
            null_rates=null_rates,
            duplicate_row_count=dup_count,
            duplicate_row_pct=dup_pct,
            empty_string_counts=empty_string_counts,
            issues=issues,
            overall_quality_score=score,
            summary=summary,
            skipped=False,
        )
