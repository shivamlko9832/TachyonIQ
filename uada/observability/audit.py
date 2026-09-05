"""
Audit Logger (P2-7)
====================
Structured JSON audit trail — one record per completed pipeline turn.
Written to a rotating JSONL file; falls back to the application logger when
the file path is not set or not writable.

Safety contract:
  - Raw SQL is NEVER logged; only a 16-hex-char SHA-256 prefix.
  - Result row values are NEVER logged.
  - Credentials and secrets are NEVER logged.
  - Question text IS logged (operators need it for debugging).
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uada.models.result import UADAResponse

logger = logging.getLogger(__name__)

_AUDIT_LOGGER_NAME = "uada.audit"


class AuditLogger:
    """Writes one structured JSON record per completed pipeline turn."""

    def __init__(self, audit_path: Path | None = None) -> None:
        self._audit_logger = self._setup_file_logger(audit_path)

    # ── Setup ────────────────────────────────────────────────────────────────

    def _setup_file_logger(self, path: Path | None) -> logging.Logger:
        audit_log = logging.getLogger(_AUDIT_LOGGER_NAME)
        audit_log.setLevel(logging.INFO)
        audit_log.propagate = False  # Don't duplicate into root logger

        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = logging.handlers.RotatingFileHandler(
                    path,
                    maxBytes=50 * 1024 * 1024,  # 50 MB per file
                    backupCount=10,
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "AuditLogger: cannot open '%s' (%s); audit records will go to app logger.",
                    path,
                    exc,
                )
                handler = logging.StreamHandler()
        else:
            handler = logging.StreamHandler()

        handler.setFormatter(logging.Formatter("%(message)s"))
        # Avoid duplicate handlers across hot-reloads in development
        if not audit_log.handlers:
            audit_log.addHandler(handler)

        return audit_log

    # ── Public API ───────────────────────────────────────────────────────────

    def log(
        self,
        *,
        response: UADAResponse,
        question: str,
        user_id: str | None = None,
    ) -> None:
        """
        Write one audit record for a completed pipeline turn.

        Never raises — a logging failure must never abort a pipeline response.
        """
        try:
            self._write(response=response, question=question, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AuditLogger.log() failed: %s", exc)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _write(
        self,
        *,
        response: UADAResponse,
        question: str,
        user_id: str | None,
    ) -> None:
        sql_hash: str | None = None
        if response.sql:
            # 16-hex prefix is enough to identify a query in incident review
            # without exposing the SQL itself.
            sql_hash = hashlib.sha256(response.sql.encode()).hexdigest()[:16]

        record: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "user_id": user_id or "anonymous",
            "session_id": response.session_id,
            "turn_id": response.turn_id,
            # Question text included for auditability — it is a user input,
            # not a query result or credential.
            "question": question,
            "question_type": response.question_type,
            "tables_used": response.tables_used,
            "sql_hash": sql_hash,
            "row_count": response.row_count,
            "is_truncated": response.is_truncated,
            "pipeline_duration_ms": (
                round(response.pipeline_duration_ms, 2)
                if response.pipeline_duration_ms is not None
                else None
            ),
            "is_success": response.is_success,
            "error_type": response.error.error_type if response.error else None,
            "error_stage": response.error.stage.value if response.error else None,
        }

        self._audit_logger.info(json.dumps(record, default=str))
