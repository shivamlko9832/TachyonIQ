#!/usr/bin/env python
"""
Deterministic security evaluation: every attack case in
tests/evaluation/datasets/security_cases.jsonl must be rejected by
SQLValidator, with the specific violation type the case expects. No LLM,
no database -- safe to run in every CI pipeline, on every push.

Usage:
    python tests/evaluation/run_security_eval.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from uada.pipeline.sql_validator import SQLValidator

logger = logging.getLogger(__name__)

DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "security_cases.jsonl"

# A representative table set; these attacks target statement shape and
# system-schema access, not the allowlist itself, so the exact table
# names don't matter as long as they cover the "legitimate table" cases.
ALLOWED_TABLES = frozenset({"orders", "customers", "products", "order_items", "regions"})


def load_cases(path: Path = DATASET_PATH) -> list[dict[str, Any]]:
    """Load the JSONL security case dataset."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run(cases: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """
    Validate every case. Returns (passed_count, failures), where a
    failure means the case was NOT correctly rejected with its expected
    violation type -- i.e. a security gap.
    """
    validator = SQLValidator(
        allowed_tables=set(ALLOWED_TABLES),
        max_subquery_depth=3,
        inject_limit=True,
        default_limit=1000,
    )

    passed = 0
    failures: list[dict[str, Any]] = []
    for case in cases:
        result = validator.validate(case["sql"])
        violation_types = [v.violation_type.value for v in result.violations]
        if (not result.is_safe) and case["expected_violation"] in violation_types:
            passed += 1
        else:
            failures.append(
                {
                    "id": case.get("id"),
                    "description": case.get("description"),
                    "expected_violation": case["expected_violation"],
                    "is_safe": result.is_safe,
                    "violations": violation_types,
                }
            )
    return passed, failures


def main(argv: list[str] | None = None) -> int:
    """
    Returns:
        0 if every case was correctly rejected, 1 if any case represents
        a security gap (executed as unsafe, or rejected for the wrong
        reason).
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cases = load_cases()
    passed, failures = run(cases)

    logger.info("Security evaluation: %d/%d passed", passed, len(cases))

    if failures:
        logger.error("%d security gap(s) found:", len(failures))
        for failure in failures:
            logger.error(
                "  [%s] %s -- expected violation '%s', got is_safe=%s violations=%s",
                failure["id"],
                failure["description"],
                failure["expected_violation"],
                failure["is_safe"],
                failure["violations"],
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
