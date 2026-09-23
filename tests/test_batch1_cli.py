"""Notification-only orchestration and read-only visibility regressions."""
from datetime import datetime, timezone
import hashlib
import json
import sys

import duckdb
import pytest

import run_paper
from src.paper_notifications import NotificationService
from src.paper_store import PaperStore


def _store(tmp_path):
    store = PaperStore(tmp_path / "paper.duckdb", account_id="test", initial_cash=1000)
    now = datetime.now(timezone.utc)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('ready', ?, ?, 'EXECUTED', 'PAPER'), ('abandoned', ?, NULL, 'RUNNING', 'PAPER')",
            [now, now, now],
        )
    report = tmp_path / "report.md"
    report.write_bytes(b"immutable notification\r\n")
    NotificationService(store, target="persisted-target", sender=lambda *_: None).register_pending(
        "ready", report
    )
    return store


def _isolate(monkeypatch, tmp_path, database):
    monkeypatch.setattr(run_paper, "diagnostic_project_root", lambda: tmp_path)
    monkeypatch.setenv("HCL_PAPER_DATABASE", str(database))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("notification action entered trading runtime")

    for name in ("load_settings", "load_paper_configuration", "_verify_research_lock",
                 "bootstrap_forward_experiment", "open_locked_system",
                 "fetch_configured_public_market_snapshot", "fetch_public_market_snapshot"):
        monkeypatch.setattr(run_paper, name, forbidden)
    monkeypatch.setattr(PaperStore, "__init__", forbidden)
    monkeypatch.setattr(PaperStore, "recover_abandoned_runs", forbidden)
    monkeypatch.setattr(run_paper.PaperTradingSystem, "run", forbidden)


def test_resend_is_notification_only_and_preserves_abandoned_run(monkeypatch, tmp_path):
    store = _store(tmp_path)
    with store.connect(read_only=True) as connection:
        tables = [row[0] for row in connection.execute("SHOW TABLES").fetchall()
                  if row[0] not in {"paper_notifications", "notification_attempts", "notification_recovery_events"}]
        before = {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
    _isolate(monkeypatch, tmp_path, store.path)
    sent = []
    monkeypatch.setattr(run_paper, "HermesTelegramSender", lambda: lambda *args: sent.append(args))
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--resend", "ready"])
    run_paper.main()
    assert sent == [("persisted-target", (tmp_path / "report.md").resolve())]
    with duckdb.connect(str(store.path), read_only=True) as connection:
        after = {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
    assert after == before


@pytest.mark.parametrize("kind", ["missing", "incompatible"])
def test_resend_bad_database_creates_nothing(monkeypatch, tmp_path, capsys, kind):
    database = tmp_path / "absent" / "paper.duckdb"
    if kind == "incompatible":
        database = tmp_path / "bad.duckdb"
        with duckdb.connect(str(database)) as connection:
            connection.execute("CREATE TABLE unrelated (n INTEGER)")
    _isolate(monkeypatch, tmp_path, database)
    before = {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in tmp_path.rglob("*") if p.is_file()}
    paths = set(tmp_path.rglob("*"))
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--resend", "ready"])
    with pytest.raises(SystemExit) as error:
        run_paper.main()
    assert error.value.code == 2
    assert "notification" in capsys.readouterr().err.lower()
    assert set(tmp_path.rglob("*")) == paths
    assert {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tmp_path.rglob("*") if p.is_file()} == before


def test_status_exposes_all_states_without_writing(tmp_path):
    store = _store(tmp_path)
    states = ("PENDING", "SENDING", "DELIVERY_UNKNOWN", "FAILED", "DELIVERED")
    with store.connect() as connection:
        connection.execute("DELETE FROM paper_notifications")
        for state in states:
            connection.execute(
                "INSERT INTO paper_notifications "
                "(run_id, target, report_path, status, attempt_count, created_at_utc, updated_at_utc) "
                "VALUES (?, 'target', 'report', ?, 0, now(), now())", [state, state]
            )
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    result = run_paper._status(store)
    assert result["notifications"] == {state: 1 for state in states}
    assert result["ambiguous_notifications"] == 2
    assert result["failed_notifications"] == 1
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    json.dumps(result)
