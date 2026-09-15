"""Deterministic conditional no-cost replay from persisted forward order intent."""

from __future__ import annotations

import math
from typing import Any


_SCENARIOS = {
    "net_replay": ("execution_price", True),
    "no_fee": ("execution_price", False),
    "frictionless": ("mid_price", False),
}


def _finite(value: Any, field: str, *, positive: bool) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {field}") from error
    if not math.isfinite(parsed) or (parsed <= 0 if positive else parsed < 0):
        raise ValueError(f"Invalid {field}")
    return parsed


def _mark_equity(cash: float, positions: dict[str, float], prices: dict[str, float]) -> float:
    return cash + sum(quantity * prices[symbol] for symbol, quantity in positions.items())


def _fee_rate(order: dict[str, Any]) -> float:
    actual_quantity = _finite(order["quantity"], "filled quantity", positive=True)
    actual_execution = _finite(order["execution_price"], "execution price", positive=True)
    absolute_fee = _finite(order["fee"], "fee", positive=False)
    return absolute_fee / (actual_quantity * actual_execution)


def _replay_one(
    *,
    starting_cash: float,
    starting_positions: dict[str, float],
    steps: list[dict[str, Any]],
    price_field: str,
    include_fee: bool,
) -> dict[str, list[Any]]:
    cash = starting_cash
    positions = dict(starting_positions)
    timestamps: list[str] = []
    equity_points: list[float] = []
    for step in steps:
        prices = {
            str(symbol): _finite(price, f"price for {symbol}", positive=True)
            for symbol, price in step["prices"].items()
        }
        if any(symbol not in prices for symbol in positions):
            raise ValueError("Missing persisted midpoint for held position")
        orders = list(step.get("orders", []))
        for order in sorted(orders, key=lambda item: (0 if item["side"] == "SELL" else 1, item["symbol"])):
            symbol = str(order["symbol"])
            side = order["side"]
            if side not in {"BUY", "SELL"} or symbol not in prices:
                raise ValueError("Invalid persisted counterfactual order intent")
            target_weight = _finite(order["target_weight"], "target weight", positive=False)
            if target_weight > 1:
                raise ValueError("Invalid persisted target weight")
            trade_price = _finite(order[price_field], price_field, positive=True)
            fee_rate = _fee_rate(order) if include_fee else 0.0
            current_quantity = positions.get(symbol, 0.0)
            equity = _mark_equity(cash, positions, prices)
            target_quantity = equity * target_weight / prices[symbol]
            if side == "SELL":
                quantity = min(current_quantity, max(0.0, current_quantity - target_quantity))
                cash += quantity * trade_price * (1.0 - fee_rate)
                positions[symbol] = current_quantity - quantity
            else:
                quantity = max(0.0, target_quantity - current_quantity)
                unit_cost = trade_price * (1.0 + fee_rate)
                quantity = min(quantity, cash / unit_cost)
                cash -= quantity * unit_cost
                positions[symbol] = current_quantity + quantity
            if cash < -1e-9:
                raise ValueError("Counterfactual cash invariant violated")
            cash = max(0.0, cash)
        timestamps.append(str(step["timestamp"]))
        equity_points.append(_mark_equity(cash, positions, prices))
    return {"timestamps": timestamps, "equity": equity_points}


def replay_no_cost_counterfactual(
    *,
    starting_cash: float,
    starting_positions: dict[str, float],
    steps: list[dict[str, Any]],
) -> dict[str, dict[str, list[Any]]]:
    """Replay persisted filled-order intents under explicit cost treatments.

    This conditional reconstruction starts at the factual anchor. It neither
    regenerates signals nor adds a missing order, and it uses only persisted
    midpoint prices, target weights, fills, and fee amounts. Re-targeting each
    persisted intent makes later available cash, quantities, and compounding
    path-dependent while changing only the selected friction components.
    """
    cash = _finite(starting_cash, "starting cash", positive=False)
    positions: dict[str, float] = {}
    for symbol, quantity in starting_positions.items():
        positions[str(symbol)] = _finite(quantity, "starting position", positive=False)
    return {
        name: _replay_one(
            starting_cash=cash,
            starting_positions=positions,
            steps=steps,
            price_field=price_field,
            include_fee=include_fee,
        )
        for name, (price_field, include_fee) in _SCENARIOS.items()
    }
