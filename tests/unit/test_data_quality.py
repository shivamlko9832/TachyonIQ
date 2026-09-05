"""
Unit tests for uada.analytics.data_quality (P4-A-1).

No LLM, no database — only pandas.
"""

from __future__ import annotations

import pytest
import pandas as pd

from uada.analytics.data_quality import DataQualityChecker
from uada.models.result import DataQualityReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_df(n: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"a": list(range(n)), "b": [float(v) for v in range(n)]})


def _null_df(null_pct: float = 0.6, n: int = 20) -> pd.DataFrame:
    vals = [None if i < int(n * null_pct) else float(i) for i in range(n)]
    return pd.DataFrame({"x": vals})


def _dup_df(n: int = 20) -> pd.DataFrame:
    """Half the rows are duplicates."""
    half = n // 2
    rows = list(range(half)) * 2
    return pd.DataFrame({"v": rows})


def _mixed_type_df(n: int = 20) -> pd.DataFrame:
    """Object column with ~50% numeric-looking, ~50% text."""
    vals = [str(i) if i % 2 == 0 else f"X{i}" for i in range(n)]
    return pd.DataFrame({"label": vals})


def _empty_str_df(n: int = 20) -> pd.DataFrame:
    """Column where >30% values are empty strings."""
    vals = ["" if i < int(n * 0.4) else f"val{i}" for i in range(n)]
    return pd.DataFrame({"name": vals})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDataQualityChecker:

    def test_returns_data_quality_report(self):
        result = DataQualityChecker().check(_clean_df())
        assert isinstance(result, DataQualityReport)

    def test_clean_df_not_skipped(self):
        result = DataQualityChecker().check(_clean_df())
        assert result.skipped is False

    def test_clean_df_full_score(self):
        result = DataQualityChecker().check(_clean_df())
        assert result.overall_quality_score == 100.0

    def test_clean_df_no_issues(self):
        result = DataQualityChecker().check(_clean_df())
        assert len(result.issues) == 0

    def test_null_rates_populated(self):
        df = _null_df(null_pct=0.5, n=20)
        result = DataQualityChecker().check(df)
        assert "x" in result.null_rates
        assert result.null_rates["x"] > 0

    def test_high_null_column_flagged(self):
        df = _null_df(null_pct=0.6, n=20)
        result = DataQualityChecker().check(df)
        assert any(i.issue_type == "high_nulls" for i in result.issues)
        assert any(i.severity == "high" for i in result.issues)

    def test_all_null_column_flagged(self):
        df = pd.DataFrame({"x": [None] * 20})
        result = DataQualityChecker().check(df)
        assert any(i.issue_type == "all_nulls" for i in result.issues)

    def test_duplicate_rows_detected(self):
        result = DataQualityChecker().check(_dup_df())
        assert result.duplicate_row_count > 0
        assert any(i.issue_type == "duplicate_rows" for i in result.issues)

    def test_duplicate_pct_correct(self):
        df = _dup_df(n=20)  # 10 duplicates in 20 rows = 50%
        result = DataQualityChecker().check(df)
        assert result.duplicate_row_pct > 0.0

    def test_empty_strings_detected(self):
        result = DataQualityChecker().check(_empty_str_df())
        assert "name" in result.empty_string_counts
        assert result.empty_string_counts["name"] > 0

    def test_mixed_type_column_flagged(self):
        result = DataQualityChecker().check(_mixed_type_df())
        type_issues = [i for i in result.issues if i.issue_type == "type_mismatch"]
        assert len(type_issues) >= 1

    def test_score_decreases_with_issues(self):
        clean_score = DataQualityChecker().check(_clean_df()).overall_quality_score
        null_score = DataQualityChecker().check(_null_df(0.6)).overall_quality_score
        assert null_score < clean_score

    def test_empty_dataframe_skipped(self):
        result = DataQualityChecker().check(pd.DataFrame())
        assert result.skipped is True
        assert result.skip_reason is not None

    def test_summary_is_non_empty(self):
        result = DataQualityChecker().check(_clean_df())
        assert isinstance(result.summary, str)
        assert len(result.summary) > 0

    def test_column_subset_respected(self):
        df = _clean_df()
        result = DataQualityChecker().check(df, columns=["a"])
        assert "a" in result.null_rates
        assert "b" not in result.null_rates

    def test_medium_null_severity(self):
        df = _null_df(null_pct=0.25, n=20)
        result = DataQualityChecker().check(df)
        medium_issues = [i for i in result.issues if i.severity == "medium"]
        assert len(medium_issues) >= 1
