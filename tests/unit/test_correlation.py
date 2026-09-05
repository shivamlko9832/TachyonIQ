"""
Unit tests for uada.analytics.correlation (P3-1)
====================================================
"""
import pytest
import pandas as pd

pytestmark = pytest.mark.unit


@pytest.fixture
def analyser():
    from uada.analytics.correlation import CorrelationAnalyser
    return CorrelationAnalyser()


class TestCorrelationAnalyser:

    def test_perfect_positive_correlation(self, analyser):
        df = pd.DataFrame({"x": range(15), "y": [v * 2 for v in range(15)]})
        r = analyser.analyse(df, ["x", "y"])
        assert not r.skipped
        assert r.min_rows_met
        assert abs(r.matrix["x"]["y"] - 1.0) < 1e-6

    def test_perfect_negative_correlation(self, analyser):
        df = pd.DataFrame({"x": range(15), "y": [-v for v in range(15)]})
        r = analyser.analyse(df, ["x", "y"])
        assert abs(r.matrix["x"]["y"] - (-1.0)) < 1e-6

    def test_skips_when_fewer_than_2_numeric_columns(self, analyser):
        df = pd.DataFrame({"x": range(15)})
        r = analyser.analyse(df, ["x"])
        assert r.skipped

    def test_skips_when_fewer_than_min_rows(self, analyser):
        df = pd.DataFrame({"x": range(5), "y": range(5)})
        r = analyser.analyse(df, ["x", "y"])
        assert r.skipped
        assert not r.min_rows_met

    def test_top_pairs_sorted_by_abs_r(self, analyser):
        n = 15
        df = pd.DataFrame({
            "a": range(n),
            "b": [v * 2 for v in range(n)],          # perfect pos
            "c": [v * 0.1 for v in range(n)],         # weaker pos
        })
        r = analyser.analyse(df, ["a", "b", "c"])
        assert not r.skipped
        assert len(r.top_pairs) >= 1
        # First pair should have highest |r|
        assert abs(float(r.top_pairs[0]["r"])) >= abs(float(r.top_pairs[-1]["r"]))

    def test_strength_labels_present_in_top_pairs(self, analyser):
        df = pd.DataFrame({"x": range(15), "y": [v * 2 for v in range(15)]})
        r = analyser.analyse(df, ["x", "y"])
        pair = r.top_pairs[0]
        assert pair["strength"] in ("very strong", "strong", "moderate", "weak", "negligible")
        assert pair["direction"] in ("positive", "negative")

    def test_matrix_is_symmetric(self, analyser):
        df = pd.DataFrame({"x": range(15), "y": range(15, 30), "z": range(30, 45)})
        r = analyser.analyse(df, ["x", "y", "z"])
        for a in ["x", "y", "z"]:
            for b in ["x", "y", "z"]:
                assert abs(r.matrix[a][b] - r.matrix[b][a]) < 1e-6

    def test_spearman_method(self, analyser):
        df = pd.DataFrame({
            "rank": range(15),
            "score": [v ** 2 for v in range(15)],  # monotonic, not linear
        })
        r_spearman = analyser.analyse(df, ["rank", "score"], method="spearman")
        assert r_spearman.method == "spearman"
        assert not r_spearman.skipped
        assert abs(r_spearman.matrix["rank"]["score"] - 1.0) < 1e-3

    def test_returns_valid_object_on_all_same_values(self, analyser):
        """Zero-variance column: corr is NaN — should be excluded from matrix."""
        df = pd.DataFrame({"x": [5.0] * 15, "y": range(15)})
        # Should not raise; skipped or has partial matrix
        r = analyser.analyse(df, ["x", "y"])
        assert isinstance(r.skipped, bool)
