import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import run_paper
from run_paper import _current_schedule_window_closed
from scripts import paper_forward_weekly
from scripts.interpreter import resolve_project_python
from scripts.paper_forward_weekly import should_launch
from src.scheduler_contract import SchedulerContractError, verify_scheduler_job


def test_scheduler_readback_contract_requires_exact_utc_schedule_path_hash_and_gate(tmp_path):
    scripts_root = tmp_path / "scripts"
    scripts_root.mkdir()
    approved = scripts_root / "paper_forward_weekly.py"
    approved.write_text("approved wrapper", encoding="utf-8")
    digest = hashlib.sha256(approved.read_bytes()).hexdigest()
    workdir = tmp_path / "project"
    workdir.mkdir()
    job = {
        "id": "abc123",
        "name": "crypto-paper-forward-weekly",
        "schedule": {"kind": "cron", "expr": "10 0 * * 1"},
        "script": "paper_forward_weekly.py",
        "no_agent": True,
        "workdir": str(workdir),
        "enabled": True,
    }

    verified = verify_scheduler_job(
        job,
        expected_name="crypto-paper-forward-weekly",
        expected_expression="10 0 * * 1",
        expected_script=approved,
        expected_workdir=workdir,
        scripts_root=scripts_root,
        expected_script_sha256=digest,
    )

    assert verified["job_id"] == "abc123"
    assert verified["verified"] is True
    assert should_launch(datetime(2026, 8, 24, 0, 5, tzinfo=timezone.utc))
    assert should_launch(datetime(2026, 8, 24, 0, 10, tzinfo=timezone.utc))
    assert should_launch(datetime(2026, 8, 24, 0, 20, tzinfo=timezone.utc))
    assert not should_launch(datetime(2026, 8, 24, 0, 21, tzinfo=timezone.utc))
    assert not should_launch(datetime(2026, 8, 24, 0, 4, tzinfo=timezone.utc))

    wrong_root = tmp_path / "other"
    wrong_root.mkdir()
    (wrong_root / "paper_forward_weekly.py").write_text("approved wrapper", encoding="utf-8")
    wrong = {**job, "script": str(wrong_root / "paper_forward_weekly.py")}
    with pytest.raises(SchedulerContractError):
        verify_scheduler_job(
            wrong,
            expected_name="crypto-paper-forward-weekly",
            expected_expression="10 0 * * 1",
            expected_script=approved,
            expected_workdir=workdir,
            scripts_root=scripts_root,
            expected_script_sha256=digest,
        )


def test_interpreter_resolution_prefers_windows_then_unix_and_fails_explicitly(tmp_path):
    project = tmp_path / "project"
    unix = project / ".venv" / "bin" / "python"
    windows = project / ".venv" / "Scripts" / "python.exe"
    unix.parent.mkdir(parents=True)
    unix.write_text("unix", encoding="ascii")
    assert resolve_project_python(project) == unix
    windows.parent.mkdir(parents=True)
    windows.write_text("windows", encoding="ascii")
    assert resolve_project_python(project) == windows
    windows.unlink()
    unix.unlink()
    with pytest.raises(FileNotFoundError, match="project virtual-environment interpreter"):
        resolve_project_python(project)


def test_missed_current_window_returns_before_market_fetch():
    source = (Path(__file__).resolve().parents[1] / "run_paper.py").read_text(encoding="utf-8")
    guard = source.index("if current_window_missed:")
    fetch = source.index("snapshot = fetch_configured_public_market_snapshot(")

    assert guard < fetch
    assert "return" in source[guard:fetch]


def test_previously_recorded_missed_window_still_fails_closed():
    now = datetime(2026, 8, 24, 0, 21, tzinfo=timezone.utc)
    from src.paper_broker import PaperConfig

    config = PaperConfig(assets=("BTC/USDT",))

    assert _current_schedule_window_closed(now, config) is True


def test_weekly_dispatch_succeeds_inside_full_governed_window(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setattr(paper_forward_weekly.subprocess, "run", run)

    assert paper_forward_weekly.main(
        datetime(2026, 1, 5, 0, 5, tzinfo=timezone.utc),
        python_resolver=lambda _project: Path("scheduler-python"),
    ) == 0
    assert len(calls) == 1


def test_weekly_dispatch_retries_after_transient_failure_in_same_window(monkeypatch):
    results = iter(
        [
            subprocess.CompletedProcess([], 75, "", "temporary failure"),
            subprocess.CompletedProcess([], 0, "ok", ""),
        ]
    )
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return next(results)

    times = iter(
        [
            datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 1, 5, 0, 11, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(paper_forward_weekly.subprocess, "run", run)

    result = paper_forward_weekly.main(
        clock=lambda: next(times),
        sleeper=lambda _seconds: None,
        python_resolver=lambda _project: Path("scheduler-python"),
    )

    assert result == 0
    assert len(calls) == 2


def test_weekly_dispatch_does_not_retry_terminal_provenance_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        paper_forward_weekly.subprocess,
        "run",
        lambda *args, **_kwargs: calls.append(args)
        or subprocess.CompletedProcess(args[0], 2, "", "RELEASE_PROVENANCE_FAILURE"),
    )

    assert paper_forward_weekly.main(
        datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc),
        sleeper=lambda _seconds: pytest.fail("terminal failure must not retry"),
        python_resolver=lambda _project: Path("scheduler-python"),
    ) == 2
    assert len(calls) == 1


def test_weekly_dispatch_retries_explicit_retryable_exit_code(monkeypatch):
    results = iter([subprocess.CompletedProcess([], 4, "", "retryable"), subprocess.CompletedProcess([], 0, "ok", "")])
    monkeypatch.setattr(paper_forward_weekly.subprocess, "run", lambda *_args, **_kwargs: next(results))
    times = iter([datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc), datetime(2026, 1, 5, 0, 11, tzinfo=timezone.utc)])
    assert paper_forward_weekly.main(clock=lambda: next(times), sleeper=lambda _seconds: None, python_resolver=lambda _project: Path("scheduler-python")) == 0


def test_weekly_wrapper_retries_the_actual_data_halt_exit_from_run_paper(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from src.paper_broker import PaperConfig, PaperRunResult

    data_halt = PaperRunResult(
        "halted-run", "DATA_HALT", "validated snapshot rejected", outcome="DATA_QUALITY_FAILURE"
    )
    config = PaperConfig(assets=("BTC/USDT",))
    settings = SimpleNamespace(
        project_root=tmp_path,
        logs_dir=tmp_path / "logs",
        log_level="INFO",
        max_retries=1,
        backoff_base_seconds=0.1,
        request_timeout_ms=1_000,
    )
    system = SimpleNamespace(
        store=SimpleNamespace(
            forward_window_exists=lambda _key: False,
            schedule_exists=lambda _key: False,
            account=lambda: {"status": "ACTIVE"},
        ),
        _scheduled_key=lambda _now: None,
        _validate_snapshot=lambda *_args: None,
        run=lambda *_args, **_kwargs: data_halt,
    )

    @contextmanager
    def open_fake(**_kwargs):
        yield system

    monkeypatch.setattr(run_paper, "load_settings", lambda: settings)
    monkeypatch.setattr(run_paper, "load_paper_configuration", lambda _root: (config, {"database_path": "paper.duckdb", "reports_dir": "reports", "default_dry_run": True}))
    monkeypatch.setattr(run_paper, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(run_paper, "_verify_research_lock", lambda *_args: "verified")
    monkeypatch.setattr(run_paper, "open_locked_system", open_fake)
    monkeypatch.setattr(run_paper, "_experiment_start", lambda _root: run_paper.pd.Timestamp("2026-01-05T00:10:00Z"))
    monkeypatch.setattr(run_paper, "recover_committed_forward_evidence", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(run_paper, "record_missed_windows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run_paper, "fetch_configured_public_market_snapshot", lambda *_args: SimpleNamespace(fetched_at=run_paper.pd.Timestamp("2026-01-05T00:10:00Z")))
    monkeypatch.setattr(run_paper, "finalize_forward_run", lambda *_args, **_kwargs: data_halt)
    monkeypatch.setattr(run_paper, "write_weekly_paper_report", lambda *_args, **_kwargs: tmp_path / "report.md")
    monkeypatch.setattr("sys.argv", ["run_paper.py", "--dry-run"])

    calls, actual_return_codes = [], []

    def run(*args, **_kwargs):
        calls.append(args)
        if len(calls) == 1:
            with pytest.raises(SystemExit) as exc:
                run_paper.main()
            actual_return_codes.append(exc.value.code)
            return subprocess.CompletedProcess(args[0], exc.value.code, "", "DATA_HALT")
        return subprocess.CompletedProcess(args[0], 0, "Status: EXECUTED", "")

    times = iter(
        [
            datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 1, 5, 0, 11, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(paper_forward_weekly.subprocess, "run", run)

    assert paper_forward_weekly.main(
        clock=lambda: next(times),
        sleeper=lambda _seconds: None,
        python_resolver=lambda _project: Path("scheduler-python"),
    ) == 0
    assert actual_return_codes == [paper_forward_weekly.RETRYABLE_EXIT_CODE]
    assert actual_return_codes[0] in paper_forward_weekly.RETRYABLE_EXIT_CODES
    assert len(calls) == 2


def test_weekly_dispatch_delegates_duplicate_prevention_to_committed_run(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        output = "Status: EXECUTED" if len(calls) == 1 else "Status: DUPLICATE_SCHEDULE"
        return subprocess.CompletedProcess(args[0], 0, output, "")

    monkeypatch.setattr(paper_forward_weekly.subprocess, "run", run)

    assert paper_forward_weekly.main(
        datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc),
        python_resolver=lambda _project: Path("scheduler-python"),
    ) == 0
    assert paper_forward_weekly.main(
        datetime(2026, 1, 5, 0, 12, tzinfo=timezone.utc),
        python_resolver=lambda _project: Path("scheduler-python"),
    ) == 0
    assert len(calls) == 2


def test_weekly_dispatch_does_not_execute_outside_window(monkeypatch):
    monkeypatch.setattr(
        paper_forward_weekly.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )

    assert paper_forward_weekly.main(
        datetime(2026, 1, 5, 0, 4, tzinfo=timezone.utc)
    ) == 0
    assert paper_forward_weekly.main(
        datetime(2026, 1, 5, 0, 21, tzinfo=timezone.utc)
    ) == 0


def test_gateway_watchdog_preserves_compact_sanitized_failure_details():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "hermes_gateway_watchdog.ps1"
    ).read_text(encoding="utf-8")

    assert "*> $null" not in source
    assert "failure_detail=" in source
    assert "Select-Object -Last 5" in source
    assert "[REDACTED]" in source
    assert "Test-Path $Executable" in source
    assert "return 127" in source


def test_canonical_scheduler_contract_excludes_runtime_deployment_observations():
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "forward_experiment"
            / "scheduler_manifest.json"
        ).read_text(encoding="utf-8")
    )
    volatile = {
        "installed",
        "job_id",
        "last_run_at",
        "last_run_result",
        "last_status",
        "next_run_at",
        "status",
    }

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert keys(manifest).isdisjoint(volatile)
    assert manifest["publication_note"].startswith("Portable static source contract")
    assert "Exact frozen deployment snapshot" not in manifest["publication_note"]
