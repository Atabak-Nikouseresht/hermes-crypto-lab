import pandas as pd

from src.backtest import BacktestConfig
from src.benchmarks import benchmark_definitions, run_benchmarks


def test_benchmark_definitions_and_buy_and_hold_execution():
    assets = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT"]
    definitions = benchmark_definitions(assets)

    assert definitions["BTC Buy and Hold"] == {"BTC/USDT": 1.0, "CASH": 0.0}
    assert definitions["50% BTC / 50% ETH"] == {
        "BTC/USDT": 0.5,
        "ETH/USDT": 0.5,
        "CASH": 0.0,
    }
    assert definitions["Cash"] == {"CASH": 1.0}
    assert sum(definitions["Equal Weight"][asset] for asset in assets) == 1.0

    dates = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
    prices = pd.DataFrame({asset: 100.0 for asset in assets}, index=dates)
    results = run_benchmarks(prices, BacktestConfig(initial_cash=10_000.0))

    assert set(results) == set(definitions)
    assert len(results["BTC Buy and Hold"].fills) == 1
    assert results["BTC Buy and Hold"].fills.iloc[0]["signal_timestamp"] == dates[0]
    assert results["BTC Buy and Hold"].fills.iloc[0]["fill_timestamp"] == dates[1]
    assert len(results["Equal Weight"].fills) == 5
    assert results["Cash"].fills.empty
