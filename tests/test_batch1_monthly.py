"""Monthly CLI integration against the real publication and durable outbox."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_monthly_report
from src.forward_monthly import is_monthly_report_committed
from src.paper_broker import PaperConfig, PaperTradingSystem
from src.paper_notifications import NotificationError, NotificationService


REPORT_DATE = datetime(2026, 9, 1, tzinfo=timezone.utc)
EXPERIMENT_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
NOTIFICATION_ID = "monthly:forward-monthly-test:2026-08"
TARGET = "telegram:monthly-test"


@pytest.fixture
def monthly_cli(tmp_path, monkeypatch):
    config = PaperConfig(assets=("BTC/USDT",))
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, config)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "(?, ?, 'locked', 'hash', 'govhash', '{}', 'ACTIVE')",
            ["forward-monthly-test", EXPERIMENT_START],
        )
    governance = tmp_path / "forward_experiment" / "governance.json"
    governance.parent.mkdir()
    governance.write_text(
        json.dumps({"experiment_id": "forward-monthly-test"}), encoding="utf-8"
    )
    state = SimpleNamespace(
        store=system.store,
        output_dir=tmp_path / "reports" / "forward_monthly",
        locked=False,
        lock_entries=0,
        research_checks=0,
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return REPORT_DATE

    def verify_research_lock(root, passed_config):
        assert root == tmp_path
        assert passed_config is config
        state.research_checks += 1

    @contextmanager
    def locked_system(**kwargs):
        assert kwargs == {
            "database_path": database,
            "config": config,
            "project_root": tmp_path,
            "lock_path": tmp_path / "runtime" / "forward_writer.lock",
            "command_name": "monthly-forward-report",
        }
        assert state.research_checks == state.lock_entries + 1
        state.lock_entries += 1
        state.locked = True
        try:
            yield system
        finally:
            state.locked = False

    monkeypatch.setattr(run_monthly_report, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        run_monthly_report, "load_settings", lambda: SimpleNamespace(project_root=tmp_path)
    )
    monkeypatch.setattr(
        run_monthly_report, "load_paper_configuration", lambda root: (config, {})
    )
    monkeypatch.setattr(
        run_monthly_report, "_project_paths", lambda *args: (database, tmp_path)
    )
    monkeypatch.setattr(run_monthly_report, "_verify_research_lock", verify_research_lock)
    monkeypatch.setattr(run_monthly_report, "open_locked_system", locked_system)
    monkeypatch.setattr(run_monthly_report, "_experiment_start", lambda root: EXPERIMENT_START)
    monkeypatch.setenv("HCL_TELEGRAM_TARGET", TARGET)
    return state


def _notification(store):
    with store.connect(read_only=True) as connection:
        cursor = connection.execute(
            "SELECT * FROM paper_notifications WHERE run_id=?", [NOTIFICATION_ID]
        )
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
    return None if row is None else dict(zip(columns, row, strict=True))


def _attempts(store):
    with store.connect(read_only=True) as connection:
        return connection.execute(
            "SELECT attempt_id, status FROM notification_attempts "
            "WHERE run_id=? ORDER BY attempted_at_utc, attempt_id", [NOTIFICATION_ID]
        ).fetchall()


def test_monthly_claim_is_durable_before_sender(monthly_cli, monkeypatch):
    sent = []

    def sender(target, report_path):
        assert monthly_cli.locked
        assert is_monthly_report_committed(monthly_cli.output_dir, "2026-08")
        row = _notification(monthly_cli.store)
        assert row is not None, "monthly sender was invoked without a durable outbox row"
        assert (row["status"], row["attempt_count"]) == ("SENDING", 1)
        assert (row["target"], Path(row["report_path"])) == (target, report_path)
        assert row["notification_kind"] == "MONTHLY"
        assert row["report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()
        marker = json.loads(
            report_path.with_suffix(".complete.json").read_text(encoding="utf-8")
        )
        assert row["report_sha256"] == marker["markdown_sha256"]
        attempts = _attempts(monthly_cli.store)
        assert len(attempts) == 1
        assert attempts[0][1] == "SENDING"
        sent.append((target, report_path))
        return {"ok": True}

    monkeypatch.setattr(run_monthly_report, "HermesTelegramSender", lambda: sender)
    run_monthly_report.main()

    assert len(sent) == 1
    assert sent[0][0] == TARGET
    assert _notification(monthly_cli.store)["status"] == "DELIVERED"
    assert _attempts(monthly_cli.store)[0][1] == "DELIVERED"
    assert not monthly_cli.locked
    with monthly_cli.store.connect(read_only=True) as connection:
        for table in ("paper_runs", "paper_orders", "paper_fills", "forward_baselines"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM forward_schedule_windows WHERE outcome='MISSED_SCHEDULE'"
        ).fetchone()[0] > 0


def test_monthly_rerun_delegates_persisted_target_without_environment(monthly_cli, monkeypatch):
    result = run_monthly_report.generate_monthly_forward_report(
        monthly_cli.store,
        experiment_id="forward-monthly-test",
        report_date=REPORT_DATE,
        output_dir=monthly_cli.output_dir,
        assets=("BTC/USDT",),
        slippage_rate=0.0005,
    )
    report_path = result["report_path"].resolve()
    with monthly_cli.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_notifications "
            "(run_id, target, report_path, status, attempt_count, created_at_utc, updated_at_utc, "
            "delivered_at_utc, notification_kind, report_sha256) "
            "VALUES (?, ?, ?, 'DELIVERED', 1, ?, ?, ?, 'MONTHLY', ?)",
            [
                NOTIFICATION_ID, TARGET, str(report_path), REPORT_DATE, REPORT_DATE,
                REPORT_DATE, hashlib.sha256(report_path.read_bytes()).hexdigest(),
            ],
        )
    calls = []

    def send_committed_monthly(service, notification_id, path):
        assert service.target == ""
        assert _notification(service.store)["target"] == TARGET
        calls.append((notification_id, path))
        return {"ok": True, "already_delivered": True}

    # Isolate the CLI environment gate from the independently owned service API.
    monkeypatch.setattr(
        NotificationService, "send_committed_monthly", send_committed_monthly, raising=False
    )
    monkeypatch.delenv("HCL_TELEGRAM_TARGET")
    run_monthly_report.main()

    assert calls == [(NOTIFICATION_ID, report_path)]


def test_monthly_outbox_rejects_existing_paper_notification_id(monthly_cli):
    result = run_monthly_report.generate_monthly_forward_report(
        monthly_cli.store,
        experiment_id="forward-monthly-test",
        report_date=REPORT_DATE,
        output_dir=monthly_cli.output_dir,
        assets=("BTC/USDT",),
        slippage_rate=0.0005,
    )
    report_path = result["report_path"].resolve()
    with monthly_cli.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_notifications "
            "(run_id, target, report_path, status, attempt_count, created_at_utc, updated_at_utc, "
            "notification_kind, report_sha256) "
            "VALUES (?, ?, ?, 'PENDING', 0, ?, ?, 'PAPER', ?)",
            [
                NOTIFICATION_ID, TARGET, str(report_path), REPORT_DATE, REPORT_DATE,
                hashlib.sha256(report_path.read_bytes()).hexdigest(),
            ],
        )
    sent = []
    service = NotificationService(
        monthly_cli.store,
        target=TARGET,
        sender=lambda *args: sent.append(args) or {"ok": True},
    )

    with pytest.raises(NotificationError, match="not a MONTHLY notification"):
        service.send_committed_monthly(NOTIFICATION_ID, report_path)

    assert sent == []
    with monthly_cli.store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT status, attempt_count, notification_kind FROM paper_notifications WHERE run_id=?",
            [NOTIFICATION_ID],
        ).fetchone()
    assert row == ("PENDING", 0, "PAPER")


def _publication_bytes(output_dir):
    return {
        path.name: path.read_bytes()
        for path in output_dir.glob("forward_monthly_2026-08.*")
    }


@pytest.mark.parametrize("rerun_target", [None, "telegram:changed-target"])
def test_delivered_monthly_rerun_is_noop(monthly_cli, monkeypatch, rerun_target):
    sent = []
    monkeypatch.setattr(
        run_monthly_report, "HermesTelegramSender", lambda: lambda *args: sent.append(args)
    )
    run_monthly_report.main()
    delivered = _notification(monthly_cli.store)
    attempts = _attempts(monthly_cli.store)
    publication = _publication_bytes(monthly_cli.output_dir)
    if rerun_target is None:
        monkeypatch.delenv("HCL_TELEGRAM_TARGET")
    else:
        monkeypatch.setenv("HCL_TELEGRAM_TARGET", rerun_target)

    run_monthly_report.main()

    assert len(sent) == 1
    assert _notification(monthly_cli.store) == delivered
    assert _attempts(monthly_cli.store) == attempts
    assert _publication_bytes(monthly_cli.output_dir) == publication
    assert monthly_cli.lock_entries == 2


class SimulatedProcessCrash(BaseException):
    pass


@pytest.mark.parametrize("failure_point", ["inside_sender", "delivery_commit"])
def test_ambiguous_monthly_delivery_blocks_rerun(monthly_cli, monkeypatch, failure_point):
    sent = []

    def sender(target, report_path):
        sent.append((target, report_path))
        if failure_point == "inside_sender":
            raise SimulatedProcessCrash("external receipt cannot be determined")
        return {"ok": True}

    def failed_delivery_commit(*args, **kwargs):
        raise RuntimeError("local delivery commit interrupted")

    monkeypatch.setattr(run_monthly_report, "HermesTelegramSender", lambda: sender)
    with monkeypatch.context() as fault:
        if failure_point == "delivery_commit":
            fault.setattr(NotificationService, "_mark_delivered", failed_delivery_commit)
            expected_error = RuntimeError
        else:
            expected_error = SimulatedProcessCrash
        with pytest.raises(expected_error):
            run_monthly_report.main()

    ambiguous = _notification(monthly_cli.store)
    assert ambiguous["status"] in {"SENDING", "DELIVERY_UNKNOWN"}
    assert ambiguous["attempt_count"] == 1
    attempts = _attempts(monthly_cli.store)
    assert len(attempts) == 1
    assert attempts[0][1] in {"SENDING", "DELIVERY_UNKNOWN"}
    publication = _publication_bytes(monthly_cli.output_dir)
    monkeypatch.delenv("HCL_TELEGRAM_TARGET")

    with pytest.raises(NotificationError, match="manual recovery|ambiguous|unknown"):
        run_monthly_report.main()

    assert len(sent) == 1
    assert _notification(monthly_cli.store) == ambiguous
    assert _attempts(monthly_cli.store) == attempts
    assert _publication_bytes(monthly_cli.output_dir) == publication


@pytest.mark.parametrize("target", [None, "", "   "])
def test_new_monthly_delivery_requires_target(monthly_cli, monkeypatch, target):
    sent = []
    monkeypatch.setattr(
        run_monthly_report, "HermesTelegramSender", lambda: lambda *args: sent.append(args)
    )
    if target is None:
        monkeypatch.delenv("HCL_TELEGRAM_TARGET")
    else:
        monkeypatch.setenv("HCL_TELEGRAM_TARGET", target)

    with pytest.raises(NotificationError, match="(?i)target"):
        run_monthly_report.main()

    assert is_monthly_report_committed(monthly_cli.output_dir, "2026-08")
    assert _notification(monthly_cli.store) is None
    assert _attempts(monthly_cli.store) == []
    assert sent == []


@pytest.mark.parametrize("artifact", ["report_path", "json_path", "completion_path"])
def test_monthly_invalid_committed_publication_never_registers_outbox(
    monthly_cli, monkeypatch, artifact
):
    result = run_monthly_report.generate_monthly_forward_report(
        monthly_cli.store,
        experiment_id="forward-monthly-test",
        report_date=REPORT_DATE,
        output_dir=monthly_cli.output_dir,
        assets=("BTC/USDT",),
        slippage_rate=0.0005,
    )
    result[artifact].write_text("corrupted", encoding="utf-8")
    sent = []
    monkeypatch.setattr(
        run_monthly_report, "HermesTelegramSender", lambda: lambda *args: sent.append(args)
    )

    with pytest.raises(ValueError, match="hash|completion marker"):
        run_monthly_report.main()

    assert _notification(monthly_cli.store) is None
    assert _attempts(monthly_cli.store) == []
    assert sent == []


def test_monthly_partial_publication_does_not_register_until_recovery(monthly_cli, monkeypatch):
    sent = []
    monkeypatch.setattr(
        run_monthly_report, "HermesTelegramSender", lambda: lambda *args: sent.append(args)
    )
    generate = run_monthly_report.generate_monthly_forward_report

    def interrupt(stage):
        if stage == "before_completion_marker":
            raise RuntimeError("publication interrupted")

    def interrupted_publication(*args, **kwargs):
        return generate(*args, **kwargs, publication_hook=interrupt)

    with monkeypatch.context() as fault:
        fault.setattr(run_monthly_report, "generate_monthly_forward_report", interrupted_publication)
        with pytest.raises(RuntimeError, match="publication interrupted"):
            run_monthly_report.main()

    assert not is_monthly_report_committed(monthly_cli.output_dir, "2026-08")
    assert _notification(monthly_cli.store) is None
    assert sent == []
    partial = _publication_bytes(monthly_cli.output_dir)
    assert len(partial) == 2

    run_monthly_report.main()

    assert is_monthly_report_committed(monthly_cli.output_dir, "2026-08")
    assert len(sent) == 1
    assert _notification(monthly_cli.store)["status"] == "DELIVERED"
    assert all(_publication_bytes(monthly_cli.output_dir)[name] == data for name, data in partial.items())


@pytest.mark.parametrize("rerun_target", [None, "telegram:changed-target"])
def test_failed_monthly_delivery_retries_persisted_report_and_target(
    monthly_cli, monkeypatch, rerun_target
):
    sent = []

    def sender(target, report_path):
        sent.append((target, report_path))
        if len(sent) == 1:
            raise RuntimeError("confirmed delivery failure")
        return {"ok": True}

    monkeypatch.setattr(run_monthly_report, "HermesTelegramSender", lambda: sender)
    with pytest.raises(NotificationError, match="confirmed delivery failure"):
        run_monthly_report.main()

    failed = _notification(monthly_cli.store)
    assert (failed["status"], failed["attempt_count"]) == ("FAILED", 1)
    assert failed["last_error"] == "confirmed delivery failure"
    assert _attempts(monthly_cli.store)[0][1] == "FAILED"
    publication = _publication_bytes(monthly_cli.output_dir)
    assert len(publication) == 3
    with monthly_cli.store.connect(read_only=True) as connection:
        missed_windows = connection.execute(
            "SELECT * FROM forward_schedule_windows ORDER BY schedule_key"
        ).fetchall()
        incidents = connection.execute(
            "SELECT * FROM forward_incidents ORDER BY incident_id"
        ).fetchall()
    if rerun_target is None:
        monkeypatch.delenv("HCL_TELEGRAM_TARGET")
    else:
        monkeypatch.setenv("HCL_TELEGRAM_TARGET", rerun_target)

    run_monthly_report.main()

    delivered = _notification(monthly_cli.store)
    assert (delivered["status"], delivered["attempt_count"]) == ("DELIVERED", 2)
    assert delivered["last_error"] is None
    assert delivered["report_sha256"] == failed["report_sha256"]
    assert delivered["target"] == TARGET
    assert delivered["report_path"] == failed["report_path"]
    assert sent == [(TARGET, Path(failed["report_path"]))] * 2
    attempts = _attempts(monthly_cli.store)
    assert [row[1] for row in attempts] == ["FAILED", "DELIVERED"]
    assert len({row[0] for row in attempts}) == 2
    assert _publication_bytes(monthly_cli.output_dir) == publication
    with monthly_cli.store.connect(read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_notifications"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT * FROM forward_schedule_windows ORDER BY schedule_key"
        ).fetchall() == missed_windows
        assert connection.execute(
            "SELECT * FROM forward_incidents ORDER BY incident_id"
        ).fetchall() == incidents
        for table in ("paper_runs", "paper_orders", "paper_fills", "forward_baselines"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
