"""Deterministic fee and slippage model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionCostModel:
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005

    def __post_init__(self) -> None:
        if not 0 <= self.fee_rate < 1:
            raise ValueError("fee_rate must be in [0, 1)")
        if not 0 <= self.slippage_rate < 1:
            raise ValueError("slippage_rate must be in [0, 1)")

    def execution_price(self, market_price: float, side: str) -> float:
        if market_price <= 0:
            raise ValueError("market_price must be positive")
        normalized = side.upper()
        if normalized == "BUY":
            return market_price * (1.0 + self.slippage_rate)
        if normalized == "SELL":
            return market_price * (1.0 - self.slippage_rate)
        raise ValueError(f"Unsupported side: {side}")

    def fee(self, quantity: float, execution_price: float) -> float:
        return abs(quantity * execution_price) * self.fee_rate
