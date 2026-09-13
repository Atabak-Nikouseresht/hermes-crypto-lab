from datetime import datetime, timezone
import math

import pandas as pd
import pytest

from src.forward_monthly import generate_monthly_forward_report
from src.paper_broker import PaperConfig, PaperTradingSystem


ASSETS = ("BTC/USDT",)


def _report_with_schedule(tmp_path, windows):
    system = PaperTradingSystem(tmp_path / "paper.duckdb", PaperConfig(assets=ASSETS))
    baseline_at = datetime(2026, 7, 27, 9, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('forward-1', '2026-07-01T00:00:00Z', 'locked', 'hash', 'govhash', '{}', 'ACTIVE')"
        )
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
            "VALUES ('baseline', ?, ?, 'EXECUTED', 'PAPER')",
            [baseline_at, baseline_at],
        )
        connection.execute(
            "INSERT INTO equity_snapshots VALUES ('snapshot-baseline', 'baseline', "
            "'locked_strategy', 100.0, 0.0, 100.0, ?)",
            [baseline_at],
        )
        connection.execute(
            "INSERT INTO forward_baselines VALUES ('forward-1', 'baseline', ?, 100.0)",
            [baseline_at],
        )
        for index, (scheduled_at, outcome, equity) in enumerate(windows):
            schedule_key = scheduled_at.strftime("%Y-%m-%dT%H:%MZ")
            run_id = f"run-{index}" if equity is not None else None
            if run_id is not None:
                connection.execute(
                    "INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode) "
                    "VALUES (?, ?, ?, 'EXECUTED', 'PAPER')",
                    [run_id, scheduled_at, scheduled_at],
                )
                connection.execute(
                    "INSERT INTO equity_snapshots VALUES (?, ?, 'locked_strategy', ?, 0.0, ?, ?)",
                    [f"snapshot-{run_id}", run_id, equity, equity, scheduled_at],
                )
            connection.execute(
                "INSERT INTO forward_schedule_windows VALUES (?, ?, ?, ?, ?)",
                [schedule_key, scheduled_at, run_id, outcome, scheduled_at],
            )
            connection.execute(
                "INSERT INTO forward_experiment_windows VALUES ('forward-1', ?)",
                [schedule_key],
            )
    return generate_monthly_forward_report(
        system.store,
        experiment_id="forward-1",
        report_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        output_dir=tmp_path / "reports",
        assets=ASSETS,
        slippage_rate=0.0005,
    )


def _weekly(day, outcome, equity):
    return datetime(2026, 8, day, 9, 10, tzinfo=timezone.utc), outcome, equity


def test_monthly_sampling_accepts_contiguous_governed_weekly_windows(tmp_path):
    result = _report_with_schedule(
        tmp_path,
        [
            _weekly(3, "CASH_ONLY", 110.0),
            _weekly(10, "CASH_ONLY", 121.0),
            _weekly(17, "CASH_ONLY", 117.37),
        ],
    )

    assert result["performance_sampling_status"] == "valid_contiguous_weekly"
    assert result["performance_sample_count"] == 4
    assert result["periodic_return_count"] == 3


def test_monthly_sampling_marks_one_missed_week_insufficient(tmp_path):
    result = _report_with_schedule(
        tmp_path,
        [
            _weekly(3, "CASH_ONLY", 110.0),
            _weekly(10, "MISSED_SCHEDULE", None),
            _weekly(17, "CASH_ONLY", 121.0),
        ],
    )

    assert result["missed_windows"] == 1
    assert result["performance_sampling_status"] == "missing_or_irregular_windows"
    assert result["volatility"] == "insufficient sample"
    assert result["sharpe"] == "insufficient sample"


def test_monthly_sampling_marks_multiple_missed_weeks_insufficient(tmp_path):
    result = _report_with_schedule(
        tmp_path,
        [
            _weekly(3, "CASH_ONLY", 110.0),
            _weekly(10, "MISSED_SCHEDULE", None),
            _weekly(17, "MISSED_SCHEDULE", None),
            _weekly(24, "CASH_ONLY", 121.0),
        ],
    )

    assert result["missed_windows"] == 2
    assert result["performance_sampling_status"] == "missing_or_irregular_windows"
    assert result["periodic_return_count"] == 0


def test_monthly_sampling_requires_at_least_two_valid_weekly_returns(tmp_path):
    result = _report_with_schedule(tmp_path, [_weekly(3, "CASH_ONLY", 110.0)])

    assert result["performance_sampling_status"] == "insufficient_weekly_returns"
    assert result["periodic_return_count"] == 1
    assert result["volatility"] == "insufficient sample"
    assert result["sharpe"] == "insufficient sample"


def test_monthly_sampling_without_missed_windows_retains_weekly_volatility(tmp_path):
    equities = [110.0, 121.0, 117.37]
    result = _report_with_schedule(
        tmp_path,
        [
            _weekly(3, "CASH_ONLY", equities[0]),
            _weekly(10, "CASH_ONLY", equities[1]),
            _weekly(17, "CASH_ONLY", equities[2]),
        ],
    )
    expected = pd.Series([0.10, 0.10, -0.03]).std(ddof=1) * math.sqrt(52)

    assert result["volatility"] == pytest.approx(expected)
    assert result["net_return"] == pytest.approx(equities[-1] / 100.0 - 1.0)


def test_monthly_sampling_never_inserts_a_zero_return_for_missed_schedule(tmp_path):
    result = _report_with_schedule(
        tmp_path,
        [
            _weekly(3, "CASH_ONLY", 110.0),
            _weekly(10, "MISSED_SCHEDULE", None),
            _weekly(17, "CASH_ONLY", 121.0),
        ],
    )

    assert result["periodic_return_count"] == 0
    assert result["net_return"] == pytest.approx(0.21)


def test_monthly_sampling_annualization_is_deterministic_for_valid_weekly_returns(tmp_path):
    windows = [
        _weekly(3, "CASH_ONLY", 110.0),
        _weekly(10, "CASH_ONLY", 121.0),
        _weekly(17, "CASH_ONLY", 117.37),
    ]

    first = _report_with_schedule(tmp_path / "first", windows)
    second = _report_with_schedule(tmp_path / "second", windows)

    assert first["volatility"] == second["volatility"]
    assert first["periodic_return_count"] == second["periodic_return_count"] == 3
