import numpy as np

from src.statistical_diagnostics import block_bootstrap_ci, probabilistic_sharpe_ratio


def test_probabilistic_sharpe_and_bootstrap_are_deterministic():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    psr = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0, periods_per_year=365)
    first = block_bootstrap_ci(returns, statistic="mean", block_size=5, replications=500, seed=7)
    second = block_bootstrap_ci(returns, statistic="mean", block_size=5, replications=500, seed=7)

    assert 0 <= psr <= 1
    assert first == second
    assert first["status"] == "OK"


def test_small_sample_is_explicitly_insufficient():
    result = block_bootstrap_ci(np.array([0.01, -0.01]), statistic="sharpe", block_size=2)
    assert result["status"] == "INSUFFICIENT_SAMPLE"
