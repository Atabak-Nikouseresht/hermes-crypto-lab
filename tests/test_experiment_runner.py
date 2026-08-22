import numpy as np
import pandas as pd

from src.backtest import BacktestConfig
from src.experiment_manager import Candidate, Period
from src.experiment_runner import benchmark_metrics_for_period, evaluate_candidate_period
from src.strategy import StrategyConfig


def test_candidate_evaluation_includes_costs_and_benchmark_comparisons():
    dates = pd.date_range("2023-01-01", periods=320, freq="D", tz="UTC")
    day = np.arange(len(dates), dtype=float)
    prices = pd.DataFrame(
        {
            "BTC/USDT": 100 * np.exp(0.0010 * day),
            "ETH/USDT": 100 * np.exp(0.0012 * day),
            "BNB/USDT": 100 * np.exp(0.0014 * day),
            "XRP/USDT": 100 * np.exp(0.0008 * day),
            "TRX/USDT": 100 * np.exp(0.0006 * day),
        },
        index=dates,
    )
    period = Period(pd.Timestamp("2023-07-30", tz="UTC"), dates[-1])
    backtest = BacktestConfig(initial_cash=10_000, fee_rate=0.001, slippage_rate=0.001)
    benchmarks = benchmark_metrics_for_period(prices, period, backtest)

    evaluation = evaluate_candidate_period(
        prices,
        period,
        Candidate(90, 7, 200, 2, 7, 30),
        StrategyConfig(),
        backtest,
        benchmarks,
    )

    assert evaluation.metrics["total_fees"] > 0
    assert set(evaluation.benchmark_metrics) == {"BTC Buy and Hold", "Equal Weight"}
    assert "cagr_difference" in evaluation.comparisons["BTC Buy and Hold"]
    assert evaluation.result.cash.min() >= -1e-9
