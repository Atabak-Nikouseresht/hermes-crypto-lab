from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import run_paper
from src.forward_operations import AlreadyRunningError, InterProcessLock
from src.paper_broker import PaperConfig


ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")


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
