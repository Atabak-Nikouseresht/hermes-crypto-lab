import pandas as pd
import pytest

from src.metrics import calculate_performance_metrics


def test_metrics_include_drawdown_recovery_cvar_and_turnover():
    dates = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    equity = pd.DataFrame({"equity": [100.0, 110.0, 90.0, 120.0]}, index=dates)
    fills = pd.DataFrame(
        {"filled_quantity": [1.0], "execution_price": [100.0], "fee": [1.0]}
    )

    metrics = calculate_performance_metrics(equity, fills)

    required = {
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "calmar",
        "cvar_95",
        "turnover",
        "recovery_duration_days",
    }
    assert required <= metrics.keys()
    assert metrics["max_drawdown"] == pytest.approx(1.0 - 90.0 / 110.0)
    assert metrics["recovery_duration_days"] == 2
    assert metrics["cvar_95"] > 0
    assert metrics["turnover"] == pytest.approx(100.0 / equity["equity"].mean())
