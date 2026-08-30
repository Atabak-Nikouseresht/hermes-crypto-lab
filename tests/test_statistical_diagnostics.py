import json
from pathlib import Path

import numpy as np
import pytest

from src.statistical_diagnostics import (
    block_bootstrap_ci,
    generate_statistical_diagnostic,
    probabilistic_sharpe_ratio,
)


def test_probabilistic_sharpe_and_bootstrap_are_deterministic():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    psr = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0, periods_per_year=365)
    first = block_bootstrap_ci(returns, statistic="mean", block_size=5, replications=500, seed=7)
    second = block_bootstrap_ci(returns, statistic="mean", block_size=5, replications=500, seed=7)

    assert 0 <= psr <= 1
    assert first == second
    assert first["status"] == "OK"


def test_probabilistic_sharpe_matches_reference_with_annualized_benchmark():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    result = probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=0.25,
        periods_per_year=365,
    )

    assert result == pytest.approx(0.9998590203062097, abs=1e-12)


def test_probabilistic_sharpe_is_frequency_invariant_for_equivalent_benchmarks():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    daily = probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=0.25,
        periods_per_year=365,
    )
    weekly = probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=0.25 * np.sqrt(52 / 365),
        periods_per_year=52,
    )

    assert weekly == pytest.approx(daily, abs=1e-12)


def test_tracked_post_selection_diagnostics_use_corrected_frequency_scaling(tmp_path):
    run = Path("experiments/runs/20260822T000641481839Z")
    generated_result = generate_statistical_diagnostic(
        equity_path=run / "final_test_equity.parquet",
        fills_path=run / "final_test_fills.parquet",
        training_trials_path=run / "training_trials.parquet",
        output_dir=tmp_path,
    )
    artifact = json.loads(
        Path("audits/statistical/statistical_diagnostics.json").read_text(encoding="utf-8")
    )
    generated = json.loads(
        Path(generated_result["json_path"]).read_text(encoding="utf-8")
    )

    assert artifact["status"] == "POST_SELECTION_DIAGNOSTIC_NOT_SEALED_OOS"
    assert artifact == generated
    assert artifact["probabilistic_sharpe_ratio_vs_zero"] == pytest.approx(
        0.5603306177454201, abs=1e-12
    )
    assert artifact["deflated_sharpe_ratio"] == pytest.approx(
        0.012314525539555088, abs=1e-12
    )


def test_small_sample_is_explicitly_insufficient():
    result = block_bootstrap_ci(np.array([0.01, -0.01]), statistic="sharpe", block_size=2)
    assert result["status"] == "INSUFFICIENT_SAMPLE"


def test_sharpe_bootstrap_defaults_to_daily_annualization():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    default = block_bootstrap_ci(
        returns, statistic="sharpe", block_size=5, replications=500, seed=7
    )
    explicit = block_bootstrap_ci(
        returns,
        statistic="sharpe",
        block_size=5,
        periods_per_year=365,
        replications=500,
        seed=7,
    )

    assert default == explicit


def test_sharpe_bootstrap_supports_alternative_frequency_reference():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    daily = block_bootstrap_ci(
        returns,
        statistic="sharpe",
        block_size=5,
        periods_per_year=365,
        replications=500,
        seed=7,
    )
    weekly = block_bootstrap_ci(
        returns,
        statistic="sharpe",
        block_size=5,
        periods_per_year=52,
        replications=500,
        seed=7,
    )

    scale = np.sqrt(52 / 365)
    assert weekly["lower"] == pytest.approx(daily["lower"] * scale, abs=1e-12)
    assert weekly["upper"] == pytest.approx(daily["upper"] * scale, abs=1e-12)
