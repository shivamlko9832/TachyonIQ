"""
Tests for tests/evaluation/run_security_eval.py.

The real dataset run is covered by simply invoking the script (see
CLAUDE.md's own verification command); these tests instead target the
runner's logic in isolation with synthetic cases, including the failure
path (a security gap), which the real all-passing dataset can't exercise.
"""

from __future__ import annotations

import pytest

from tests.evaluation.run_security_eval import load_cases, main, run

pytestmark = pytest.mark.unit


class TestLoadCases:
    def test_loads_real_dataset(self) -> None:
        cases = load_cases()
        assert len(cases) == 50
        assert all({"id", "sql", "expected_violation"} <= case.keys() for case in cases)


class TestRun:
    def test_all_cases_pass_on_real_dataset(self) -> None:
        passed, failures = run(load_cases())
        assert passed == 50
        assert failures == []

    def test_detects_a_security_gap(self) -> None:
        # A SELECT is safe, but this case claims it should be rejected --
        # a real gap the runner must report as a failure, not a pass.
        cases = [
            {
                "id": "fake-001",
                "sql": "SELECT * FROM orders",
                "expected_violation": "non_select_statement",
                "description": "Intentionally wrong expectation.",
            }
        ]
        passed, failures = run(cases)
        assert passed == 0
        assert len(failures) == 1
        assert failures[0]["id"] == "fake-001"
        assert failures[0]["is_safe"] is True

    def test_wrong_violation_type_is_a_failure(self) -> None:
        # Genuinely unsafe, but for a different reason than expected --
        # still a gap worth surfacing, not a silent pass.
        cases = [
            {
                "id": "fake-002",
                "sql": "DROP TABLE orders",
                "expected_violation": "table_not_in_allowlist",
                "description": "Wrong expected violation type.",
            }
        ]
        passed, failures = run(cases)
        assert passed == 0
        assert failures[0]["violations"] == ["non_select_statement"]


class TestMain:
    def test_returns_zero_on_real_dataset(self) -> None:
        assert main([]) == 0

    def test_returns_one_on_a_security_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests.evaluation.run_security_eval as module

        monkeypatch.setattr(
            module,
            "load_cases",
            lambda path=module.DATASET_PATH: [
                {
                    "id": "fake-003",
                    "sql": "SELECT * FROM orders",
                    "expected_violation": "non_select_statement",
                    "description": "Intentionally wrong expectation.",
                }
            ],
        )
        assert main([]) == 1
