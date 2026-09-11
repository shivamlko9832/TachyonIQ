"""
Replanner — deterministic QueryPlan modification on failed ResultCritic evaluation.
=====================================================================================

When ``ResultCritic.is_sufficient`` is ``False``, the Replanner analyses the
failed dimension checks and applies targeted, deterministic mutations to the
``QueryPlan`` so a second SQL-generation attempt has a better chance of
returning a sufficient result.

Design constraints
------------------
- **Deterministic**: no LLM, no randomness.
- **Stateless**: same inputs → same output every call.
- **Never raises**: any exception returns the original plan unchanged with a
  note in ``ReplanResult.skipped``.
- **Single responsibility**: mutate the plan; the orchestrator decides whether
  to retry.

Dimension → mutation map
------------------------
Dimension               Action
─────────────────────   ──────────────────────────────────────────────────────
empty_result            Drop the last resolved filter; add a plan note to
                        reconsider the time range.
unexpected_cardinality  Fan-out: add DISTINCT-guard plan note + defensive LIMIT.
(fan-out)
unexpected_cardinality  Over-agg: inject note to add time-bucket grouping.
(over-aggregation)
missing_time_axis       Add note to include DATE_TRUNC column in SELECT + GROUP BY.
suspicious_joins        Prune joins beyond _MAX_JOINS; add safety note.
aggregation_mismatch    Add note to verify metric formula + column type.
insufficient_sample     Drop all non-time filters; remove LIMIT.
intent_alignment        Add alias-alignment note referencing plan output_alias.
result_anomaly          No mutation (flag-only — data quality, not plan defect).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uada.models.query_plan import QueryPlan
    from uada.pipeline.result_critic import CriticResult

logger = logging.getLogger(__name__)


# ── Result dataclass ───────────────────────────────────────────────────────────


@dataclass
class ReplanResult:
    """Outcome of a :meth:`Replanner.replan` call."""

    plan: "QueryPlan"
    """Modified QueryPlan (deep copy). Identical to the original when nothing applied."""

    applied: list[str] = field(default_factory=list)
    """Human-readable descriptions of mutations applied (for logging + traces)."""

    skipped: list[str] = field(default_factory=list)
    """Hints that could not be addressed deterministically."""

    original_hints: list[str] = field(default_factory=list)
    """Raw ``replan_hints`` strings from ``CriticResult`` (for tracing)."""

    @property
    def did_replan(self) -> bool:
        """``True`` when at least one mutation was successfully applied."""
        return bool(self.applied)


# ── Replanner class ────────────────────────────────────────────────────────────


class Replanner:
    """
    Deterministic ``QueryPlan`` mutator triggered by a failed
    ``ResultCritic`` evaluation.

    Usage::

        replanner = Replanner()
        replan_result = replanner.replan(critic_result, original_plan)
        if replan_result.did_replan:
            new_sql = await sql_generator.generate(replan_result.plan)
    """

    # Defensive LIMIT applied when a fan-out is detected and no limit is set.
    _DEFAULT_FANOUT_LIMIT: int = 1_000

    # Maximum number of JOINs to keep when suspicious_joins fires.
    _MAX_JOINS: int = 2

    # Minimum sample size referenced in plan notes (mirrors ResultCritic constant).
    _MIN_SAMPLE_SIZE: int = 30

    def replan(self, critic: "CriticResult", plan: "QueryPlan") -> ReplanResult:
        """
        Produce a mutated :class:`~uada.models.query_plan.QueryPlan` guided
        by a :class:`~uada.pipeline.result_critic.CriticResult`.

        Parameters
        ----------
        critic:
            The ``CriticResult`` produced by ``ResultCritic.critique()``.
        plan:
            The original ``QueryPlan`` that produced the insufficient result.

        Returns
        -------
        ReplanResult
            Always returns a valid ``ReplanResult``; never raises.  When an
            unexpected error occurs the original plan is returned with a note
            in ``skipped``.
        """
        try:
            return self._apply(critic, plan)
        except Exception as exc:  # pragma: no cover — safety-net, not expected path
            logger.warning(
                "Replanner internal error — returning original plan unchanged: %s",
                exc,
                exc_info=True,
            )
            return ReplanResult(
                plan=plan,
                applied=[],
                skipped=["Replanner raised unexpectedly; original plan retained."],
                original_hints=list(critic.replan_hints),
            )

    # ── internals ─────────────────────────────────────────────────────────────

    def _apply(self, critic: "CriticResult", plan: "QueryPlan") -> ReplanResult:
        """Core mutation logic — operates on a deep copy of the plan."""
        mutated = plan.model_copy(deep=True)
        applied: list[str] = []
        skipped: list[str] = []

        # Quick index: dimension_name → DimensionResult
        dim_map = {d.name: d for d in critic.dimensions}

        # ── C1: empty result ──────────────────────────────────────────────────
        if critic.empty_result:
            self._fix_empty_result(mutated, applied, skipped)

        # ── C2: unexpected cardinality ────────────────────────────────────────
        c2 = dim_map.get("unexpected_cardinality")
        if c2 and not c2.passed:
            hint = (c2.replan_hint or "").lower()
            # Distinguish fan-out vs over-aggregation by hint content
            if "fan-out" in hint or "count distinct" in hint or "join" in hint:
                self._fix_fanout(mutated, applied, skipped)
            else:
                # Default to over-aggregation fix (covers "time bucket" hint)
                self._fix_over_aggregation(mutated, applied, skipped)

        # ── C3: missing time axis ─────────────────────────────────────────────
        c3 = dim_map.get("missing_time_axis")
        if c3 and not c3.passed:
            self._fix_missing_time_axis(mutated, applied, skipped)

        # ── C4: suspicious joins ──────────────────────────────────────────────
        c4 = dim_map.get("suspicious_joins")
        if c4 and not c4.passed:
            self._fix_suspicious_joins(mutated, applied, skipped)

        # ── C5: aggregation mismatch ──────────────────────────────────────────
        c5 = dim_map.get("aggregation_mismatch")
        if c5 and not c5.passed:
            self._fix_aggregation_mismatch(mutated, applied, skipped)

        # ── C6: insufficient sample ───────────────────────────────────────────
        c6 = dim_map.get("insufficient_sample")
        if c6 and not c6.passed:
            # Skip if empty_result already handled it (overlapping fix)
            if not critic.empty_result:
                self._fix_insufficient_sample(mutated, applied, skipped)

        # ── C7: intent alignment ──────────────────────────────────────────────
        c7 = dim_map.get("intent_alignment")
        if c7 and not c7.passed:
            self._fix_intent_alignment(mutated, applied, skipped)

        # ── C8: result anomaly ────────────────────────────────────────────────
        # Flag-only dimension: the anomaly is in the data, not the plan structure.
        c8 = dim_map.get("result_anomaly")
        if c8 and not c8.passed:
            skipped.append(
                "result_anomaly: data-quality flag — no structural plan mutation applicable."
            )

        logger.info(
            "Replanner: %d mutation(s) applied, %d skipped. mutations=%s",
            len(applied),
            len(skipped),
            applied,
        )
        return ReplanResult(
            plan=mutated,
            applied=applied,
            skipped=skipped,
            original_hints=list(critic.replan_hints),
        )

    # ── per-dimension mutation helpers ─────────────────────────────────────────

    def _fix_empty_result(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C1 — Zero rows returned.

        Drop the last (most recently added) resolved filter and add a plan
        note suggesting the generator reconsider the time range.
        """
        if plan.filters:
            dropped = plan.filters.pop()
            applied.append(
                f"empty_result: dropped filter "
                f"'{dropped.original_semantic_filter}' to widen the result set."
            )
        else:
            skipped.append(
                "empty_result: no filters to drop — result was already unfiltered."
            )

        note = (
            "REPLAN (empty result): the previous query returned zero rows. "
            "If a time filter is in effect, consider doubling the date range or "
            "removing it entirely to confirm that data exists for this metric."
        )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append(
                "empty_result: added plan note to reconsider time range."
            )

    def _fix_fanout(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C2 (fan-out) — JOIN multiplication produced too many rows.

        Add a DISTINCT-guard plan note and set a defensive row LIMIT.
        """
        note = (
            "REPLAN (fan-out cardinality): a previous attempt produced an "
            "unexpectedly large result due to JOIN row multiplication. "
            "Add COUNT DISTINCT on the grain key, or ensure the JOIN condition "
            "is selective enough to prevent Cartesian expansion."
        )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append(
                "unexpected_cardinality(fan-out): added DISTINCT guard note."
            )

        if plan.limit is None:
            plan.limit = self._DEFAULT_FANOUT_LIMIT
            applied.append(
                f"unexpected_cardinality(fan-out): set defensive row LIMIT "
                f"to {self._DEFAULT_FANOUT_LIMIT}."
            )
        else:
            skipped.append(
                f"unexpected_cardinality(fan-out): LIMIT already set "
                f"({plan.limit}); not overriding."
            )

    def _fix_over_aggregation(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C2 (over-aggregation) — time-series query collapsed to a single row.

        Add a plan note to include the time bucket in SELECT + GROUP BY.
        """
        if plan.time_resolution and plan.time_resolution.group_by_sql:
            grp = plan.time_resolution.group_by_sql
            note = (
                "REPLAN (over-aggregation): the previous query collapsed a time-series "
                f"into a single row. Include '{grp}' in both SELECT and GROUP BY."
            )
        else:
            note = (
                "REPLAN (over-aggregation): the previous query collapsed a time-series "
                "into a single row. Add a DATE_TRUNC or equivalent time-bucket expression "
                "to both SELECT and GROUP BY."
            )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append(
                "unexpected_cardinality(over-aggregation): "
                "added time-bucket grouping note."
            )

    def _fix_missing_time_axis(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C3 — Time-series intent but no time column found in result columns.

        Add a plan note instructing the generator to include the time column.
        """
        if plan.time_resolution:
            col = plan.time_resolution.time_column
            grp = (
                plan.time_resolution.group_by_sql
                or f"DATE_TRUNC('day', {col})"
            )
            note = (
                f"REPLAN (missing time axis): the previous result contained no "
                f"time column. Add '{grp}' to the SELECT list and GROUP BY clause."
            )
        else:
            note = (
                "REPLAN (missing time axis): the previous result contained no "
                "time column despite a time-series intent. Add a DATE_TRUNC or "
                "equivalent expression to SELECT and GROUP BY."
            )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append(
                "missing_time_axis: added time-grouping instruction note."
            )

    def _fix_suspicious_joins(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C4 — Deep JOIN chain with high row count.

        Prune joins beyond ``_MAX_JOINS`` and add a safety verification note.
        """
        if len(plan.joins) > self._MAX_JOINS:
            pruned = plan.joins[self._MAX_JOINS:]
            plan.joins = plan.joins[: self._MAX_JOINS]
            pruned_names = [f"{j.from_table}→{j.to_table}" for j in pruned]
            applied.append(
                f"suspicious_joins: pruned {len(pruned)} join(s) "
                f"({', '.join(pruned_names)}) — keeping first {self._MAX_JOINS}."
            )
        else:
            skipped.append(
                f"suspicious_joins: only {len(plan.joins)} join(s) present "
                f"(≤ {self._MAX_JOINS}); no structural pruning applied."
            )

        note = (
            "REPLAN (suspicious joins): a prior attempt may have caused row "
            "fan-out via a deep JOIN chain. Verify all JOIN conditions use "
            "indexed keys and add COUNT DISTINCT on the grain key if needed."
        )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append("suspicious_joins: added JOIN safety verification note.")

    def _fix_aggregation_mismatch(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C5 — Aggregation function inconsistent with column data type.

        Add a targeted plan note; the generator must re-derive the correct
        formula (a structural rewrite would require semantic context not
        available here).
        """
        _id_suffixes = ("_id", "_key", "_code", "_num", "_ref")
        suspect = [
            m.output_alias
            for m in plan.measures
            if (
                m.aggregation is not None
                and m.aggregation.value in ("SUM", "AVG")
                and any(m.name.lower().endswith(s) for s in _id_suffixes)
            )
        ]

        if suspect:
            note = (
                f"REPLAN (aggregation mismatch): measure(s) {suspect} apply "
                "SUM/AVG on what appear to be identifier columns. Verify whether "
                "COUNT or COUNT DISTINCT is the intended aggregation function."
            )
        else:
            note = (
                "REPLAN (aggregation mismatch): the previous query used an "
                "incorrect aggregation function (e.g. COUNT on a text column or "
                "SUM on a non-numeric column). Verify each metric formula and "
                "confirm the target column's data type."
            )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append(
                "aggregation_mismatch: added metric formula verification note."
            )

    def _fix_insufficient_sample(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C6 — Too few rows for statistical analysis.

        Drop all resolved filters to widen the result set and remove any LIMIT.
        """
        if plan.filters:
            n = len(plan.filters)
            plan.filters.clear()
            applied.append(
                f"insufficient_sample: removed {n} filter(s) to increase sample size."
            )
        else:
            skipped.append(
                "insufficient_sample: no filters to remove — already unfiltered."
            )

        if plan.limit is not None:
            plan.limit = None
            applied.append("insufficient_sample: removed LIMIT to allow full result set.")

        note = (
            f"REPLAN (insufficient sample): the previous result contained fewer "
            f"than {self._MIN_SAMPLE_SIZE} rows — insufficient for statistical "
            "analysis. Filters have been relaxed; ensure the query returns "
            "adequate data."
        )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)

    def _fix_intent_alignment(
        self, plan: "QueryPlan", applied: list[str], skipped: list[str]
    ) -> None:
        """
        C7 — Result column names don't match plan output_alias fields.

        Add an alias-alignment plan note listing every expected output alias.
        """
        expected = (
            [m.output_alias for m in plan.measures]
            + [d.output_alias for d in plan.dimensions]
        )
        note = (
            "REPLAN (intent alignment): the previous result was missing one or "
            "more expected columns. Ensure the SELECT clause uses exactly these "
            f"aliases: {expected}."
        )
        if note not in plan.plan_notes:
            plan.plan_notes.append(note)
            applied.append(
                f"intent_alignment: added alias-alignment note "
                f"for {len(expected)} expected column(s)."
            )
