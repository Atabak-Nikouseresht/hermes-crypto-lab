from __future__ import annotations

import math

import pytest

from src.costs import ExecutionCostModel


def test_default_cost_model_applies_symmetric_slippage_and_fee():
    model = ExecutionCostModel()

    assert model.execution_price(100.0, "BUY") == pytest.approx(100.05)
    assert model.execution_price(100.0, "SELL") == pytest.approx(99.95)
    assert model.execution_price(100.0, "buy") == pytest.approx(100.05)
    assert model.fee(2.0, 100.0) == pytest.approx(0.2)
    assert model.fee(-2.0, 100.0) == pytest.approx(0.2)
    assert model.fee(0.0, 100.0) == 0.0


@pytest.mark.parametrize("field", ["fee_rate", "slippage_rate"])
@pytest.mark.parametrize("value", [True, False, "0.01", math.nan, math.inf, -math.inf, -0.01, 1.0])
def test_cost_model_rejects_invalid_rates(field, value):
    with pytest.raises(ValueError, match=field):
        ExecutionCostModel(**{field: value})


def test_cost_model_accepts_zero_rates():
    model = ExecutionCostModel(fee_rate=0.0, slippage_rate=0.0)

    assert model.execution_price(100.0, "BUY") == 100.0
    assert model.fee(1.0, 100.0) == 0.0


@pytest.mark.parametrize("price", [True, False, 0, -1, math.nan, math.inf, -math.inf, "100"])
def test_execution_price_rejects_invalid_market_prices(price):
    with pytest.raises(ValueError, match="market_price"):
        ExecutionCostModel().execution_price(price, "BUY")


@pytest.mark.parametrize("side", ["HOLD", "buy-now", "", None, 1])
def test_execution_price_rejects_invalid_sides(side):
    with pytest.raises(ValueError, match="side|Unsupported"):
        ExecutionCostModel().execution_price(100.0, side)


@pytest.mark.parametrize("quantity", [True, False, "1", math.nan, math.inf, -math.inf])
def test_fee_rejects_invalid_quantities(quantity):
    with pytest.raises(ValueError, match="quantity"):
        ExecutionCostModel().fee(quantity, 100.0)


@pytest.mark.parametrize("price", [True, False, "100", math.nan, math.inf, -math.inf, 0, -1])
def test_fee_rejects_invalid_execution_prices(price):
    with pytest.raises(ValueError, match="execution_price"):
        ExecutionCostModel().fee(1.0, price)
