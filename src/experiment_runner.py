"""Leakage-aware candidate and benchmark evaluation primitives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from src.backtest import BacktestConfig, BacktestResult, EventDrivenBacktester
from src.benchmarks import run_benchmarks
from src.experiment_manager import Candidate, Period, penalized_score
from src.metrics import calculate_performance_metrics
from src.strategy import StrategyConfig, generate_signal


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: Candidate
    period: Period
    metrics: dict[str, float | int]
    penalized_score: float
    benchmark_metrics: dict[str, dict[str, float | int]]
    comparisons: dict[str, dict[str, float]]
    result: BacktestResult


def _asset_path(processed_dir: Path, asset: str, timeframe: str) -> Path:
    safe = asset.replace("/", "_").replace(":", "_")
    return processed_dir / f"{safe}_{timeframe}.parquet"


def load_common_timestamp_index(
    processed_dir: Path, assets: list[str], timeframe: str
) -> pd.DatetimeIndex:
    common: pd.DatetimeIndex | None = None
    for asset in assets:
        frame = pd.read_parquet(
            _asset_path(processed_dir, asset, timeframe), columns=["timestamp"]
        )
        index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True).unique())
        common = index if common is None else common.intersection(index)
    if common is None or common.empty:
        raise ValueError("No common timestamp history across configured assets")
    return common.sort_values()


def load_close_prices_through(
    processed_dir: Path,
    assets: list[str],
    timeframe: str,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Read only rows at or before the stage boundary via Parquet filters."""
    end_utc = pd.Timestamp(end).tz_convert("UTC")
    series = []
    for asset in assets:
        frame = pd.read_parquet(
            _asset_path(processed_dir, asset, timeframe),
            columns=["timestamp", "close"],
            filters=[("timestamp", "<=", end_utc.to_pydatetime())],
        )
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        series.append(
            pd.Series(frame["close"].to_numpy(dtype=float), index=timestamps, name=asset)
        )
    prices = pd.concat(series, axis=1, join="inner").sort_index()
    if prices.empty or prices.index.max() > end_utc:
        raise ValueError("Stage-bounded Parquet read failed")
    return prices


def _simulation_prices(available_prices: pd.DataFrame, period: Period) -> pd.DataFrame:
    if available_prices.index.max() < period.end:
        raise PermissionError("Requested period exceeds the stage's available data")
    simulation = available_prices.loc[period.start : period.end]
    if simulation.empty or simulation.index[0] != period.start:
        raise ValueError("Period start must be an available signal bar")
    return simulation


def benchmark_metrics_for_period(
    available_prices: pd.DataFrame,
    period: Period,
    backtest_config: BacktestConfig,
) -> dict[str, dict[str, float | int]]:
    simulation = _simulation_prices(available_prices, period)
    results = run_benchmarks(simulation, backtest_config)
    return {
        name: calculate_performance_metrics(result.equity_curve, result.fills)
        for name, result in results.items()
        if name in {"BTC Buy and Hold", "Equal Weight"}
    }


def evaluate_candidate_period(
    available_prices: pd.DataFrame,
    period: Period,
    candidate: Candidate,
    base_strategy_config: StrategyConfig,
    base_backtest_config: BacktestConfig,
    benchmark_metrics: dict[str, dict[str, float | int]],
) -> CandidateEvaluation:
    strategy_config = replace(
        base_strategy_config,
        momentum_long_days=candidate.momentum_window,
        momentum_skip_days=candidate.skip_window,
        btc_moving_average_days=candidate.trend_filter,
        max_assets=candidate.selected_assets,
        volatility_days=candidate.volatility_window,
    )
    backtest_config = replace(
        base_backtest_config, rebalance_interval_days=candidate.rebalance_days
    )
    simulation = _simulation_prices(available_prices, period)
    engine = EventDrivenBacktester(simulation, backtest_config)
    result = engine.run(
        lambda _engine_prices, as_of: generate_signal(
            available_prices, as_of=as_of, config=strategy_config
        )
    )
    metrics = calculate_performance_metrics(result.equity_curve, result.fills)
    duration_days = max((period.end - period.start).days, 1)
    score = penalized_score(metrics, duration_days=duration_days)
    comparisons = {
        name: {
            "cagr_difference": float(metrics["cagr"] - values["cagr"]),
            "sharpe_difference": float(metrics["sharpe"] - values["sharpe"]),
            "max_drawdown_difference": float(
                metrics["max_drawdown"] - values["max_drawdown"]
            ),
            "ending_equity_difference": float(
                metrics["ending_equity"] - values["ending_equity"]
            ),
        }
        for name, values in benchmark_metrics.items()
    }
    return CandidateEvaluation(
        candidate=candidate,
        period=period,
        metrics=metrics,
        penalized_score=score,
        benchmark_metrics=benchmark_metrics,
        comparisons=comparisons,
        result=result,
    )
