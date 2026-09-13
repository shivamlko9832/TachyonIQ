"""Validate the dense demo database and emit statistical proof as JSON.

The runner does not call an LLM. Every assertion is computed directly from the
database through the production SQL safety boundary, making it suitable for
pre-demo acceptance and regression checks.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

from uada.pipeline.sql_validator import SQLValidator

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "demo.sqlite"
MANIFEST_PATH = ROOT / "data" / "demo_ground_truth.json"
REPORT_PATH = ROOT / "data" / "demo_validation_report.json"
ALLOWED_TABLES = {
    "orders",
    "customers",
    "products",
    "support_tickets",
    "customer_monthly_metrics",
    "marketing_performance",
    "business_kpi_monthly",
    "wm_customers",
    "wm_contracts",
    "wm_finance_monthly",
}


def _pearson(xs: list[float], ys: list[float]) -> float:
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    denominator = math.sqrt(sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys))
    return numerator / denominator if denominator else 0.0


def _cohens_d(left: list[float], right: list[float]) -> float:
    left_var = statistics.variance(left)
    right_var = statistics.variance(right)
    pooled = math.sqrt(
        ((len(left) - 1) * left_var + (len(right) - 1) * right_var) / (len(left) + len(right) - 2)
    )
    return (statistics.fmean(left) - statistics.fmean(right)) / pooled if pooled else 0.0


def _execute_scalar(
    connection: sqlite3.Connection,
    validator: SQLValidator,
    sql: str,
) -> float | int | str | None:
    validation = validator.validate(sql, dialect="sqlite")
    if not validation.is_safe:
        raise AssertionError(f"proof SQL failed validation: {validation.violations}")
    return connection.execute(validation.normalised_sql or sql).fetchone()[0]


def _close_enough(actual: object, expected: object) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-6)
    return actual == expected


def run(
    database_path: Path = DB_PATH,
    manifest_path: Path = MANIFEST_PATH,
    *,
    report_path: Path | None = REPORT_PATH,
) -> dict[str, Any]:
    if not database_path.exists() or not manifest_path.exists():
        from scripts._gen_demo_db import generate

        generate(database_path, manifest_path=manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validator = SQLValidator(allowed_tables=ALLOWED_TABLES, inject_limit=False)
    checked_proofs: dict[str, object] = {}

    with sqlite3.connect(database_path) as connection:
        for table, expected_count in manifest["row_counts"].items():
            actual_count = _execute_scalar(
                connection,
                validator,
                f'SELECT COUNT(*) FROM "{table}"',
            )
            if actual_count != expected_count:
                raise AssertionError(
                    f"row count mismatch for {table}: {actual_count} != {expected_count}"
                )

        for name, proof in manifest["proof_queries"].items():
            actual = _execute_scalar(connection, validator, proof["sql"])
            expected = proof["expected_scalar"]
            if not _close_enough(actual, expected):
                raise AssertionError(f"proof mismatch for {name}: {actual} != {expected}")
            checked_proofs[name] = actual

        health_rows = connection.execute(
            "SELECT feature_adoption_pct, churn_risk_score FROM customer_monthly_metrics"
        ).fetchall()
        adoption = [float(row[0]) for row in health_rows]
        risk = [float(row[1]) for row in health_rows]
        low_risk = [float(row[1]) for row in health_rows if row[0] < 40]
        high_risk = [float(row[1]) for row in health_rows if row[0] >= 70]

        service_rows = connection.execute(
            """SELECT resolution_hours, csat_score FROM support_tickets
               WHERE resolution_hours IS NOT NULL AND csat_score IS NOT NULL"""
        ).fetchall()
        resolution = [float(row[0]) for row in service_rows]
        csat = [float(row[1]) for row in service_rows]

        apac_august = [
            float(row[0])
            for row in connection.execute(
                """SELECT o.net_revenue FROM orders o
                   JOIN customers c ON c.id=o.customer_id
                   WHERE c.region='APAC' AND o.order_date>='2026-08-01'
                     AND o.order_date<'2026-09-01'
                     AND o.status NOT IN ('cancelled','refunded')"""
            )
        ]

        waste_monthly = connection.execute(
            """SELECT month, SUM(revenue), SUM(operating_profit), SUM(fuel_cost),
                      SUM(labor_cost), SUM(disposal_cost), SUM(fleet_cost)
               FROM wm_finance_monthly
               GROUP BY month ORDER BY month"""
        ).fetchall()
        west_cost_periods = connection.execute(
            """SELECT CASE WHEN f.month>='2026-01-01' THEN '2026' ELSE '2025' END AS period,
                      SUM(f.collection_volume_tons), SUM(f.fuel_cost),
                      SUM(f.disposal_cost), SUM(f.operating_profit), SUM(f.revenue)
               FROM wm_finance_monthly f
               JOIN wm_contracts k ON k.id=f.contract_id
               JOIN wm_customers c ON c.id=k.customer_id
               WHERE c.region='West' AND f.month>='2025-01-01' AND f.month<'2026-09-01'
                 AND (f.month<'2025-09-01' OR f.month>='2026-01-01')
               GROUP BY period ORDER BY period"""
        ).fetchall()

    europe_prior = float(checked_proofs["europe_prior_six_month_revenue"])
    europe_recent = float(checked_proofs["europe_recent_six_month_revenue"])
    apac_mean = statistics.fmean(apac_august)
    apac_std = statistics.stdev(apac_august)
    statistical_evidence = {
        "adoption_vs_churn_risk_pearson_r": round(_pearson(adoption, risk), 4),
        "low_vs_high_adoption_risk_cohens_d": round(_cohens_d(low_risk, high_risk), 4),
        "low_adoption_mean_risk": round(statistics.fmean(low_risk), 4),
        "high_adoption_mean_risk": round(statistics.fmean(high_risk), 4),
        "resolution_time_vs_csat_pearson_r": round(_pearson(resolution, csat), 4),
        "europe_recent_vs_prior_six_month_change_pct": round(
            100 * (europe_recent - europe_prior) / europe_prior,
            2,
        ),
        "apac_august_largest_order_z_score": round(
            (max(apac_august) - apac_mean) / apac_std,
            2,
        ),
        "health_observations": len(health_rows),
        "service_observations": len(service_rows),
        "waste_monthly_observations": len(waste_monthly),
    }

    waste_revenue = [float(row[1]) for row in waste_monthly]
    waste_margin = [100.0 * float(row[2]) / float(row[1]) for row in waste_monthly]
    waste_fuel_share = [100.0 * float(row[3]) / float(row[1]) for row in waste_monthly]
    statistical_evidence.update(
        {
            "waste_revenue_mean": round(statistics.fmean(waste_revenue), 2),
            "waste_revenue_volatility_pct": round(
                100.0 * statistics.stdev(waste_revenue) / statistics.fmean(waste_revenue), 2
            ),
            "waste_fuel_share_vs_margin_pearson_r": round(
                _pearson(waste_fuel_share, waste_margin), 4
            ),
            "west_2025_fuel_cost_per_ton": round(
                float(west_cost_periods[0][2]) / float(west_cost_periods[0][1]), 2
            ),
            "west_2026_fuel_cost_per_ton": round(
                float(west_cost_periods[1][2]) / float(west_cost_periods[1][1]), 2
            ),
            "west_2025_disposal_cost_per_ton": round(
                float(west_cost_periods[0][3]) / float(west_cost_periods[0][1]), 2
            ),
            "west_2026_disposal_cost_per_ton": round(
                float(west_cost_periods[1][3]) / float(west_cost_periods[1][1]), 2
            ),
        }
    )

    if statistical_evidence["adoption_vs_churn_risk_pearson_r"] > -0.5:
        raise AssertionError("expected strong inverse relationship between adoption and churn risk")
    if statistical_evidence["low_vs_high_adoption_risk_cohens_d"] < 1.0:
        raise AssertionError("expected a large churn-risk effect between adoption cohorts")
    if statistical_evidence["resolution_time_vs_csat_pearson_r"] > -0.3:
        raise AssertionError("expected slower ticket resolution to coincide with lower CSAT")
    if statistical_evidence["europe_recent_vs_prior_six_month_change_pct"] > -20:
        raise AssertionError("expected a material recent Europe revenue decline")
    if statistical_evidence["apac_august_largest_order_z_score"] < 8:
        raise AssertionError("expected a clear APAC August order anomaly")
    if int(checked_proofs["regions_below_target_2026"]) < 1:
        raise AssertionError("expected at least one region below 2026 target")
    for quality_check in (
        "wm_operating_cost_identity_violations",
        "wm_operating_profit_identity_violations",
        "wm_duplicate_contract_months",
        "wm_required_nulls",
        "wm_ar_rollforward_violations",
    ):
        if int(checked_proofs[quality_check]) != 0:
            raise AssertionError(f"waste finance reconciliation failed: {quality_check}")
    if float(checked_proofs["wm_west_2026_margin_pct"]) >= float(
        checked_proofs["wm_west_2025_margin_pct"]
    ):
        raise AssertionError("expected documented West margin pressure in 2026")
    if statistical_evidence["west_2026_fuel_cost_per_ton"] <= statistical_evidence[
        "west_2025_fuel_cost_per_ton"
    ]:
        raise AssertionError("expected documented West fuel-cost pressure in 2026")
    if statistical_evidence["west_2026_disposal_cost_per_ton"] <= statistical_evidence[
        "west_2025_disposal_cost_per_ton"
    ]:
        raise AssertionError("expected documented West disposal-cost pressure in 2026")

    report = {
        "database": str(database_path.resolve()),
        "passed": True,
        "row_counts": manifest["row_counts"],
        "proofs_checked": checked_proofs,
        "statistical_evidence": statistical_evidence,
    }
    if report_path is not None:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
    sys.exit(0)
