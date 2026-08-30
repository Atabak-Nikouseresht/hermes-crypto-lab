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

    def _attempt(self, run_id: str, report_path: Path, target: str) -> dict[str, Any]:
        self._assert_committed(run_id)
        report_path = Path(report_path).resolve()
        if not report_path.is_file():
            raise NotificationError(f"Report file does not exist: {report_path}")
        now = datetime.now(timezone.utc)
        with self.store.connect(read_only=True) as connection:
            existing = connection.execute(
                "SELECT attempt_count, created_at_utc FROM paper_notifications WHERE run_id=?",
                [run_id],
            ).fetchone()
        attempt_count = int(existing[0]) + 1 if existing else 1
        created_at = existing[1] if existing else now
        try:
            response = self.sender(target, report_path)
        except Exception as error:
            message = str(error)
            with self.store.connect() as connection:
                connection.execute("BEGIN TRANSACTION")
                connection.execute(
                    """
                    INSERT INTO paper_notifications VALUES (?, ?, ?, 'FAILED', ?, ?, ?, ?, NULL)
                    ON CONFLICT (run_id) DO UPDATE SET
                        target=excluded.target,
                        report_path=excluded.report_path,
                        status='FAILED',
                        attempt_count=excluded.attempt_count,
                        last_error=excluded.last_error,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    [
                        run_id,
                        target,
                        str(report_path),
                        attempt_count,
                        message,
                        created_at,
                        now,
                    ],
                )
                connection.execute(
                    "INSERT INTO notification_attempts VALUES (?, ?, ?, 'FAILED', ?)",
                    [str(uuid.uuid4()), run_id, now, message],
                )
                connection.execute("COMMIT")
            raise NotificationError(message) from error

        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                """
                INSERT INTO paper_notifications VALUES (?, ?, ?, 'DELIVERED', ?, NULL, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                    target=excluded.target,
                    report_path=excluded.report_path,
                    status='DELIVERED',
                    attempt_count=excluded.attempt_count,
                    last_error=NULL,
                    updated_at_utc=excluded.updated_at_utc,
                    delivered_at_utc=excluded.delivered_at_utc
                """,
                [run_id, target, str(report_path), attempt_count, created_at, now, now],
            )
            connection.execute(
                "INSERT INTO notification_attempts VALUES (?, ?, ?, 'DELIVERED', NULL)",
                [str(uuid.uuid4()), run_id, now],
            )
            connection.execute("COMMIT")
        return response if isinstance(response, dict) else {"ok": True}

    def send_committed_run(self, run_id: str, report_path: Path) -> dict[str, Any]:
        self.register_pending(run_id, report_path)
        return self._attempt(run_id, report_path, self.target)

    def resend(self, run_id: str) -> dict[str, Any]:
        """Retry Telegram only; never fetches data or invokes strategy execution."""
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT target, report_path, status FROM paper_notifications WHERE run_id=?",
                [run_id],
            ).fetchone()
        if row is None:
            raise NotificationError(f"No prior notification record for run {run_id}")
        if row[2] not in {"PENDING", "FAILED"}:
            raise NotificationError(
                f"Notification for run {run_id} is already delivered; resend refused"
            )
        return self._attempt(run_id, Path(row[1]), row[0])
