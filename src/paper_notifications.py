"""Durable post-commit Telegram delivery and notification-only recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
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

    @staticmethod
    def _report_sha256(report_path: Path) -> str:
        digest = hashlib.sha256()
        with report_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

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
        """Persist a paper notification and immutable content digest before delivery."""
        self._assert_committed(run_id)
        report_path = Path(report_path).resolve()
        if not report_path.is_file():
            raise NotificationError(f"Report file does not exist: {report_path}")
        if not self.target or not self.target.strip():
            raise NotificationError("Telegram target is required for a new notification")
        now = datetime.now(timezone.utc)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO paper_notifications "
                "(run_id, target, report_path, status, attempt_count, last_error, created_at_utc, "
                "updated_at_utc, delivered_at_utc, report_sha256, notification_kind) "
                "VALUES (?, ?, ?, 'PENDING', 0, NULL, ?, ?, NULL, ?, 'PAPER')",
                [run_id, self.target, str(report_path), now, now, self._report_sha256(report_path)],
            )

    def _claim_attempt(
        self, run_id: str, _report_path: Path | None = None, _target: str | None = None
    ) -> str:
        """Durably claim one safe retry before external delivery."""
        now = datetime.now(timezone.utc)
        attempt_id = str(uuid.uuid4())
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                claimed = connection.execute(
                    "UPDATE paper_notifications SET status='SENDING', attempt_count=attempt_count + 1, "
                    "last_error=NULL, updated_at_utc=? WHERE run_id=? AND status IN ('PENDING', 'FAILED') "
                    "RETURNING attempt_count",
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
        self._mark_unsuccessful(run_id, attempt_id, message, 'FAILED')

    def _mark_unsuccessful(self, run_id: str, attempt_id: str, message: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    "UPDATE paper_notifications SET status=?, last_error=?, updated_at_utc=? "
                    "WHERE run_id=? AND status='SENDING'",
                    [status, message, now, run_id],
                )
                connection.execute(
                    "UPDATE notification_attempts SET status=?, error=? "
                    "WHERE attempt_id=? AND run_id=? AND status='SENDING'",
                    [status, message, attempt_id, run_id],
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
                    "UPDATE paper_notifications SET status='DELIVERED', last_error=NULL, "
                    "updated_at_utc=?, delivered_at_utc=? WHERE run_id=? AND status='SENDING'",
                    [now, now, run_id],
                )
                connection.execute(
                    "UPDATE notification_attempts SET status='DELIVERED', error=NULL "
                    "WHERE attempt_id=? AND run_id=? AND status='SENDING'",
                    [attempt_id, run_id],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def _attempt(
        self,
        run_id: str,
        report_path: Path,
        target: str,
        report_sha256: str | None,
        *,
        require_committed: bool,
    ) -> dict[str, Any]:
        if require_committed:
            self._assert_committed(run_id)
        report_path = Path(report_path).resolve()
        if not report_path.is_file():
            raise NotificationError(f"Report file does not exist: {report_path}")
        if not report_sha256:
            raise NotificationError(
                f"Legacy notification for run {run_id} has no historical report hash; "
                "automatic delivery is refused and the record will not be backfilled"
            )
        if self._report_sha256(report_path) != report_sha256:
            raise NotificationError(f"Notification report hash mismatch for run {run_id}")
        attempt_id = self._claim_attempt(run_id)
        try:
            response = self.sender(target, report_path)
        except (subprocess.TimeoutExpired, TimeoutError, ConnectionError) as error:
            message = str(error) or type(error).__name__
            self._mark_unsuccessful(run_id, attempt_id, message, 'DELIVERY_UNKNOWN')
            raise NotificationError(message) from error
        except Exception as error:
            message = str(error)
            self._mark_failed(run_id, attempt_id, message)
            raise NotificationError(message) from error
        except BaseException as error:
            self._mark_unsuccessful(
                run_id, attempt_id, str(error) or type(error).__name__, 'DELIVERY_UNKNOWN'
            )
            raise
        self._mark_delivered(run_id, attempt_id)
        return response if isinstance(response, dict) else {"ok": True}

    def send_committed_run(self, run_id: str, report_path: Path) -> dict[str, Any]:
        self.register_pending(run_id, report_path)
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT target, report_path, report_sha256 FROM paper_notifications WHERE run_id=?",
                [run_id],
            ).fetchone()
        if row is None:
            raise NotificationError(f"No prior notification record for run {run_id}")
        return self._attempt(run_id, Path(row[1]), row[0], row[2], require_committed=True)

    def resend(self, run_id: str) -> dict[str, Any]:
        """Retry only safe PENDING/FAILED notifications; never run trading logic."""
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT target, report_path, status, report_sha256 "
                "FROM paper_notifications WHERE run_id=?",
                [run_id],
            ).fetchone()
        if row is None:
            raise NotificationError(f"No prior notification record for run {run_id}")
        if row[2] in {"SENDING", "DELIVERY_UNKNOWN"}:
            raise NotificationError(
                f"Notification for run {run_id} requires manual recovery before resend"
            )
        if row[2] not in {"PENDING", "FAILED"}:
            raise NotificationError(
                f"Notification for run {run_id} is already delivered; resend refused"
            )
        return self._attempt(run_id, Path(row[1]), row[0], row[3], require_committed=True)

    def recover(self, run_id: str, *, resolution: str, operator: str, reason: str) -> None:
        """Durably record an operator decision for an ambiguous delivery; never send."""
        if not operator.strip() or not reason.strip():
            raise NotificationError("Manual notification recovery requires operator and reason")
        outcomes = {
            "unknown": "DELIVERY_UNKNOWN",
            "delivered": "DELIVERED",
            "not-delivered": "FAILED",
        }
        if resolution not in outcomes:
            raise NotificationError("Invalid notification recovery resolution")
        now = datetime.now(timezone.utc)
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                row = connection.execute(
                    "SELECT status FROM paper_notifications WHERE run_id=?", [run_id]
                ).fetchone()
                if row is None:
                    raise NotificationError(f"No prior notification record for run {run_id}")
                if row[0] not in {"SENDING", "DELIVERY_UNKNOWN"}:
                    raise NotificationError(
                        f"Notification for run {run_id} is not in an ambiguous delivery state"
                    )
                new_status = outcomes[resolution]
                attempt = connection.execute(
                    "SELECT attempt_id FROM notification_attempts "
                    "WHERE run_id=? AND status IN ('SENDING', 'DELIVERY_UNKNOWN') "
                    "ORDER BY attempted_at_utc DESC LIMIT 1",
                    [run_id],
                ).fetchone()
                connection.execute(
                    "UPDATE paper_notifications SET status=?, updated_at_utc=?, "
                    "delivered_at_utc=CASE WHEN ?='DELIVERED' THEN ? ELSE delivered_at_utc END "
                    "WHERE run_id=?",
                    [new_status, now, new_status, now, run_id],
                )
                if attempt is not None:
                    connection.execute(
                        "UPDATE notification_attempts SET status=? WHERE attempt_id=? "
                        "AND run_id=? AND status IN ('SENDING', 'DELIVERY_UNKNOWN')",
                        [new_status, attempt[0], run_id],
                    )
                connection.execute(
                    "INSERT INTO notification_audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()), run_id, attempt[0] if attempt else None,
                        resolution, operator.strip(), reason.strip(),
                        row[0], new_status, now,
                    ],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def send_committed_monthly(
        self, notification_id: str, report_path: Path
    ) -> dict[str, Any]:
        """Deliver a committed monthly report through the same durable outbox."""
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT target, report_path, status, report_sha256 "
                "FROM paper_notifications WHERE run_id=?",
                [notification_id],
            ).fetchone()
        if row is None:
            report_path = Path(report_path).resolve()
            if not self.target or not self.target.strip():
                raise NotificationError(
                    "Monthly delivery requires a Telegram target for a new notification"
                )
            if not report_path.is_file():
                raise NotificationError(f"Report file does not exist: {report_path}")
            now = datetime.now(timezone.utc)
            report_sha256 = self._report_sha256(report_path)
            with self.store.connect() as connection:
                connection.execute(
                    "INSERT INTO paper_notifications (run_id, target, report_path, status, "
                    "attempt_count, last_error, created_at_utc, updated_at_utc, delivered_at_utc, "
                    "report_sha256, notification_kind) VALUES (?, ?, ?, 'PENDING', 0, NULL, ?, ?, "
                    "NULL, ?, 'MONTHLY')",
                    [notification_id, self.target, str(report_path), now, now, report_sha256],
                )
            row = (self.target, str(report_path), "PENDING", report_sha256)
        if row[2] == "DELIVERED":
            return {"ok": True, "already_delivered": True}
        if row[2] in {"SENDING", "DELIVERY_UNKNOWN"}:
            raise NotificationError(
                f"Notification for run {notification_id} requires manual recovery before resend"
            )
        if row[2] not in {"PENDING", "FAILED"}:
            raise NotificationError(f"Unsupported notification state for {notification_id}: {row[2]}")
        return self._attempt(
            notification_id, Path(row[1]), row[0], row[3], require_committed=False
        )
