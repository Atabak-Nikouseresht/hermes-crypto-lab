"""Backtest artifact persistence and benchmark-comparison reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.backtest import BacktestResult
from src.metrics import calculate_performance_metrics


def write_backtest_report(
    results: dict[str, BacktestResult], reports_dir: Path, run_id: str
) -> tuple[dict[str, Path], dict[str, dict[str, float | int]]]:
    if "Primary Strategy" not in results:
        raise ValueError("results must contain Primary Strategy")
    output_dir = reports_dir / f"backtest_{run_id}"
    output_dir.mkdir(parents=True, exist_ok=False)

    metrics = {
        name: calculate_performance_metrics(result.equity_curve, result.fills)
        for name, result in results.items()
    }
    comparison_json = output_dir / "comparison.json"
    comparison_csv = output_dir / "comparison.csv"
    comparison_markdown = output_dir / "comparison.md"
    comparison_json.write_text(
        json.dumps({"run_id": run_id, "metrics": metrics}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    pd.DataFrame(metrics).T.rename_axis("portfolio").to_csv(comparison_csv)

    lines = [
        "# Backtest comparison",
        "",
        f"- Run ID: `{run_id}`",
        "- Calendar: daily crypto returns, 365-day annualization",
        "- CVaR: positive expected loss in the worst 5% daily-return tail",
        "- Turnover: cumulative execution notional divided by mean equity",
        "",
        "| Portfolio | CAGR | Volatility | Sharpe | Sortino | Max drawdown | Calmar | CVaR 95% | Turnover | Recovery days | Ending equity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in metrics.items():
        lines.append(
            f"| {name} | {values['cagr']:.2%} | {values['volatility']:.2%} | "
            f"{values['sharpe']:.3f} | {values['sortino']:.3f} | "
            f"{values['max_drawdown']:.2%} | {values['calmar']:.3f} | "
            f"{values['cvar_95']:.2%} | {values['turnover']:.3f} | "
            f"{values['recovery_duration_days']} | {values['ending_equity']:.2f} |"
        )
    lines.extend(
        [
            "",
            "Static benchmark orders are submitted on the common analysis start date, filled on the next available bar, and then held without rebalancing. The primary strategy follows the same t-to-t+1 rule.",
            "",
        ]
    )
    comparison_markdown.write_text("\n".join(lines), encoding="utf-8")

    equity_curves = pd.concat(
        {name: result.equity_curve["equity"] for name, result in results.items()}, axis=1
    )
    equity_path = output_dir / "equity_curves.parquet"
    equity_curves.to_parquet(equity_path)

    strategy = results["Primary Strategy"]
    orders_path = output_dir / "strategy_orders.parquet"
    fills_path = output_dir / "strategy_fills.parquet"
    positions_path = output_dir / "strategy_positions.parquet"
    cash_path = output_dir / "strategy_cash.parquet"
    strategy.orders.to_parquet(orders_path, index=False)
    strategy.fills.to_parquet(fills_path, index=False)
    strategy.positions.to_parquet(positions_path, index=False)
    strategy.cash.to_frame().to_parquet(cash_path)

    paths = {
        "output_dir": output_dir,
        "comparison_markdown": comparison_markdown,
        "comparison_json": comparison_json,
        "comparison_csv": comparison_csv,
        "equity_curves": equity_path,
        "strategy_orders": orders_path,
        "strategy_fills": fills_path,
        "strategy_positions": positions_path,
        "strategy_cash": cash_path,
    }
    return paths, metrics
