from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import duckdb
import pandas as pd

import run_paper
from src.forward_operations import AlreadyRunningError, InterProcessLock
from src.config import load_settings
from src.paper_broker import PaperConfig


ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")


def test_canonical_backtest_ignores_economic_environment_overrides(tmp_path, monkeypatch):
    import yaml

    from run_backtest import load_run_configuration

    source = Path(__file__).resolve().parents[1] / "config" / "strategy.yaml"
    target = tmp_path / "config"
    target.mkdir()
    target.joinpath("strategy.yaml").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    expected = yaml.safe_load(source.read_text(encoding="utf-8"))["backtest"]
    monkeypatch.setenv("HCL_INITIAL_CASH", "999999")
    monkeypatch.setenv("HCL_FEE_RATE", "0")
    monkeypatch.setenv("HCL_SLIPPAGE_RATE", "0")

    _strategy, backtest = load_run_configuration(tmp_path)

    assert backtest.initial_cash == float(expected["initial_cash"])
    assert backtest.fee_rate == float(expected["fee_rate"])
    assert backtest.slippage_rate == float(expected["slippage_rate"])


def test_backtest_cli_identifies_fixed_baseline_not_locked_candidate(monkeypatch, capsys):
    import run_backtest

    monkeypatch.setattr(
        run_backtest,
        "run_research_backtest",
        lambda: {
            "run_id": "run-1",
            "paths": {"comparison_markdown": "comparison.md"},
        },
    )

    run_backtest.main()

    output = capsys.readouterr().out
    assert "Historical fixed-baseline backtest" in output
    assert "not the locked forward candidate evaluation" in output


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
    ("mode", "expected"),
    [
        ("--status", '"reconciliation"'),
        ("--reconcile", '"valid": true'),
        ("--kill-switch-status", '"automatic_reset": false'),
        ("--reset-kill-switch", "kill switch reset"),
    ],
)
def test_operational_cli_modes_execute_in_process_for_coverage(
    monkeypatch, tmp_path, capsys, mode, expected
):
    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    values = {
        **values,
        "database_path": str(tmp_path / "operational.duckdb"),
        "reports_dir": str(tmp_path / "reports"),
    }
    settings = SimpleNamespace(
        project_root=root,
        logs_dir=tmp_path / "logs",
        log_level="INFO",
    )
    monkeypatch.delenv("HCL_TELEGRAM_TARGET", raising=False)
    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(sys, "argv", ["run_paper.py", mode])

    run_paper.main()

    assert expected in capsys.readouterr().out.lower()


def test_project_paths_resolve_relative_runtime_locations_and_env_override(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("HCL_PAPER_DATABASE", raising=False)
    database, reports = run_paper._project_paths(
        tmp_path,
        {"database_path": "database/paper.duckdb", "reports_dir": "reports"},
    )
    assert database == tmp_path / "database/paper.duckdb"
    assert reports == tmp_path / "reports"

    override = (tmp_path / "override.duckdb").resolve()
    monkeypatch.setenv("HCL_PAPER_DATABASE", str(override))
    database, _ = run_paper._project_paths(
        tmp_path,
        {"database_path": "ignored.duckdb", "reports_dir": str(tmp_path / "absolute")},
    )
    assert database == override


def test_schedule_helpers_fail_closed_for_naive_timestamps_and_missing_governance(
    tmp_path,
):
    project_root = Path(__file__).resolve().parents[1]
    config, _ = run_paper.load_paper_configuration(project_root)
    before = pd.Timestamp.now(tz="UTC")
    inferred = run_paper._experiment_start(tmp_path)
    after = pd.Timestamp.now(tz="UTC")
    assert before <= inferred <= after
    with pytest.raises(ValueError, match="timezone-aware"):
        run_paper._current_schedule_window_closed(datetime(2026, 1, 5, 0, 21), config)
    with pytest.raises(ValueError, match="timezone-aware"):
        run_paper._schedule_window_deadline("2026-01-05T00:05:00", config)


def test_active_paper_strategy_is_derived_from_locked_candidate_identity():
    project_root = Path(__file__).resolve().parents[1]

    config, _ = run_paper.load_paper_configuration(project_root)

    assert config.locked_candidate_id == "mw120_sw00_ma150_n2_r07_v30"
    assert config.strategy_config.momentum_long_days == 120
    assert config.strategy_config.momentum_skip_days == 0
    assert config.strategy_config.btc_moving_average_days == 150
    assert config.strategy_config.max_assets == 2
    assert config.rebalance_days == 7
    assert config.strategy_config.volatility_days == 30


def test_sample_notification_cli_writes_paper_only_report_and_delivers(
    monkeypatch, tmp_path, capsys
):
    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    values = {
        **values,
        "database_path": str(tmp_path / "sample.duckdb"),
        "reports_dir": str(tmp_path / "reports"),
    }
    settings = SimpleNamespace(
        project_root=root,
        logs_dir=tmp_path / "logs",
        log_level="INFO",
    )
    delivered = []
    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(
        run_paper,
        "HermesTelegramSender",
        lambda: lambda target, path: delivered.append((target, path)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_paper.py", "--sample-telegram", "--telegram-target", "local-test"],
    )

    run_paper.main()

    assert len(delivered) == 1
    assert delivered[0][0] == "local-test"
    assert "SAMPLE_NOTIFICATION_ONLY" in delivered[0][1].read_text(encoding="utf-8")
    assert "Sample Telegram report delivered" in capsys.readouterr().out


def test_resend_cli_only_retries_existing_notification(monkeypatch, tmp_path, capsys):
    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    settings = SimpleNamespace(project_root=root, logs_dir=tmp_path, log_level="INFO")
    resent = []

    @contextmanager
    def open_fake(**_kwargs):
        yield SimpleNamespace(store=object())

    class FakeNotifications:
        def __init__(self, *_args, **_kwargs):
            pass

        def resend(self, run_id):
            resent.append(run_id)

    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(run_paper, "open_locked_system", open_fake)
    monkeypatch.setattr(run_paper, "NotificationService", FakeNotifications)
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--resend", "committed-run"])

    run_paper.main()

    assert resent == ["committed-run"]
    assert "strategy was not executed" in capsys.readouterr().out


def test_audit_cli_records_reports_and_notifies_without_market_execution(
    monkeypatch, tmp_path, capsys
):
    from src.paper_broker import PaperRunResult

    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    values = {**values, "reports_dir": str(tmp_path / "reports")}
    settings = SimpleNamespace(project_root=root, logs_dir=tmp_path, log_level="INFO")
    result = PaperRunResult("missed-run", "MISSED_SCHEDULE", "missed")
    delivered = []

    @contextmanager
    def open_fake(**_kwargs):
        yield SimpleNamespace(store=object())

    class FakeNotifications:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_committed_run(self, run_id, report_path):
            delivered.append((run_id, report_path))

    report = tmp_path / "reports" / "missed.md"
    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(run_paper, "open_locked_system", open_fake)
    monkeypatch.setattr(run_paper, "audit_missed_schedule", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(run_paper, "write_operational_failure_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(run_paper, "NotificationService", FakeNotifications)
    monkeypatch.setattr(run_paper, "HermesTelegramSender", lambda: object())
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--audit-missed", "--telegram-target", "local-test"])

    run_paper.main()

    assert delivered == [("missed-run", report)]
    assert "MISSED_SCHEDULE recorded and delivered" in capsys.readouterr().out


def test_dry_run_success_orchestrates_public_snapshot_and_report_without_notification(
    monkeypatch, tmp_path, capsys
):
    from src.paper_broker import PaperRunResult

    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    values = {**values, "reports_dir": str(tmp_path / "reports")}
    settings = SimpleNamespace(
        project_root=root,
        logs_dir=tmp_path,
        log_level="INFO",
        max_retries=1,
        backoff_base_seconds=0.1,
        request_timeout_ms=1_000,
    )
    store = SimpleNamespace(
        forward_window_exists=lambda _key: False,
        schedule_exists=lambda _key: False,
        account=lambda: {"status": "ACTIVE"},
    )
    result = PaperRunResult("dry-run", "DRY_RUN", "proposal", outcome="CASH_ONLY")
    system = SimpleNamespace(
        store=store,
        _scheduled_key=lambda _now: None,
        _validate_snapshot=lambda *_args: None,
        run=lambda *_args, **_kwargs: result,
    )
    snapshot = SimpleNamespace(fetched_at=run_paper.pd.Timestamp("2026-01-06T00:01:00Z"))
    report = tmp_path / "reports" / "dry.md"
    monkeypatch.setattr(run_paper, "_current_schedule_window_closed", lambda *_args: False)

    @contextmanager
    def open_fake(**_kwargs):
        yield system

    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(run_paper, "open_locked_system", open_fake)
    monkeypatch.setattr(run_paper, "_experiment_start", lambda _root: snapshot.fetched_at)
    monkeypatch.setattr(run_paper, "recover_committed_forward_evidence", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(run_paper, "record_missed_windows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run_paper, "fetch_configured_public_market_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(run_paper, "finalize_forward_run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(run_paper, "write_weekly_paper_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        run_paper,
        "resolve_telegram_target",
        lambda _target: (_ for _ in ()).throw(AssertionError("dry-run must not notify")),
    )
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--dry-run"])

    run_paper.main()

    output = capsys.readouterr().out
    assert "Status: DRY_RUN" in output
    assert "Outcome: CASH_ONLY" in output
    assert str(report) in output


def test_dry_run_market_failure_commits_auditable_failure_without_notification(
    monkeypatch, tmp_path
):
    from src.paper_broker import PaperRunResult

    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    values = {**values, "reports_dir": str(tmp_path / "reports")}
    settings = SimpleNamespace(
        project_root=root,
        logs_dir=tmp_path,
        log_level="INFO",
        max_retries=1,
        backoff_base_seconds=0.1,
        request_timeout_ms=1_000,
    )
    store = SimpleNamespace(
        forward_window_exists=lambda _key: False,
        schedule_exists=lambda _key: False,
        account=lambda: {"status": "ACTIVE"},
    )
    system = SimpleNamespace(store=store, _scheduled_key=lambda _now: None)
    committed = []
    monkeypatch.setattr(run_paper, "_current_schedule_window_closed", lambda *_args: False)

    @contextmanager
    def open_fake(**_kwargs):
        yield system

    def commit(*_args, **kwargs):
        committed.append(kwargs)
        return PaperRunResult("failed-run", "DATA_QUALITY_FAILURE", kwargs["message"])

    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(run_paper, "open_locked_system", open_fake)
    monkeypatch.setattr(run_paper, "_experiment_start", lambda _root: run_paper.pd.Timestamp.now(tz="UTC"))
    monkeypatch.setattr(run_paper, "recover_committed_forward_evidence", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(run_paper, "record_missed_windows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        run_paper,
        "fetch_configured_public_market_snapshot",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("public timeout")),
    )
    monkeypatch.setattr(run_paper, "commit_operational_failure", commit)
    monkeypatch.setattr(
        run_paper,
        "write_operational_failure_report",
        lambda *_args, **_kwargs: tmp_path / "failed.md",
    )
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--dry-run"])

    with pytest.raises(SystemExit, match="2"):
        run_paper.main()

    assert committed[0]["outcome"] == "DATA_QUALITY_FAILURE"
    assert "public timeout" in committed[0]["message"]


def test_scheduled_paper_run_passes_verified_release_provenance_to_execution(
    monkeypatch, tmp_path
):
    from src.paper_broker import PaperRunResult
    from src.release_provenance import ReleaseProvenance

    root = Path(__file__).resolve().parents[1]
    config, values = run_paper.load_paper_configuration(root)
    values = {**values, "reports_dir": str(tmp_path / "reports")}
    now = datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc)
    settings = SimpleNamespace(
        project_root=root,
        logs_dir=tmp_path,
        log_level="INFO",
        max_retries=1,
        backoff_base_seconds=0.1,
        request_timeout_ms=1_000,
    )
    store = SimpleNamespace(
        forward_window_exists=lambda _key: False,
        schedule_exists=lambda _key: False,
        account=lambda: {"status": "ACTIVE"},
    )
    captured_run_kwargs = {}
    result = PaperRunResult("official", "EXECUTED", "committed", outcome="NO_REBALANCE")
    system = SimpleNamespace(
        store=store,
        _scheduled_key=lambda _now: "2026-01-05T00:05Z",
        _validate_snapshot=lambda *_args: None,
        run=lambda *_args, **kwargs: captured_run_kwargs.update(kwargs) or result,
    )
    snapshot = SimpleNamespace(fetched_at=pd.Timestamp(now))
    provenance = ReleaseProvenance(
        git_commit="a" * 40,
        git_dirty=False,
        hardening_manifest_sha256="b" * 64,
        execution_protocol_version="paper-exec-v3-ask-bid-minspread-utc0010",
        captured_at_utc=now,
    )

    @contextmanager
    def open_fake(**_kwargs):
        yield system

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(run_paper, "datetime", ControlledDateTime)
    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, values))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(run_paper, "open_locked_system", open_fake)
    monkeypatch.setattr(run_paper, "_experiment_start", lambda _root: pd.Timestamp(now))
    monkeypatch.setattr(run_paper, "recover_committed_forward_evidence", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(run_paper, "record_missed_windows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run_paper, "fetch_configured_public_market_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(run_paper, "build_forward_diagnostics", lambda *_args: {})
    monkeypatch.setattr(run_paper, "capture_release_provenance", lambda _root: provenance)
    monkeypatch.setattr(run_paper, "finalize_forward_run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(run_paper, "write_weekly_paper_report", lambda *_args, **_kwargs: tmp_path / "report.md")
    monkeypatch.setattr(run_paper, "resolve_telegram_target", lambda _target: "telegram:test")
    monkeypatch.setattr(
        run_paper,
        "NotificationService",
        lambda *_args, **_kwargs: SimpleNamespace(send_committed_run=lambda *_args: None),
    )
    monkeypatch.setattr(sys, "argv", ["run_paper.py", "--paper"])

    run_paper.main()

    assert captured_run_kwargs["release_provenance"] == provenance
    assert captured_run_kwargs["require_release_provenance"] is True


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
