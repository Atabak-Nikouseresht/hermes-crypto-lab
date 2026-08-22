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
