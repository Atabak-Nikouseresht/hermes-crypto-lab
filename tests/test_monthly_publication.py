from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

import run_monthly_report
from src.forward_monthly import (
    generate_monthly_forward_report,
    is_monthly_report_committed,
)
from src.paper_broker import PaperConfig, PaperTradingSystem


ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")
REPORT_DATE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _store_with_forward_data(tmp_path):
    system = PaperTradingSystem(tmp_path / "paper.duckdb", PaperConfig(assets=ASSETS))
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('forward-1', '2026-08-01T00:00:00Z', 'locked', 'hash', 'govhash', '{}', 'ACTIVE')"
        )
        for run_id, timestamp, equity, positions_value in [
            ("r1", "2026-08-03T09:10:00Z", 2000.0, 0.0),
            ("r2", "2026-08-10T09:10:00Z", 2040.0, 1000.0),
        ]:
            connection.execute(
                "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
                "VALUES (?, ?, ?, 'EXECUTED', 'PAPER')",
                [run_id, timestamp, timestamp],
            )
            connection.execute(
                "INSERT INTO equity_snapshots VALUES (?, ?, 'locked_strategy', ?, ?, ?, ?)",
                [f"s-{run_id}", run_id, 2000.0 - positions_value, positions_value, equity, timestamp],
            )
        connection.execute(
            "INSERT INTO forward_schedule_windows VALUES "
            "('2026-08-03T09:05Z','2026-08-03T09:10:00Z','r1','CASH_ONLY','2026-08-03T09:10:00Z'),"
            "('2026-08-10T09:05Z','2026-08-10T09:10:00Z','r2','PAPER_TRADE_COMPLETED','2026-08-10T09:10:00Z')"
        )
        connection.execute(
            "INSERT INTO forward_experiment_windows VALUES "
            "('forward-1','2026-08-03T09:05Z'),"
            "('forward-1','2026-08-10T09:05Z')"
        )
        for run_id, timestamp, multiplier in [
            ("r1", "2026-08-03T09:10:00Z", 1.0),
            ("r2", "2026-08-10T09:10:00Z", 1.1),
        ]:
            for index, symbol in enumerate(ASSETS, start=1):
                connection.execute(
                    "INSERT INTO forward_market_observations VALUES (?, ?, ?, ?)",
                    [run_id, timestamp, symbol, 100.0 * index * multiplier],
                )
        connection.execute(
            "INSERT INTO forward_baselines VALUES "
            "('forward-1','r1','2026-08-03T09:10:00Z',2000.0)"
        )
    return system.store


def _generate(store, output_dir, publication_hook=None):
    return generate_monthly_forward_report(
        store,
        experiment_id="forward-1",
        report_date=REPORT_DATE,
        output_dir=output_dir,
        assets=ASSETS,
        slippage_rate=0.0005,
        publication_hook=publication_hook,
    )


def test_monthly_publication_commits_a_forward_only_report_pair(tmp_path):
    output_dir = tmp_path / "reports"
    result = _generate(_store_with_forward_data(tmp_path), output_dir)

    assert result["data_scope"] == "forward_only"
    assert result["backtest_rows_used"] == 0
    assert result["strategy_timestamps"] == result["btc_benchmark_timestamps"]
    assert result["strategy_timestamps"] == result["equal_weight_benchmark_timestamps"]
    assert result["json_path"].is_file()
    assert result["report_path"].is_file()
    assert result["completion_path"].is_file()
    assert is_monthly_report_committed(output_dir, "2026-08")


def test_valid_committed_monthly_report_is_reused_without_reading_store(tmp_path, monkeypatch):
    store = _store_with_forward_data(tmp_path)
    output_dir = tmp_path / "reports"
    first = _generate(store, output_dir)
    original_json = first["json_path"].read_bytes()
    original_markdown = first["report_path"].read_bytes()

    def fail_if_read(*_args, **_kwargs):
        raise AssertionError("committed report must be reused before data access")

    monkeypatch.setattr(store, "connect", fail_if_read)
    reused = _generate(store, output_dir)

    assert reused["publication_status"] == "reused"
    assert reused["json_path"].read_bytes() == original_json
    assert reused["report_path"].read_bytes() == original_markdown


@pytest.mark.parametrize(
    ("failure_stage", "expected_canonical_files"),
    [
        ("after_json_temp", set()),
        ("after_json_publish", {"forward_monthly_2026-08.json"}),
        (
            "after_markdown_publish",
            {"forward_monthly_2026-08.json", "forward_monthly_2026-08.md"},
        ),
    ],
)
def test_interrupted_publication_is_recovered_deterministically(
    tmp_path, failure_stage, expected_canonical_files
):
    store = _store_with_forward_data(tmp_path)
    output_dir = tmp_path / "reports"

    def fail(stage):
        if stage == failure_stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=failure_stage):
        _generate(store, output_dir, publication_hook=fail)

    assert {path.name for path in output_dir.glob("forward_monthly_2026-08.*")} == expected_canonical_files
    assert not list(output_dir.glob(".*.tmp"))

    recovered = _generate(store, output_dir)
    assert recovered["publication_status"] in {"committed", "recovered"}
    assert is_monthly_report_committed(output_dir, "2026-08")


@pytest.mark.parametrize("artifact", ["json_path", "report_path"])
def test_corrupted_committed_artifacts_fail_closed(tmp_path, artifact):
    output_dir = tmp_path / "reports"
    result = _generate(_store_with_forward_data(tmp_path), output_dir)
    result[artifact].write_text("corrupted", encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        _generate(_store_with_forward_data(tmp_path / "other"), output_dir)


def test_invalid_completion_marker_fails_closed(tmp_path):
    output_dir = tmp_path / "reports"
    result = _generate(_store_with_forward_data(tmp_path), output_dir)
    marker = json.loads(result["completion_path"].read_text(encoding="utf-8"))
    marker["committed_at_utc"] = "not-a-utc-timestamp"
    result["completion_path"].write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(ValueError, match="completion marker"):
        _generate(_store_with_forward_data(tmp_path / "other"), output_dir)


def test_committed_report_cannot_be_silently_overwritten(tmp_path):
    output_dir = tmp_path / "reports"
    result = _generate(_store_with_forward_data(tmp_path), output_dir)
    expected = {
        path: path.read_bytes()
        for path in (result["json_path"], result["report_path"], result["completion_path"])
    }

    reused = _generate(_store_with_forward_data(tmp_path / "other"), output_dir)

    assert reused["publication_status"] == "reused"
    assert {path: path.read_bytes() for path in expected} == expected


def test_monthly_telegram_is_blocked_until_report_commit(monkeypatch, tmp_path):
    (tmp_path / "forward_experiment").mkdir()
    (tmp_path / "forward_experiment" / "governance.json").write_text(
        json.dumps({"experiment_id": "forward-1"}), encoding="utf-8"
    )
    sent: list[tuple[str, object]] = []

    @contextmanager
    def locked_system(**_kwargs):
        yield SimpleNamespace(store=object())

    monkeypatch.setattr(
        run_monthly_report, "load_settings", lambda: SimpleNamespace(project_root=tmp_path)
    )
    monkeypatch.setattr(
        run_monthly_report,
        "load_paper_configuration",
        lambda _root: (SimpleNamespace(assets=ASSETS, slippage_rate=0.0005), {}),
    )
    monkeypatch.setattr(run_monthly_report, "_project_paths", lambda *_args: (tmp_path / "paper.duckdb", tmp_path))
    monkeypatch.setattr(run_monthly_report, "_verify_research_lock", lambda *_args: None)
    monkeypatch.setattr(run_monthly_report, "open_locked_system", locked_system)
    monkeypatch.setattr(run_monthly_report, "record_missed_windows", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_monthly_report, "_experiment_start", lambda _root: REPORT_DATE)
    monkeypatch.setattr(
        run_monthly_report,
        "generate_monthly_forward_report",
        lambda *_args, **_kwargs: {"report_path": tmp_path / "uncommitted.md"},
    )
    monkeypatch.setattr(
        run_monthly_report,
        "HermesTelegramSender",
        lambda: lambda target, path: sent.append((target, path)),
    )
    monkeypatch.setenv("HCL_TELEGRAM_TARGET", "telegram:test")

    with pytest.raises(RuntimeError, match="not committed"):
        run_monthly_report.main()

    assert sent == []
