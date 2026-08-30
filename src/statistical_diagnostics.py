"""Post-selection statistical diagnostics with explicit assumption limits."""

from __future__ import annotations

from math import erf, exp, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any
import json

import numpy as np
import pandas as pd


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def probabilistic_sharpe_ratio(
    returns: np.ndarray, *, benchmark_sharpe: float, periods_per_year: int
) -> float:
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3 or values.std(ddof=1) == 0:
        return 0.5
    mean = values.mean()
    std = values.std(ddof=1)
    sample_sharpe = mean / std
    benchmark_sample_sharpe = benchmark_sharpe / sqrt(periods_per_year)
    centered = (values - mean) / std
    skew = float(np.mean(centered**3))
    kurtosis = float(np.mean(centered**4))
    denominator = sqrt(
        max(
            1e-12,
            1.0
            - skew * sample_sharpe
            + ((kurtosis - 1.0) / 4.0) * sample_sharpe**2,
        )
    )
    statistic = (
        (sample_sharpe - benchmark_sample_sharpe)
        * sqrt(len(values) - 1)
        / denominator
    )
    return float(_normal_cdf(statistic))


def block_bootstrap_ci(
    returns: np.ndarray,
    *,
    statistic: str,
    block_size: int,
    periods_per_year: int = 365,
    replications: int = 2000,
    seed: int = 20260822,
) -> dict[str, Any]:
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < max(3, block_size + 1):
        return {"status": "INSUFFICIENT_SAMPLE", "lower": None, "upper": None}
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(replications):
        pieces = []
        while sum(len(piece) for piece in pieces) < len(values):
            start = int(rng.integers(0, len(values)))
            indices = (np.arange(start, start + block_size) % len(values)).astype(int)
            pieces.append(values[indices])
        sample = np.concatenate(pieces)[: len(values)]
        if statistic == "mean":
            result = float(sample.mean())
        elif statistic == "sharpe":
            std = sample.std(ddof=1)
            result = (
                float(sample.mean() / std * sqrt(periods_per_year)) if std > 0 else 0.0
            )
        else:
            raise ValueError(f"Unsupported statistic: {statistic}")
        samples.append(result)
    lower, upper = np.quantile(samples, [0.025, 0.975])
    return {"status": "OK", "lower": float(lower), "upper": float(upper)}


def _expected_max_sharpe(sharpes: np.ndarray, trials: int) -> float:
    values = np.asarray(sharpes, dtype=float)
    sigma = float(values.std(ddof=1))
    mean = float(values.mean())
    if trials <= 1 or sigma == 0:
        return mean
    gamma = 0.5772156649
    normal = NormalDist()
    first = normal.inv_cdf(1.0 - 1.0 / trials)
    second = normal.inv_cdf(1.0 - 1.0 / (trials * exp(1.0)))
    return mean + sigma * ((1.0 - gamma) * first + gamma * second)


def generate_statistical_diagnostic(
    *,
    equity_path: Path,
    fills_path: Path,
    training_trials_path: Path,
    output_dir: Path,
    starting_equity: float = 2000.0,
) -> dict[str, Any]:
    equity_frame = pd.read_parquet(equity_path)
    equity_column = "equity" if "equity" in equity_frame else equity_frame.columns[0]
    equity = equity_frame[equity_column].astype(float)
    returns = equity.pct_change().dropna().to_numpy()
    training = pd.read_parquet(training_trials_path)
    training_sharpes = training["metrics.sharpe"].astype(float).to_numpy()
    expected_max = _expected_max_sharpe(training_sharpes, 96)
    fills = pd.read_parquet(fills_path)
    fees = float(fills["fee"].sum()) if not fills.empty else 0.0
    slippage = float(fills["slippage_cost"].sum()) if not fills.empty else 0.0
    ending = float(equity.iloc[-1])
    base_return = ending / starting_equity - 1.0
    total_modeled_cost = fees + slippage
    cost_sensitivity = {}
    for name, multiplier in {"observed": 1.0, "stressed": 2.0, "extreme": 5.0}.items():
        adjusted_ending = ending - (multiplier - 1.0) * total_modeled_cost
        cost_sensitivity[name] = {
            "adjusted_ending_equity": adjusted_ending,
            "adjusted_return": adjusted_ending / starting_equity - 1.0,
            "analytical_overlay_only": True,
        }
    heatmap = (
        training.groupby(
            ["parameters.momentum_window", "parameters.trend_filter"], as_index=False
        )["penalized_score"]
        .agg(["mean", "median", "std"])
        .reset_index()
        .to_dict(orient="records")
    )
    result = {
        "status": "POST_SELECTION_DIAGNOSTIC_NOT_SEALED_OOS",
        "training_configuration_trials": 96,
        "total_candidate_backtests_disclosed": 117,
        "probabilistic_sharpe_ratio_vs_zero": probabilistic_sharpe_ratio(
            returns, benchmark_sharpe=0.0, periods_per_year=365
        ),
        "deflated_sharpe_ratio": probabilistic_sharpe_ratio(
            returns, benchmark_sharpe=expected_max, periods_per_year=365
        ),
        "deflated_sharpe_benchmark": expected_max,
        "pbo": {
            "status": "INSUFFICIENT_DATA",
            "reason": "No candidate-by-subperiod return matrix was preserved for all 96 configurations; forcing PBO would be invalid.",
        },
        "mean_return_block_bootstrap_95": block_bootstrap_ci(
            returns, statistic="mean", block_size=7, periods_per_year=365
        ),
        "sharpe_block_bootstrap_95": block_bootstrap_ci(
            returns, statistic="sharpe", block_size=7, periods_per_year=365
        ),
        "cost_sensitivity": cost_sensitivity,
        "parameter_neighborhood_summary": heatmap,
        "regime_attribution": {
            "status": "INSUFFICIENT_DATA",
            "reason": "Sealed artifacts do not preserve daily regime labels aligned to return attribution.",
        },
        "asset_contribution": {
            "status": "INSUFFICIENT_DATA",
            "reason": "Exact daily asset-level PnL attribution was not preserved in the sealed result.",
        },
        "trade_contribution": {
            "status": "INSUFFICIENT_DATA",
            "reason": "Round-trip trade grouping is not uniquely defined by the sealed fill ledger.",
        },
        "turnover_decomposition": {
            "total_fees": fees,
            "total_slippage": slippage,
            "base_return": base_return,
        },
        "limitations": [
            "DSR is an approximate post-selection diagnostic and does not restore a fresh holdout.",
            "PBO is intentionally not computed without a valid subperiod matrix.",
            "No diagnostic is used to retune or reselect the locked candidate.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "statistical_diagnostics.json"
    markdown_path = output_dir / "statistical_diagnostics.md"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(
        "\n".join(
            [
                "# Statistical honesty diagnostics",
                "",
                "- Status: **POST_SELECTION_DIAGNOSTIC_NOT_SEALED_OOS**",
                "- Trials disclosed: **96 training configurations / 117 candidate backtests**",
                f"- PSR vs zero: **{result['probabilistic_sharpe_ratio_vs_zero']:.3f}**",
                f"- Deflated Sharpe Ratio: **{result['deflated_sharpe_ratio']:.3f}**",
                "- PBO: **INSUFFICIENT_DATA**",
                f"- Mean-return bootstrap: `{result['mean_return_block_bootstrap_95']}`",
                f"- Sharpe bootstrap: `{result['sharpe_block_bootstrap_95']}`",
                "",
                "> These diagnostics do not change the candidate, acceptance criteria, or sealed final-test result.",
            ]
        ),
        encoding="utf-8",
    )
    result["json_path"] = str(json_path)
    result["markdown_path"] = str(markdown_path)
    return result
