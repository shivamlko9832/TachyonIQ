"""
Result Critic (pipeline step 7)
==================================
Fully deterministic 8-dimension evaluator for query results.

Sits between query execution and insight generation.  Scores the result
0.0–1.0; a score ≥ SUFFICIENCY_THRESHOLD (0.8) means the result is
SUFFICIENT and the pipeline may proceed.  Below that threshold the
Replanner (Step 6) is triggered.

Design constraints (from CC-08):
- Fully deterministic; zero LLM calls.
- Stateless; safe to construct once and reuse across requests.
- Cannot fail: every exception is caught and returns score=0.0, which is
  the safest default because it triggers replanning rather than passing a
  bad result through to the user.

The 8 dimensions and their weights sum to 1.0:

  C1  empty_result            0.25   Zero rows returned
  C2  unexpected_cardinality  0.20   Fan-out or over-aggregation
  C3  missing_time_axis       0.10   Time-series intent but no time column
  C4  suspicious_joins        0.10   Likely join explosion
  C5  aggregation_mismatch    0.15   COUNT/SUM confusion or ID-column sum
  C6  insufficient_sample     0.10   n < 30 for statistical intents
  C7  intent_alignment        0.05   Expected aliases absent from result
  C8  result_anomaly          0.05   Extreme outlier distribution flagged
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from uada.db.interface import QueryExecutionResult
    from uada.models.query_plan import QueryPlan

logger = logging.getLogger(__name__)

# ── Weight table (must sum to 1.0) ───────────────────────────────────────────

_DIM_WEIGHTS: dict[str, float] = {
    "empty_result":            0.25,
    "unexpected_cardinality":  0.20,
    "missing_time_axis":       0.10,
    "suspicious_joins":        0.10,
    "aggregation_mismatch":    0.15,
    "insufficient_sample":     0.10,
    "intent_alignment":        0.05,
    "result_anomaly":          0.05,
}

assert abs(sum(_DIM_WEIGHTS.values()) - 1.0) < 1e-9, "Dimension weights must sum to 1.0"

# Intent types that require a time axis in the result
_TIME_AXIS_INTENTS: frozenset[str] = frozenset({"time_series"})

# Intent types that require a minimum sample size for valid statistical tests
_STATISTICAL_INTENTS: frozenset[str] = frozenset({
    "comparison", "diagnostic", "time_series",
})


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class DimensionResult:
    """Score and diagnostic information for one critic dimension."""

    name: str
    """Dimension key matching _DIM_WEIGHTS."""

    passed: bool
    """True when this dimension found no issue."""

    score: float
    """0.0–1.0 contribution from this dimension (before weighting)."""

    flag: str | None
    """Human-readable description of the problem, or None if passed."""

    replan_hint: str | None
    """Actionable instruction for the Replanner, or None if passed."""


@dataclass
class CriticResult:
    """Composite result returned by :meth:`ResultCritic.critique`."""

    score: float
    """Weighted composite score 0.0–1.0."""

    is_sufficient: bool
    """``True`` when score ≥ ``ResultCritic.SUFFICIENCY_THRESHOLD`` (0.8)."""

    dimensions: list[DimensionResult]
    """Per-dimension breakdown in evaluation order (C1–C8)."""

    replan_hints: list[str]
    """Aggregated actionable hints from all failing dimensions; empty when sufficient."""

    insufficient_sample: bool
    """``True`` when C6 fired — statistical tests may be invalid."""

    anomaly_flagged: bool
    """``True`` when C8 fired — unusual value distribution detected."""

    empty_result: bool
    """``True`` when C1 fired — zero rows returned."""


# ── ResultCritic ──────────────────────────────────────────────────────────────

class ResultCritic:
    """Pipeline step 7: deterministic 8-dimension result evaluator.

    Construct once at app startup (it is stateless) and reuse across requests.
    """

    SUFFICIENCY_THRESHOLD: ClassVar[float] = 0.8
    MINIMUM_SAMPLE_SIZE: ClassVar[int] = 30

    # Thresholds for cardinality / anomaly checks
    _FAN_OUT_ROW_THRESHOLD: ClassVar[int] = 5_000
    _ANOMALY_IQR_MULTIPLIER: ClassVar[float] = 3.0
    _ANOMALY_OUTLIER_RATE: ClassVar[float] = 0.10  # >10 % of rows are outliers

    # ── Public API ────────────────────────────────────────────────────────────

    def critique(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
        *,
        intent_type: str | None = None,
    ) -> CriticResult:
        """Evaluate *result* against *plan* across all 8 dimensions.

        Args:
            result: The ``QueryExecutionResult`` from the database adapter.
            plan: The ``QueryPlan`` that produced the result — provides
                expected aliases, join graph, and time resolution info.
            intent_type: The ``QuestionType.value`` string (e.g.
                ``"time_series"``) from the AnalyticalIntent.  When ``None``
                the intent-sensitive checks (C3, C6, C7) use safe defaults.

        Returns:
            A :class:`CriticResult` with composite score and full breakdown.
            Never raises — exceptions yield ``score=0.0`` (safe-fail default).
        """
        try:
            return self._run_critique(result, plan, intent_type)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ResultCritic internal error (returning score=0.0 as safe default): %s",
                exc, exc_info=True,
            )
            safe_dim = DimensionResult(
                name="empty_result", passed=False, score=0.0,
                flag=f"ResultCritic internal error: {exc}",
                replan_hint="An unexpected error occurred during result evaluation. Replanning.",
            )
            return CriticResult(
                score=0.0, is_sufficient=False,
                dimensions=[safe_dim], replan_hints=[safe_dim.replan_hint],
                insufficient_sample=False, anomaly_flagged=False, empty_result=True,
            )

    # ── Internal orchestration ────────────────────────────────────────────────

    def _run_critique(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
        intent_type: str | None,
    ) -> CriticResult:
        dims: list[DimensionResult] = [
            self._check_empty_result(result),                               # C1
            self._check_unexpected_cardinality(result, plan, intent_type),  # C2
            self._check_missing_time_axis(result, plan, intent_type),       # C3
            self._check_suspicious_joins(result, plan),                     # C4
            self._check_aggregation_mismatch(result, plan),                 # C5
            self._check_insufficient_sample(result, intent_type),           # C6
            self._check_intent_alignment(result, plan),                     # C7
            self._check_result_anomaly(result),                             # C8
        ]

        score = sum(d.score * _DIM_WEIGHTS[d.name] for d in dims)
        score = round(min(1.0, max(0.0, score)), 4)

        replan_hints = [d.replan_hint for d in dims if d.replan_hint is not None]
        insufficient_sample = any(d.name == "insufficient_sample" and not d.passed for d in dims)
        anomaly_flagged = any(d.name == "result_anomaly" and not d.passed for d in dims)
        empty_result = any(d.name == "empty_result" and not d.passed for d in dims)

        # An empty result cannot support a trustworthy answer regardless of how
        # well the other structural checks happen to score.  Treat this as a
        # hard failure so the orchestrator can replan instead of presenting a
        # polished response backed by no evidence.
        if empty_result:
            score = 0.0

        is_sufficient = score >= self.SUFFICIENCY_THRESHOLD

        if not is_sufficient:
            logger.warning(
                "ResultCritic: score=%.4f < %.2f (INSUFFICIENT). hints=%s",
                score, self.SUFFICIENCY_THRESHOLD, replan_hints,
            )
        else:
            logger.info("ResultCritic: score=%.4f (SUFFICIENT).", score)

        return CriticResult(
            score=score,
            is_sufficient=is_sufficient,
            dimensions=dims,
            replan_hints=replan_hints,
            insufficient_sample=insufficient_sample,
            anomaly_flagged=anomaly_flagged,
            empty_result=empty_result,
        )

    # ── Dimension checks (C1–C8) ──────────────────────────────────────────────

    def _check_empty_result(self, result: QueryExecutionResult) -> DimensionResult:
        """C1 — Zero rows returned."""
        if result.row_count > 0:
            return DimensionResult("empty_result", True, 1.0, None, None)
        return DimensionResult(
            name="empty_result",
            passed=False,
            score=0.0,
            flag="Query returned 0 rows.",
            replan_hint=(
                "Check whether the time filter is too restrictive, the date range has no data, "
                "or the wrong table was selected."
            ),
        )

    def _check_unexpected_cardinality(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
        intent_type: str | None,
    ) -> DimensionResult:
        """C2 — Fan-out (too many rows) or over-aggregation (too few rows for the intent)."""
        row_count = result.row_count
        has_joins = len(plan.joins) > 0
        has_dimensions = len(plan.dimensions) > 0

        # Fan-out heuristic: result is truncated at the row limit AND there are
        # joins but no GROUP BY dimensions — strongly suggests a join explosion.
        fan_out = result.is_truncated and has_joins and not has_dimensions
        if fan_out:
            return DimensionResult(
                name="unexpected_cardinality",
                passed=False,
                score=0.0,
                flag=(
                    f"Possible fan-out: result truncated at {row_count:,} rows with "
                    f"{len(plan.joins)} join(s) and no aggregation dimensions."
                ),
                replan_hint=(
                    "Add a COUNT DISTINCT guard on the grain key, or replan without the "
                    "fan-out join."
                ),
            )

        # Over-aggregation heuristic: a time-series intent with a time resolution
        # specified in the plan produced only a single row — the time bucket was
        # likely dropped from the GROUP BY.
        is_time_intent = intent_type in _TIME_AXIS_INTENTS
        over_agg = is_time_intent and plan.time_resolution is not None and row_count == 1
        if over_agg:
            return DimensionResult(
                name="unexpected_cardinality",
                passed=False,
                score=0.5,  # Partial — data exists, time bucketing is wrong
                flag=(
                    f"Possible over-aggregation: time-series query returned 1 row "
                    f"despite a time resolution in the plan."
                ),
                replan_hint=(
                    "Ensure the time bucket column (DATE_TRUNC or equivalent) is "
                    "included in both SELECT and GROUP BY."
                ),
            )

        return DimensionResult("unexpected_cardinality", True, 1.0, None, None)

    def _check_missing_time_axis(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
        intent_type: str | None,
    ) -> DimensionResult:
        """C3 — Time-series intent but no recognisable time column in the result."""
        if intent_type not in _TIME_AXIS_INTENTS or plan.time_resolution is None:
            return DimensionResult("missing_time_axis", True, 1.0, None, None)

        time_keywords = frozenset({"date", "month", "week", "quarter", "year", "period", "time", "day"})
        has_time_col = any(
            any(kw in col.lower() for kw in time_keywords)
            for col in result.column_names
        )
        if has_time_col:
            return DimensionResult("missing_time_axis", True, 1.0, None, None)

        return DimensionResult(
            name="missing_time_axis",
            passed=False,
            score=0.0,
            flag="Time-series query result has no identifiable time-axis column.",
            replan_hint=(
                "Add the time dimension (DATE_TRUNC or equivalent) to SELECT and GROUP BY."
            ),
        )

    def _check_suspicious_joins(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
    ) -> DimensionResult:
        """C4 — Join structure that may produce unreliable results."""
        if not plan.joins:
            return DimensionResult("suspicious_joins", True, 1.0, None, None)

        # Three warning signals combined: many joins, no aggregation, high row count.
        many_joins = len(plan.joins) > 2
        no_dims = len(plan.dimensions) == 0
        high_rows = result.row_count > self._FAN_OUT_ROW_THRESHOLD

        suspicious = many_joins and no_dims and high_rows
        if suspicious:
            return DimensionResult(
                name="suspicious_joins",
                passed=False,
                score=0.0,
                flag=(
                    f"Suspicious join pattern: {len(plan.joins)} joins, "
                    f"{result.row_count:,} rows, no aggregation dimensions."
                ),
                replan_hint=(
                    "Validate JOIN conditions against the semantic model; add COUNT DISTINCT "
                    "on the grain key or GROUP BY to prevent row explosion."
                ),
            )

        return DimensionResult("suspicious_joins", True, 1.0, None, None)

    def _check_aggregation_mismatch(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
    ) -> DimensionResult:
        """C5 — COUNT/SUM confusion or SUM applied to likely-ID columns."""
        col_names_lower = [c.lower() for c in result.column_names]
        col_types_lower = [t.lower() for t in result.column_types] if result.column_types else []
        mismatches: list[str] = []

        for measure in plan.measures:
            expr_upper = measure.sql_expression.upper()
            alias_lower = measure.output_alias.lower()

            # COUNT measure producing a non-numeric type is a clear error.
            if "COUNT" in expr_upper:
                try:
                    idx = col_names_lower.index(alias_lower)
                    if col_types_lower:
                        col_type = col_types_lower[idx]
                        text_like = any(t in col_type for t in ("char", "text", "varchar"))
                        if text_like:
                            mismatches.append(
                                f"COUNT measure '{alias_lower}' has text type '{col_type}'"
                            )
                except ValueError:
                    pass  # alias absent — caught by C7 intent_alignment

            # SUM applied to a column whose name suggests it is an identifier or code
            # rarely produces meaningful business numbers.
            if "SUM" in expr_upper:
                id_suffixes = ("_id", "_key", "_code", "_num", "_ref")
                if any(alias_lower.endswith(s) for s in id_suffixes):
                    mismatches.append(
                        f"SUM applied to likely-ID column '{alias_lower}' "
                        f"— may produce meaningless total"
                    )

        if mismatches:
            return DimensionResult(
                name="aggregation_mismatch",
                passed=False,
                score=0.5,
                flag="; ".join(mismatches),
                replan_hint=(
                    "Verify metric formula: check COUNT vs SUM vs AVG and confirm "
                    "the target column data type."
                ),
            )

        return DimensionResult("aggregation_mismatch", True, 1.0, None, None)

    def _check_insufficient_sample(
        self,
        result: QueryExecutionResult,
        intent_type: str | None,
    ) -> DimensionResult:
        """C6 — Fewer than MINIMUM_SAMPLE_SIZE rows for statistical intent."""
        if intent_type not in _STATISTICAL_INTENTS:
            return DimensionResult("insufficient_sample", True, 1.0, None, None)

        if result.row_count >= self.MINIMUM_SAMPLE_SIZE:
            return DimensionResult("insufficient_sample", True, 1.0, None, None)

        return DimensionResult(
            name="insufficient_sample",
            passed=False,
            score=0.3,  # Partial — data exists, just insufficient for statistics
            flag=(
                f"Only {result.row_count} rows; statistical tests require "
                f"n ≥ {self.MINIMUM_SAMPLE_SIZE}."
            ),
            replan_hint=(
                f"Widen the time range or reduce filter specificity to obtain at "
                f"least {self.MINIMUM_SAMPLE_SIZE} data points before running "
                f"statistical tests."
            ),
        )

    def _check_intent_alignment(
        self,
        result: QueryExecutionResult,
        plan: QueryPlan,
    ) -> DimensionResult:
        """C7 — Expected column aliases absent from the result."""
        expected = {m.output_alias.lower() for m in plan.measures} | {
            d.output_alias.lower() for d in plan.dimensions
        }
        if not expected:
            return DimensionResult("intent_alignment", True, 1.0, None, None)

        actual = {c.lower() for c in result.column_names}
        missing = expected - actual

        if not missing:
            return DimensionResult("intent_alignment", True, 1.0, None, None)

        completeness = 1.0 - len(missing) / len(expected)
        return DimensionResult(
            name="intent_alignment",
            passed=False,
            score=round(max(0.0, completeness), 4),
            flag=f"Result is missing expected column(s): {', '.join(sorted(missing))}.",
            replan_hint=(
                "Ensure SELECT aliases match the plan's output_alias fields exactly."
            ),
        )

    def _check_result_anomaly(self, result: QueryExecutionResult) -> DimensionResult:
        """C8 — Extreme value distribution in numeric columns (IQR-based).

        This dimension *flags* rather than blocks: the score deduction is
        small (0.2) so it alone cannot push a good result below threshold.
        """
        if result.row_count < 5:
            # Too few rows to estimate distribution reliably.
            return DimensionResult("result_anomaly", True, 1.0, None, None)

        flagged_cols: list[str] = []
        for col_idx, col_name in enumerate(result.column_names):
            col_type = (
                result.column_types[col_idx].lower()
                if result.column_types
                else ""
            )
            numeric_indicators = ("int", "float", "numeric", "decimal", "double", "real", "number")
            if not any(ind in col_type for ind in numeric_indicators):
                continue

            try:
                vals: list[float] = [
                    float(row[col_idx])
                    for row in result.rows
                    if row[col_idx] is not None
                ]
                if len(vals) < 4:
                    continue

                sorted_vals = sorted(vals)
                n = len(sorted_vals)
                q1 = sorted_vals[n // 4]
                q3 = sorted_vals[(3 * n) // 4]
                iqr = q3 - q1

                if iqr == 0:
                    continue  # All values identical — not an anomaly signal

                lower_fence = q1 - self._ANOMALY_IQR_MULTIPLIER * iqr
                upper_fence = q3 + self._ANOMALY_IQR_MULTIPLIER * iqr
                outlier_count = sum(1 for v in vals if v < lower_fence or v > upper_fence)

                if outlier_count / len(vals) > self._ANOMALY_OUTLIER_RATE:
                    flagged_cols.append(col_name)

            except (TypeError, ValueError):
                pass  # Non-numeric at runtime despite type hint — skip silently

        if flagged_cols:
            return DimensionResult(
                name="result_anomaly",
                passed=False,
                score=0.8,  # Small deduction — flag only, does not block on its own
                flag=(
                    f"Unusual value distribution in column(s): "
                    f"{', '.join(flagged_cols)} (>10% of values outside 3×IQR fence)."
                ),
                replan_hint=(
                    "Review result values for data quality issues before drawing conclusions."
                ),
            )

        return DimensionResult("result_anomaly", True, 1.0, None, None)
