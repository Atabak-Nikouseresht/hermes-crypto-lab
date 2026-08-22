from datetime import datetime, timezone
import json

from src.forward_monthly import generate_monthly_forward_report
from src.paper_broker import PaperConfig, PaperTradingSystem


ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")


def test_monthly_report_uses_forward_only_data_and_identical_benchmark_timestamps(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=ASSETS))
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

    result = generate_monthly_forward_report(
        system.store,
        experiment_id="forward-1",
        report_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        output_dir=tmp_path / "reports",
        assets=ASSETS,
        slippage_rate=0.0005,
    )

    assert result["data_scope"] == "forward_only"
    assert result["backtest_rows_used"] == 0
    assert result["scheduled_windows"] == 2
    assert result["completed_windows"] == 2
    assert result["weeks_in_cash"] == 1
    assert result["net_return"] == 0.02
    assert result["sharpe"] == "insufficient sample"
    assert result["strategy_timestamps"] == result["btc_benchmark_timestamps"]
    assert result["strategy_timestamps"] == result["equal_weight_benchmark_timestamps"]
    assert result["report_path"].is_file()
