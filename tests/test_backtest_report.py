import pandas as pd

from src.backtest import BacktestConfig, EventDrivenBacktester
from src.backtest_report import write_backtest_report


def test_backtest_report_writes_comparison_and_separate_ledgers(tmp_path):
    dates = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    prices = pd.DataFrame({"BTC/USDT": [100.0, 101.0, 102.0]}, index=dates)
    result = EventDrivenBacktester(
        prices, BacktestConfig(initial_cash=1_000.0)
    ).run(None, initial_target_weights={"CASH": 1.0})

    paths, metrics = write_backtest_report(
        {"Primary Strategy": result, "Cash": result}, tmp_path, "test-run"
    )

    assert paths["comparison_markdown"].exists()
    assert paths["comparison_json"].exists()
    assert paths["equity_curves"].exists()
    assert paths["strategy_orders"].exists()
    assert paths["strategy_fills"].exists()
    assert paths["strategy_positions"].exists()
    assert paths["strategy_cash"].exists()
    assert metrics["Cash"]["ending_equity"] == 1_000.0
