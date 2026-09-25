"""Deterministic fee and slippage model."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real


@dataclass(frozen=True)
class ExecutionCostModel:
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005

    def __post_init__(self) -> None:
        for name, value in (
            ("fee_rate", self.fee_rate),
            ("slippage_rate", self.slippage_rate),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
                or not 0 <= value < 1
            ):
                raise ValueError(f"{name} must be a finite numeric value in [0, 1)")

    def execution_price(self, market_price: float, side: str) -> float:
        if (
            isinstance(market_price, bool)
            or not isinstance(market_price, Real)
            or not math.isfinite(float(market_price))
            or market_price <= 0
        ):
            raise ValueError("market_price must be a positive finite number")
        if not isinstance(side, str):
            raise ValueError("side must be BUY or SELL")
        normalized = side.upper()
        if normalized == "BUY":
            return market_price * (1.0 + self.slippage_rate)
        if normalized == "SELL":
            return market_price * (1.0 - self.slippage_rate)
        raise ValueError(f"Unsupported side: {side}")

    def fee(self, quantity: float, execution_price: float) -> float:
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, Real)
            or not math.isfinite(float(quantity))
        ):
            raise ValueError("quantity must be a finite number")
        if (
            isinstance(execution_price, bool)
            or not isinstance(execution_price, Real)
            or not math.isfinite(float(execution_price))
            or execution_price <= 0
        ):
            raise ValueError("execution_price must be a positive finite number")
        return abs(float(quantity) * float(execution_price)) * float(self.fee_rate)
