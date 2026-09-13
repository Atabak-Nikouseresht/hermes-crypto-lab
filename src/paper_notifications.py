"""Post-commit Telegram delivery with notification-only retry."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable
import uuid

from src.paper_store import PaperStore


class NotificationError(RuntimeError):
    pass


class HermesTelegramSender:
    """Send an existing report through the configured Hermes Telegram target."""

    def __init__(self, *, hermes_executable: str = "hermes", timeout_seconds: int = 60):
        self.hermes_executable = hermes_executable
        self.timeout_seconds = timeout_seconds

    def __call__(self, target: str, report_path: Path) -> dict[str, Any]:
        completed = subprocess.run(
            [
                self.hermes_executable,
                "send",
                "--to",
                target,
                "--file",
                str(Path(report_path).resolve()),
                "--json",
            ],
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown delivery error").strip()
            raise RuntimeError(detail)
        try:
            return json.loads(completed.stdout) if completed.stdout.strip() else {"ok": True}
        except json.JSONDecodeError:
            return {"ok": True, "raw": completed.stdout.strip()}


class NotificationService:
    def __init__(
        self,
        store: PaperStore,
        *,
        target: str,
        sender: Callable[[str, Path], Any],
    ):
        self.store = store
        self.target = target
        self.sender = sender

    def _assert_committed(self, run_id: str) -> None:
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT status, completed_at_utc FROM paper_runs WHERE run_id=?", [run_id]
            ).fetchone()
        if row is None:
            raise NotificationError(f"Unknown paper run: {run_id}")
        if row[0] == "RUNNING" or row[1] is None:
            raise NotificationError("Telegram delivery is forbidden before transaction finalization")

    def register_pending(self, run_id: str, report_path: Path) -> None:
        """Persist notification eligibility without invoking the external sender."""
        self._assert_committed(run_id)
        report_path = Path(report_path).resolve()
        if not report_path.is_file():
            raise NotificationError(f"Report file does not exist: {report_path}")
        now = datetime.now(timezone.utc)
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_notifications
                VALUES (?, ?, ?, 'PENDING', 0, NULL, ?, ?, NULL)
                """,
                [run_id, self.target, str(report_path), now, now],
            )

    def _claim_attempt(self, run_id: str, report_path: Path, target: str) -> str:
        """Durably claim one retryable notification before external delivery."""
        now = datetime.now(timezone.utc)
        attempt_id = str(uuid.uuid4())
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                claimed = connection.execute(
                    """
                    UPDATE paper_notifications
                    SET status='SENDING', attempt_count=attempt_count + 1,
                        last_error=NULL, updated_at_utc=?
                    WHERE run_id=? AND status IN ('PENDING', 'FAILED')
                    RETURNING attempt_count
                    """,
                    [now, run_id],
                ).fetchone()
                if claimed is None:
                    raise NotificationError(
                        f"Notification for run {run_id} requires manual recovery before resend"
                    )
                connection.execute(
                    "INSERT INTO notification_attempts VALUES (?, ?, ?, 'SENDING', NULL)",
                    [attempt_id, run_id, now],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return attempt_id

    def _mark_failed(self, run_id: str, attempt_id: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    """
                    UPDATE paper_notifications
                    SET status='FAILED', last_error=?, updated_at_utc=?
                    WHERE run_id=? AND status='SENDING'
                    """,
                    [message, now, run_id],
                )
                connection.execute(
                    """
                    UPDATE notification_attempts SET status='FAILED', error=?
                    WHERE attempt_id=? AND run_id=? AND status='SENDING'
                    """,
                    [message, attempt_id, run_id],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def _mark_delivered(self, run_id: str, attempt_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    """
                    UPDATE paper_notifications
                    SET status='DELIVERED', last_error=NULL, updated_at_utc=?,
                        delivered_at_utc=?
                    WHERE run_id=? AND status='SENDING'
                    """,
                    [now, now, run_id],
                )
                connection.execute(
                    """
                    UPDATE notification_attempts SET status='DELIVERED', error=NULL
                    WHERE attempt_id=? AND run_id=? AND status='SENDING'
                    """,
                    [attempt_id, run_id],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def _attempt(self, run_id: str, report_path: Path, target: str) -> dict[str, Any]:
        self._assert_committed(run_id)
        report_path = Path(report_path).resolve()
        if not report_path.is_file():
            raise NotificationError(f"Report file does not exist: {report_path}")
        attempt_id = self._claim_attempt(run_id, report_path, target)
        try:
            response = self.sender(target, report_path)
        except Exception as error:
            message = str(error)
            self._mark_failed(run_id, attempt_id, message)
            raise NotificationError(message) from error

        self._mark_delivered(run_id, attempt_id)
        return response if isinstance(response, dict) else {"ok": True}

    def send_committed_run(self, run_id: str, report_path: Path) -> dict[str, Any]:
        self.register_pending(run_id, report_path)
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT target, report_path FROM paper_notifications WHERE run_id=?",
                [run_id],
            ).fetchone()
        if row is None:
            raise NotificationError(f"No prior notification record for run {run_id}")
        return self._attempt(run_id, Path(row[1]), row[0])

    def resend(self, run_id: str) -> dict[str, Any]:
        """Retry Telegram only; never fetches data or invokes strategy execution."""
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT target, report_path, status FROM paper_notifications WHERE run_id=?",
                [run_id],
            ).fetchone()
        if row is None:
            raise NotificationError(f"No prior notification record for run {run_id}")
        if row[2] == "SENDING":
            raise NotificationError(
                f"Notification for run {run_id} requires manual recovery before resend"
            )
        if row[2] not in {"PENDING", "FAILED"}:
            raise NotificationError(
                f"Notification for run {run_id} is already delivered; resend refused"
            )
        return self._attempt(run_id, Path(row[1]), row[0])
