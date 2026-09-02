from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

from src.backtest import BacktestConfig, EventDrivenBacktester
from src.backtest_report import write_backtest_report
from src.benchmarks import run_benchmarks
from src.config import load_assets, load_settings
from src.logging_config import configure_logging
from src.research_data import load_canonical_close_prices
from src.strategy import StrategyConfig, generate_signal

LOGGER = logging.getLogger(__name__)


def load_close_prices(processed_dir: Path, assets: list[str], timeframe: str) -> pd.DataFrame:
    return load_canonical_close_prices(processed_dir, assets, timeframe)


def load_run_configuration(project_root: Path) -> tuple[StrategyConfig, BacktestConfig]:
    load_dotenv(project_root / ".env", override=False)
    payload = yaml.safe_load(
        (project_root / "config" / "strategy.yaml").read_text(encoding="utf-8")
    )
    if payload.get("optimization_enabled"):
        raise ValueError("Parameter optimization must remain disabled in this phase")
    strategy_values = payload["strategy"]
    strategy = StrategyConfig(
        momentum_short_days=int(strategy_values["momentum_short_days"]),
        momentum_long_days=int(strategy_values["momentum_long_days"]),
        momentum_skip_days=int(strategy_values["momentum_skip_days"]),
        btc_moving_average_days=int(strategy_values["btc_moving_average_days"]),
        volatility_days=int(strategy_values["volatility_days"]),
        annualization_days=int(strategy_values["annualization_days"]),
        max_assets=int(strategy_values["max_assets"]),
        asset_caps={key: float(value) for key, value in strategy_values["asset_caps"].items()},
        altcoins=set(strategy_values["altcoins"]),
        max_altcoin_weight=float(strategy_values["max_altcoin_weight"]),
    )
    backtest_values = payload["backtest"]
    backtest = BacktestConfig(
        initial_cash=float(backtest_values["initial_cash"]),
        fee_rate=float(backtest_values["fee_rate"]),
        slippage_rate=float(backtest_values["slippage_rate"]),
    )
    return strategy, backtest


def _week_key(timestamp: pd.Timestamp) -> tuple[int, int]:
    iso = timestamp.isocalendar()
    return int(iso.year), int(iso.week)


def find_common_analysis_start(
    prices: pd.DataFrame, strategy: StrategyConfig
) -> tuple[pd.Timestamp, pd.Timestamp]:
    required = max(
        strategy.momentum_long_days,
        strategy.btc_moving_average_days - 1,
        strategy.volatility_days,
    ) + 1
    for location in range(required - 1, len(prices.index) - 1):
        if _week_key(prices.index[location]) != _week_key(prices.index[location + 1]):
            return prices.index[location], prices.index[location + 1]
    raise ValueError("Insufficient history for a complete strategy signal and next-bar fill")


def run_research_backtest(project_root: Path | None = None) -> dict[str, object]:
    settings = load_settings(project_root)
    configure_logging(settings.logs_dir, settings.log_level)
    strategy_config, backtest_config = load_run_configuration(settings.project_root)
    assets = load_assets(settings.project_root / "config" / "assets.yaml")
    all_prices, dataset_provenance = load_canonical_close_prices(
        settings.processed_dir,
        assets,
        "1d",
        include_provenance=True,
    )
    initial_signal_date, first_execution_date = find_common_analysis_start(
        all_prices, strategy_config
    )
    analysis_prices = all_prices.loc[initial_signal_date:].copy()

    LOGGER.info(
        "Backtest analysis period %s to %s; initial signal=%s",
        initial_signal_date.isoformat(),
        analysis_prices.index[-1].isoformat(),
        initial_signal_date.isoformat(),
    )
    strategy_engine = EventDrivenBacktester(analysis_prices, backtest_config)
    strategy_result = strategy_engine.run(
        lambda _engine_prices, as_of: generate_signal(
            all_prices, as_of=as_of, config=strategy_config
        ),
    )
    benchmark_results = run_benchmarks(analysis_prices, backtest_config)
    all_results = {"Primary Strategy": strategy_result, **benchmark_results}

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths, metrics = write_backtest_report(all_results, settings.reports_dir, run_id)
    metadata_path = paths["output_dir"] / "run_metadata.json"
    strategy_payload = asdict(strategy_config)
    strategy_payload["altcoins"] = sorted(strategy_config.altcoins)
    metadata = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_start_utc": initial_signal_date.isoformat(),
        "first_execution_utc": first_execution_date.isoformat(),
        "analysis_end_utc": analysis_prices.index[-1].isoformat(),
        "initial_signal_utc": initial_signal_date.isoformat(),
        "assets": assets,
        "dataset_provenance": dataset_provenance,
        "strategy": strategy_payload,
        "backtest": {
            **asdict(backtest_config),
            "accounting_currency": "USDT",
            "capital_reference": "EUR 2,000 equivalent",
        },
        "signal_execution_rule": "signal at week-end close t; fill at next available close t+1",
        "benchmark_rule": "static order at analysis start; fill on next bar; hold thereafter",
        "optimization_performed": False,
        "run_kind": "historical_fixed_baseline_backtest",
        "locked_forward_candidate_evaluation": False,
        "canonical_run": True,
        "effective_configuration": {
            "strategy": strategy_payload,
            "backtest": asdict(backtest_config),
            "assets": assets,
            "timeframe": "1d",
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    paths["run_metadata"] = metadata_path
    LOGGER.info("Backtest completed; comparison=%s", paths["comparison_markdown"])
    return {"run_id": run_id, "paths": paths, "metrics": metrics, "metadata": metadata}


def main() -> None:
    result = run_research_backtest()
    print(f"Historical fixed-baseline backtest {result['run_id']} completed")
    print("This is not the locked forward candidate evaluation.")
    print(f"Comparison: {result['paths']['comparison_markdown']}")


if __name__ == "__main__":
    main()
