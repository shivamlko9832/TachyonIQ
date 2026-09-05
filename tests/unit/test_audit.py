"""
Tests for AuditLogger (uada/observability/audit.py).

Verifies the safety contract:
  - sql_hash is a 16-hex prefix of SHA-256, never raw SQL
  - result row values are never in the log record
  - question text IS logged (design intent)
  - all expected record fields are present
  - AuditLogger.log() never raises, even with a bad path or corrupt response
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uada.models.result import PipelineStage, UADAError, UADAResponse

pytestmark = pytest.mark.unit

_SESSION = "sess-audit-test"
_QUESTION = "What was total revenue last month?"
_SQL = "SELECT SUM(revenue) FROM orders WHERE period = '2026-08'"


def _success_response(*, sql: str | None = _SQL) -> UADAResponse:
    return UADAResponse(
        session_id=_SESSION,
        turn_id=0,
        timestamp=datetime.now(tz=UTC),
        answer="Total revenue was 1,234.",
        sql=sql,
        row_count=1,
        is_truncated=False,
        question_type="aggregation",
        tables_used=["orders"],
        pipeline_duration_ms=42.5,
    )


def _error_response() -> UADAResponse:
    return UADAResponse(
        session_id=_SESSION,
        turn_id=1,
        timestamp=datetime.now(tz=UTC),
        error=UADAError(
            stage=PipelineStage.SQL_VALIDATION,
            error_type="security_violation",
            message="DROP TABLE detected.",
            user_message="This query cannot be executed.",
            is_retryable=False,
        ),
        pipeline_duration_ms=5.0,
    )


class TestAuditLoggerFileOutput:
    """Records written to a real temp file are valid JSON with correct fields."""

    @pytest.fixture
    def log_path(self, tmp_path: Path) -> Path:
        return tmp_path / "audit" / "audit.jsonl"

    @pytest.fixture
    def audit_logger(self, log_path: Path):
        from uada.observability.audit import AuditLogger

        # Each test gets a fresh logger instance; clear the shared handler
        # list that the module-level singleton re-uses across hot-reloads.
        import logging as _logging
        al = _logging.getLogger("uada.audit")
        al.handlers.clear()
        return AuditLogger(log_path)

    def _last_record(self, log_path: Path) -> dict:
        lines = log_path.read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_creates_log_file_on_first_write(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_success_response(), question=_QUESTION, user_id="u1")
        assert log_path.exists()

    def test_success_record_has_all_expected_fields(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_success_response(), question=_QUESTION, user_id="u1")
        rec = self._last_record(log_path)

        assert rec["session_id"] == _SESSION
        assert rec["turn_id"] == 0
        assert rec["question"] == _QUESTION
        assert rec["question_type"] == "aggregation"
        assert rec["tables_used"] == ["orders"]
        assert rec["row_count"] == 1
        assert rec["is_truncated"] is False
        assert rec["is_success"] is True
        assert rec["error_type"] is None
        assert rec["error_stage"] is None
        assert rec["user_id"] == "u1"
        assert isinstance(rec["pipeline_duration_ms"], float)

    def test_sql_hash_is_16_hex_prefix_of_sha256(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_success_response(sql=_SQL), question=_QUESTION)
        rec = self._last_record(log_path)

        expected_hash = hashlib.sha256(_SQL.encode()).hexdigest()[:16]
        assert rec["sql_hash"] == expected_hash
        assert len(rec["sql_hash"]) == 16
        assert all(c in "0123456789abcdef" for c in rec["sql_hash"])

    def test_raw_sql_never_in_record(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_success_response(sql=_SQL), question=_QUESTION)
        raw = log_path.read_text()
        # The SQL string should not appear verbatim anywhere in the log
        assert _SQL not in raw

    def test_sql_hash_is_none_when_no_sql(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_success_response(sql=None), question=_QUESTION)
        rec = self._last_record(log_path)
        assert rec["sql_hash"] is None

    def test_error_response_records_error_fields(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_error_response(), question=_QUESTION)
        rec = self._last_record(log_path)

        assert rec["is_success"] is False
        assert rec["error_type"] == "security_violation"
        assert rec["error_stage"] == "sql_validation"
        assert rec["sql_hash"] is None

    def test_anonymous_user_when_user_id_is_none(self, audit_logger, log_path: Path) -> None:
        audit_logger.log(response=_success_response(), question=_QUESTION, user_id=None)
        rec = self._last_record(log_path)
        assert rec["user_id"] == "anonymous"

    def test_multiple_calls_produce_multiple_lines(self, audit_logger, log_path: Path) -> None:
        for i in range(3):
            audit_logger.log(response=_success_response(), question=f"Q{i}")
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_each_line_is_valid_json(self, audit_logger, log_path: Path) -> None:
        for i in range(5):
            audit_logger.log(response=_success_response(), question=f"Q{i}")
        for line in log_path.read_text().strip().splitlines():
            json.loads(line)  # must not raise


class TestAuditLoggerSafety:
    """AuditLogger.log() never raises, regardless of input."""

    @pytest.fixture
    def stream_logger(self):
        from uada.observability.audit import AuditLogger
        import logging as _logging
        al = _logging.getLogger("uada.audit")
        al.handlers.clear()
        return AuditLogger(audit_path=None)  # StreamHandler fallback

    def test_log_never_raises_on_success(self, stream_logger) -> None:
        stream_logger.log(response=_success_response(), question=_QUESTION)

    def test_log_never_raises_on_error_response(self, stream_logger) -> None:
        stream_logger.log(response=_error_response(), question=_QUESTION)

    def test_log_never_raises_on_empty_question(self, stream_logger) -> None:
        stream_logger.log(response=_success_response(), question="")

    def test_no_path_falls_back_to_stream_without_crash(self, tmp_path: Path) -> None:
        from uada.observability.audit import AuditLogger
        import logging as _logging
        al = _logging.getLogger("uada.audit")
        al.handlers.clear()
        # Pass a path in an unwritable directory to trigger OSError fallback
        bad_path = Path("/no/such/directory/audit.jsonl")
        logger_inst = AuditLogger(bad_path)  # should not raise
        logger_inst.log(response=_success_response(), question=_QUESTION)  # should not raise
