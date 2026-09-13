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
    service = NotificationService(system.store, target="telegram:test-target", sender=sender)

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


def test_process_interruption_after_durable_send_claim_preserves_ambiguous_delivery(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('interrupted', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")

    externally_sent = []

    def interrupted_sender(target, report_path):
        externally_sent.append((target, report_path))
        raise KeyboardInterrupt

    service = NotificationService(
        system.store,
        target="telegram:test-target",
        sender=interrupted_sender,
    )
    with pytest.raises(KeyboardInterrupt):
        service.send_committed_run("interrupted", report)

    with system.store.connect(read_only=True) as connection:
        state = connection.execute(
            "SELECT status, attempt_count, report_path FROM paper_notifications "
            "WHERE run_id='interrupted'"
        ).fetchone()
        trades = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
    assert state[:2] == ("SENDING", 1)
    assert state[2] == str(report.resolve())
    assert trades == (0, 0)
    assert externally_sent == [("telegram:test-target", report.resolve())]


@pytest.mark.parametrize("status", ["PENDING", "FAILED"])
def test_resend_allows_only_retryable_notification_states(tmp_path, status):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('retryable', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
        connection.execute(
            "INSERT INTO paper_notifications VALUES (?, ?, ?, ?, 0, NULL, ?, ?, NULL)",
            ["retryable", "telegram:test", str(report.resolve()), status, now, now],
        )
    sent = []
    service = NotificationService(
        system.store,
        target="",
        sender=lambda target, path: sent.append((target, path)) or {"ok": True},
    )

    service.resend("retryable")

    assert sent == [("telegram:test", report.resolve())]


def test_resend_refuses_delivered_notification_without_sending(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('delivered', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
        connection.execute(
            "INSERT INTO paper_notifications VALUES (?, ?, ?, 'DELIVERED', 1, NULL, ?, ?, ?)",
            ["delivered", "telegram:test", str(report.resolve()), now, now, now],
        )
    sent = []
    service = NotificationService(system.store, target="", sender=lambda *args: sent.append(args))

    with pytest.raises(NotificationError, match="already delivered"):
        service.resend("delivered")

    assert sent == []


def test_resend_refuses_unknown_run_without_sending(tmp_path):
    system = PaperTradingSystem(
        tmp_path / "paper.duckdb", PaperConfig(assets=("BTC/USDT",))
    )
    sent = []
    service = NotificationService(system.store, target="", sender=lambda *args: sent.append(args))

    with pytest.raises(NotificationError, match="No prior notification record"):
        service.resend("unknown")

    assert sent == []


def test_durable_pre_send_claim_records_stable_attempt_without_invoking_sender(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('claimed', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
    sent = []
    service = NotificationService(
        system.store,
        target="telegram:test",
        sender=lambda *args: sent.append(args),
    )
    service.register_pending("claimed", report)

    attempt_id = service._claim_attempt("claimed", report.resolve(), "telegram:test")

    with system.store.connect(read_only=True) as connection:
        notification = connection.execute(
            "SELECT status, attempt_count FROM paper_notifications WHERE run_id='claimed'"
        ).fetchone()
        attempt = connection.execute(
            "SELECT attempt_id, status FROM notification_attempts WHERE run_id='claimed'"
        ).fetchone()
    assert notification == ("SENDING", 1)
    assert attempt == (attempt_id, "SENDING")
    assert sent == []
    with pytest.raises(NotificationError, match="manual recovery"):
        service.resend("claimed")


def test_sender_success_before_local_delivery_commit_is_not_automatically_duplicated(
    tmp_path, monkeypatch
):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('ambiguous', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
    sent = []
    service = NotificationService(
        system.store,
        target="telegram:test",
        sender=lambda *args: sent.append(args) or {"ok": True},
    )
    monkeypatch.setattr(
        service,
        "_mark_delivered",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("local delivery commit interrupted")),
    )

    with pytest.raises(RuntimeError, match="local delivery commit interrupted"):
        service.send_committed_run("ambiguous", report)

    with system.store.connect(read_only=True) as connection:
        notification = connection.execute(
            "SELECT status, attempt_count FROM paper_notifications WHERE run_id='ambiguous'"
        ).fetchone()
        attempt = connection.execute(
            "SELECT status FROM notification_attempts WHERE run_id='ambiguous'"
        ).fetchone()
    assert notification == ("SENDING", 1)
    assert attempt == ("SENDING",)
    with pytest.raises(NotificationError, match="manual recovery"):
        service.resend("ambiguous")
    assert len(sent) == 1


def test_concurrent_claim_for_same_notification_is_serialized(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    report = tmp_path / "report.md"
    report.write_text("virtual report", encoding="utf-8")
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('serialized', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
    first = NotificationService(system.store, target="telegram:test", sender=lambda *_: None)
    second = NotificationService(system.store, target="telegram:test", sender=lambda *_: None)
    first.register_pending("serialized", report)

    first._claim_attempt("serialized", report.resolve(), "telegram:test")
    with pytest.raises(NotificationError, match="manual recovery"):
        second._claim_attempt("serialized", report.resolve(), "telegram:test")


def test_send_committed_run_reuses_persisted_target_and_report_after_failure(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('stable-destination', ?, ?, 'EXECUTED', 'PAPER')",
            [now, now],
        )
    original_report = tmp_path / "original.md"
    original_report.write_text("original", encoding="utf-8")
    replacement_report = tmp_path / "replacement.md"
    replacement_report.write_text("replacement", encoding="utf-8")
    failing = NotificationService(
        system.store,
        target="telegram:original",
        sender=lambda *_: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    with pytest.raises(NotificationError, match="unavailable"):
        failing.send_committed_run("stable-destination", original_report)
    sent = []
    retry = NotificationService(
        system.store,
        target="telegram:replacement",
        sender=lambda target, report: sent.append((target, report)) or {"ok": True},
    )

    retry.send_committed_run("stable-destination", replacement_report)

    assert sent == [("telegram:original", original_report.resolve())]
