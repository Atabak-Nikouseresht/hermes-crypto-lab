from pathlib import Path

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
