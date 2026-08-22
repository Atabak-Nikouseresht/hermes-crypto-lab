from datetime import datetime, timezone

import duckdb
import pytest

from src.paper_broker import PaperConfig, PaperTradingSystem
from src.paper_notifications import NotificationError, NotificationService


class FailingThenWorkingSender:
    def __init__(self):
        self.calls = 0

    def __call__(self, target, report_path):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("telegram unavailable")
        return {"ok": True, "target": target}


def test_telegram_failure_after_committed_run_does_not_change_trades_and_retry_is_notification_only(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            """
            INSERT INTO paper_runs
            (run_id, started_at_utc, completed_at_utc, status, mode, schedule_key,
             signal_timestamp_utc, data_timestamp_utc, message, reconciliation)
            VALUES ('committed-run', ?, ?, 'EXECUTED', 'PAPER', '2026-08-24T09:05Z',
                    ?, ?, 'committed', '{"valid": true}')
            """,
            [now, now, now, now],
        )
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    sender = FailingThenWorkingSender()
    service = NotificationService(system.store, target="telegram:configured-at-runtime", sender=sender)

    with pytest.raises(NotificationError):
        service.send_committed_run("committed-run", report)
    with duckdb.connect(str(database), read_only=True) as connection:
        before = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
        status = connection.execute(
            "SELECT status, attempt_count FROM paper_notifications WHERE run_id='committed-run'"
        ).fetchone()
    assert status == ("FAILED", 1)

    service.resend("committed-run")

    with duckdb.connect(str(database), read_only=True) as connection:
        after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
        status = connection.execute(
            "SELECT status, attempt_count FROM paper_notifications WHERE run_id='committed-run'"
        ).fetchone()
        attempts = connection.execute(
            "SELECT COUNT(*) FROM notification_attempts WHERE run_id='committed-run'"
        ).fetchone()[0]
    assert before == after == (0, 0)
    assert status == ("DELIVERED", 2)
    assert attempts == 2
    assert sender.calls == 2


def test_notification_refuses_running_transaction(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, status, mode) "
            "VALUES ('running', ?, 'RUNNING', 'PAPER')",
            [now],
        )
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    service = NotificationService(system.store, target="telegram", sender=lambda *_: None)

    with pytest.raises(NotificationError):
        service.send_committed_run("running", report)
