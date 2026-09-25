import pandas as pd
import pytest
import numpy as np

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


@pytest.mark.parametrize("annualization_days", [0, -1, True, 365.0])
def test_metrics_reject_invalid_annualization_days(annualization_days):
    dates = pd.date_range("2024-01-01", periods=2, tz="UTC")
    equity = pd.DataFrame({"equity": [100.0, 101.0]}, index=dates)

    with pytest.raises(ValueError, match="annualization_days"):
        calculate_performance_metrics(
            equity, pd.DataFrame(), annualization_days=annualization_days
        )


@pytest.mark.parametrize("confidence", [0, 1, -0.1, 1.1, True, np.nan, np.inf, "0.95"])
def test_metrics_reject_invalid_cvar_confidence(confidence):
    dates = pd.date_range("2024-01-01", periods=2, tz="UTC")
    equity = pd.DataFrame({"equity": [100.0, 101.0]}, index=dates)

    with pytest.raises(ValueError, match="cvar_confidence"):
        calculate_performance_metrics(
            equity, pd.DataFrame(), cvar_confidence=confidence
        )


@pytest.mark.parametrize("values", [[100.0, np.nan], [100.0, np.inf], [100.0, -np.inf]])
def test_metrics_reject_nonfinite_equity(values):
    dates = pd.date_range("2024-01-01", periods=2, tz="UTC")
    equity = pd.DataFrame({"equity": values}, index=dates)

    with pytest.raises(ValueError, match="equity"):
        calculate_performance_metrics(equity, pd.DataFrame())
