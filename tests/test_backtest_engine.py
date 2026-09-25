import numpy as np
import pandas as pd
import pytest

from src.backtest import BacktestConfig, EventDrivenBacktester
from src.costs import ExecutionCostModel


def _prices(periods=10, price=100.0):
    dates = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    return pd.DataFrame({"BTC/USDT": price}, index=dates)


def test_week_end_signal_is_filled_only_on_next_available_bar():
    prices = _prices()
    engine = EventDrivenBacktester(prices, BacktestConfig(initial_cash=1_000.0))

    result = engine.run(lambda _prices, _as_of: {"BTC/USDT": 1.0, "CASH": 0.0})

    assert len(result.orders) == 1
    assert result.orders.iloc[0]["signal_timestamp"] == pd.Timestamp("2024-01-07", tz="UTC")
    assert len(result.fills) == 1
    assert result.fills.iloc[0]["fill_timestamp"] == pd.Timestamp("2024-01-08", tz="UTC")
    assert result.fills.iloc[0]["fill_timestamp"] > result.orders.iloc[0]["signal_timestamp"]


def test_execution_cost_model_applies_adverse_slippage_and_fees():
    costs = ExecutionCostModel(fee_rate=0.01, slippage_rate=0.02)

    assert costs.execution_price(100.0, "BUY") == pytest.approx(102.0)
    assert costs.execution_price(100.0, "SELL") == pytest.approx(98.0)
    assert costs.fee(10.0, 102.0) == pytest.approx(10.2)


def test_buy_quantities_are_scaled_to_prevent_negative_cash():
    prices = _prices()
    config = BacktestConfig(initial_cash=1_000.0, fee_rate=0.01, slippage_rate=0.01)
    engine = EventDrivenBacktester(prices, config)

    result = engine.run(lambda _prices, _as_of: {"BTC/USDT": 1.0, "CASH": 0.0})

    assert result.cash.min() >= -1e-9
    fill = result.fills.iloc[0]
    assert fill["filled_quantity"] < result.orders.iloc[0]["requested_quantity"]
    expected_quantity = 1_000.0 / (101.0 * 1.01)
    assert fill["filled_quantity"] == pytest.approx(expected_quantity)
    assert result.cash.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_fourteen_day_rebalance_uses_every_other_week_end():
    prices = _prices(periods=30)
    signal_dates = []
    engine = EventDrivenBacktester(
        prices,
        BacktestConfig(initial_cash=1_000.0, rebalance_interval_days=14),
    )

    def target(_prices, as_of):
        signal_dates.append(as_of)
        return {"CASH": 1.0}

    engine.run(target)

    assert signal_dates == [
        pd.Timestamp("2024-01-07", tz="UTC"),
        pd.Timestamp("2024-01-21", tz="UTC"),
    ]


@pytest.mark.parametrize("price", [np.nan, np.inf, -np.inf, 0.0, -1.0])
def test_backtester_rejects_nonfinite_or_nonpositive_prices(price):
    prices = _prices()
    prices.iloc[0, 0] = price

    with pytest.raises(ValueError, match="positive finite"):
        EventDrivenBacktester(prices, BacktestConfig())


@pytest.mark.parametrize(
    "weights",
    [
        {"BTC/USDT": np.nan},
        {"BTC/USDT": np.inf},
        {"BTC/USDT": -np.inf},
        {"BTC/USDT": -0.1},
        {"BTC/USDT": 1.1},
        {"ETH/USDT": 0.5},
        {"CASH": np.nan},
        {"CASH": 1.1},
        {"BTC/USDT": 0.5, "CASH": 0.25},
        {"BTC/USDT": "0.5"},
    ],
)
def test_backtester_rejects_malformed_or_inconsistent_target_weights(weights):
    engine = EventDrivenBacktester(_prices(), BacktestConfig())

    with pytest.raises(ValueError):
        engine.run(None, initial_target_weights=weights)


def test_backtester_accepts_partial_risky_allocation_without_explicit_cash():
    result = EventDrivenBacktester(_prices(), BacktestConfig(initial_cash=1_000.0)).run(
        None, initial_target_weights={"BTC/USDT": 0.5}
    )

    assert result.orders.iloc[0]["target_weight"] == 0.5


def test_backtester_accepts_consistent_explicit_cash_allocation():
    result = EventDrivenBacktester(_prices(), BacktestConfig(initial_cash=1_000.0)).run(
        None, initial_target_weights={"BTC/USDT": 0.5, "CASH": 0.5}
    )

    assert result.orders.iloc[0]["target_weight"] == 0.5


@pytest.mark.parametrize("tolerance", [-1.0, float("inf"), 1.0])
def test_backtest_config_rejects_tolerances_that_could_bypass_weight_validation(
    tolerance,
):
    with pytest.raises(ValueError, match="quantity_tolerance"):
        BacktestConfig(quantity_tolerance=tolerance)


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_cash": True},
        {"initial_cash": np.nan},
        {"initial_cash": np.inf},
        {"initial_cash": "100"},
        {"rebalance_interval_days": True},
        {"rebalance_interval_days": 7.0},
        {"rebalance_interval_days": 0},
        {"fee_rate": True},
        {"fee_rate": np.nan},
        {"fee_rate": 1.0},
        {"slippage_rate": np.inf},
    ],
)
def test_backtest_config_rejects_malformed_economic_controls(overrides):
    with pytest.raises(ValueError):
        BacktestConfig(**overrides)


@pytest.mark.parametrize(
    ("quantity", "price"),
    [
        (True, 100.0),
        (np.nan, 100.0),
        (np.inf, 100.0),
        (1.0, True),
        (1.0, np.nan),
        (1.0, np.inf),
        (1.0, 0.0),
    ],
)
def test_execution_cost_fee_rejects_malformed_values(quantity, price):
    with pytest.raises(ValueError):
        ExecutionCostModel().fee(quantity, price)


@pytest.mark.parametrize("price", [True, np.nan, np.inf, -np.inf, 0.0, -1.0])
def test_execution_price_rejects_malformed_market_price(price):
    with pytest.raises(ValueError):
        ExecutionCostModel().execution_price(price, "BUY")


def test_execution_price_rejects_non_string_side():
    with pytest.raises(ValueError, match="side"):
        ExecutionCostModel().execution_price(100.0, None)
