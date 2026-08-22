"""Deterministic static-allocation benchmark definitions and runners."""

from __future__ import annotations

import pandas as pd

from src.backtest import BacktestConfig, BacktestResult, EventDrivenBacktester


def benchmark_definitions(assets: list[str]) -> dict[str, dict[str, float]]:
    required = {"BTC/USDT", "ETH/USDT"}
    if not required.issubset(assets):
        raise ValueError("Benchmarks require BTC/USDT and ETH/USDT")
    equal_weight = 1.0 / len(assets)
    return {
        "BTC Buy and Hold": {"BTC/USDT": 1.0, "CASH": 0.0},
        "Equal Weight": {**{asset: equal_weight for asset in assets}, "CASH": 0.0},
        "50% BTC / 50% ETH": {
            "BTC/USDT": 0.5,
            "ETH/USDT": 0.5,
            "CASH": 0.0,
        },
        "Cash": {"CASH": 1.0},
    }


def run_benchmarks(
    close_prices: pd.DataFrame, config: BacktestConfig
) -> dict[str, BacktestResult]:
    definitions = benchmark_definitions(list(close_prices.columns))
    results = {}
    for name, weights in definitions.items():
        engine = EventDrivenBacktester(close_prices, config)
        results[name] = engine.run(None, initial_target_weights=weights)
    return results
