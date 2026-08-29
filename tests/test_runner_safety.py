from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import duckdb

import run_paper
from src.forward_operations import AlreadyRunningError, InterProcessLock
from src.config import load_settings
from src.paper_broker import PaperConfig


ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, (5, 1.0, 30000)),
        (
            {
                "HCL_MAX_RETRIES": "9",
                "HCL_BACKOFF_BASE_SECONDS": "2.5",
                "HCL_REQUEST_TIMEOUT_MS": "45000",
            },
            (9, 2.5, 45000),
        ),
    ],
)
def test_paper_market_runtime_settings_reach_adapter(
    monkeypatch, tmp_path, overrides, expected
):
    for variable in (
        "HCL_MAX_RETRIES",
        "HCL_BACKOFF_BASE_SECONDS",
        "HCL_REQUEST_TIMEOUT_MS",
    ):
        monkeypatch.delenv(variable, raising=False)
    for variable, value in overrides.items():
        monkeypatch.setenv(variable, value)
    settings = load_settings(tmp_path)
    captured = {}

    def adapter(config, **kwargs):
        captured.update(kwargs)
        return config

    monkeypatch.setattr(run_paper, "fetch_public_market_snapshot", adapter)

    config = PaperConfig(assets=ASSETS)
    assert run_paper.fetch_configured_public_market_snapshot(config, settings) is config
    assert (
        captured["max_retries"],
        captured["backoff_base_seconds"],
        captured["timeout_ms"],
    ) == expected


def test_locked_system_does_not_open_database_when_writer_lock_is_busy(tmp_path):
    database = tmp_path / "paper.duckdb"
    lock = tmp_path / "forward_writer.lock"
    config = PaperConfig(assets=ASSETS)

    with InterProcessLock(lock, command_name="holder"):
        with pytest.raises(AlreadyRunningError):
            with run_paper.open_locked_system(
                database_path=database,
                config=config,
                project_root=tmp_path,
                lock_path=lock,
                command_name="contender",
                bootstrap=False,
            ):
                pass

    assert not database.exists()


def test_telegram_target_comes_only_from_runtime_configuration(monkeypatch):
    monkeypatch.delenv("HCL_TELEGRAM_TARGET", raising=False)
    with pytest.raises(ValueError, match="HCL_TELEGRAM_TARGET"):
        run_paper.resolve_telegram_target(None)

    monkeypatch.setenv("HCL_TELEGRAM_TARGET", "telegram:test-target")
    assert run_paper.resolve_telegram_target(None) == "telegram:test-target"
    assert run_paper.resolve_telegram_target("telegram:cli-target") == "telegram:cli-target"


def test_dry_run_reaches_local_execution_without_telegram_target(monkeypatch, tmp_path):
    class LocalExecutionReached(RuntimeError):
        pass

    @contextmanager
    def open_probe(**_kwargs):
        raise LocalExecutionReached
        yield

    settings = SimpleNamespace(
        project_root=tmp_path,
        logs_dir=tmp_path / "logs",
        log_level="INFO",
    )
    values = {
        "database_path": "paper.duckdb",
        "reports_dir": "reports",
        "default_dry_run": True,
    }
    monkeypatch.delenv("HCL_TELEGRAM_TARGET", raising=False)
    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(
        run_paper,
        "load_paper_configuration",
        lambda _root: (PaperConfig(assets=ASSETS), values),
    )
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "hash")
    monkeypatch.setattr(run_paper, "open_locked_system", open_probe)
    monkeypatch.setattr(
        run_paper,
        "resolve_telegram_target",
        lambda _target: (_ for _ in ()).throw(
            AssertionError("Telegram target resolved for local-only dry-run")
        ),
    )
    monkeypatch.setattr("sys.argv", ["run_paper.py", "--dry-run"])

    with pytest.raises(LocalExecutionReached):
        run_paper.main()


@pytest.mark.parametrize("mode", ["--status", "--reconcile", "--kill-switch-status"])
def test_inspection_cli_modes_work_without_telegram_target(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("HCL_TELEGRAM_TARGET", None)
    environment["HCL_PAPER_DATABASE"] = str(
        tmp_path / f"{mode.removeprefix('--')}.duckdb"
    )

    completed = subprocess.run(
        [sys.executable, str(root / "run_paper.py"), mode],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "HCL_TELEGRAM_TARGET" not in completed.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ("--status", "--reconcile"),
        ("--audit-missed", "--startup-audit"),
        ("--paper", "--status"),
        ("--resend", "run-id", "--sample-telegram"),
    ],
)
def test_conflicting_primary_cli_modes_fail_at_argument_parsing(arguments, tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["HCL_PAPER_DATABASE"] = str(tmp_path / "must-not-open.duckdb")

    completed = subprocess.run(
        [sys.executable, str(root / "run_paper.py"), *arguments],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 2
    assert "not allowed with argument" in completed.stderr


@pytest.mark.parametrize(
    "after_window",
    [
        datetime(2026, 1, 5, 0, 21, tzinfo=timezone.utc),
        datetime(2026, 1, 6, 0, 1, tzinfo=timezone.utc),
    ],
)
def test_paper_run_records_miss_when_window_closes_during_fetch(
    monkeypatch, tmp_path, after_window
):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "deadline.duckdb"
    config, values = run_paper.load_paper_configuration(root)
    inside_window = datetime(2026, 1, 5, 0, 19, tzinfo=timezone.utc)

    clock_values = iter([inside_window, after_window, after_window, after_window])
    fetch_calls = 0

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = next(clock_values)
            return value if tz is not None else value.replace(tzinfo=None)

    def fetch_snapshot(*_args, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return object()

    def forbidden_strategy_execution(*_args, **_kwargs):
        raise AssertionError("strategy execution crossed the governed deadline")

    values = {
        **values,
        "database_path": str(database),
        "reports_dir": str(tmp_path / "reports"),
    }
    settings = SimpleNamespace(
        project_root=root,
        logs_dir=tmp_path / "logs",
        log_level="INFO",
        max_retries=5,
        backoff_base_seconds=1.0,
        request_timeout_ms=30000,
    )
    monkeypatch.setattr(run_paper, "datetime", ControlledDateTime)
    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(
        run_paper,
        "_experiment_start",
        lambda _root: run_paper.pd.Timestamp("2026-01-05T00:00:00Z"),
    )
    monkeypatch.setattr(run_paper, "resolve_telegram_target", lambda _target: "local-test")
    monkeypatch.setattr(run_paper, "fetch_public_market_snapshot", fetch_snapshot)
    monkeypatch.setattr(
        run_paper, "build_forward_diagnostics", forbidden_strategy_execution
    )
    monkeypatch.setattr(
        run_paper.PaperTradingSystem, "run", forbidden_strategy_execution
    )
    monkeypatch.setattr("sys.argv", ["run_paper.py", "--paper"])

    run_paper.main()
    run_paper.main()

    with duckdb.connect(str(database), read_only=True) as connection:
        orders = connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        fills = connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        equity = connection.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        incidents = connection.execute(
            "SELECT incident_type, scheduled_for_utc FROM forward_incidents"
        ).fetchall()
        windows = connection.execute(
            "SELECT outcome FROM forward_schedule_windows"
        ).fetchall()

    assert fetch_calls == 1
    assert (orders, fills, equity) == (0, 0, 0)
    assert len(incidents) == 1
    assert incidents[0][0] == "MISSED_SCHEDULE"
    assert run_paper.pd.Timestamp(incidents[0][1]).tz_convert("UTC") == run_paper.pd.Timestamp(
        "2026-01-05T00:10:00Z"
    )
    assert windows == [("MISSED_SCHEDULE",)]
