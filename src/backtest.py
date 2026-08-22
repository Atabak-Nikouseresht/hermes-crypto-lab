"""Deterministic event-driven close-to-next-close backtesting engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from src.costs import ExecutionCostModel

TargetWeightFunction = Callable[[pd.DataFrame, pd.Timestamp], Any]


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    rebalance_interval_days: int = 7
    quantity_tolerance: float = 1e-12

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.rebalance_interval_days <= 0 or self.rebalance_interval_days % 7 != 0:
            raise ValueError("rebalance_interval_days must be a positive multiple of 7")


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.DataFrame
    cash: pd.Series
    orders: pd.DataFrame
    fills: pd.DataFrame
    positions: pd.DataFrame


class EventDrivenBacktester:
    """Queue weekly close signals and fill them on the next available bar."""

    def __init__(self, close_prices: pd.DataFrame, config: BacktestConfig):
        if close_prices.empty:
            raise ValueError("close_prices cannot be empty")
        prices = close_prices.copy().sort_index()
        if prices.index.tz is None:
            raise ValueError("close_prices index must be timezone-aware UTC")
        prices.index = prices.index.tz_convert("UTC")
        if prices.index.has_duplicates:
            raise ValueError("close_prices index cannot contain duplicates")
        if prices.isna().any().any() or (prices <= 0).any().any():
            raise ValueError("close_prices must contain only positive complete values")
        self.prices = prices.astype(float)
        self.config = config
        self.costs = ExecutionCostModel(config.fee_rate, config.slippage_rate)

    @staticmethod
    def _week_key(timestamp: pd.Timestamp) -> tuple[int, int]:
        iso = timestamp.isocalendar()
        return int(iso.year), int(iso.week)

    def _is_last_available_bar_of_week(self, location: int) -> bool:
        if location >= len(self.prices.index) - 1:
            return False
        return self._week_key(self.prices.index[location]) != self._week_key(
            self.prices.index[location + 1]
        )

    @staticmethod
    def _extract_weights(signal: Any) -> dict[str, float]:
        weights = signal.target_weights if hasattr(signal, "target_weights") else signal
        result = {str(asset): float(weight) for asset, weight in dict(weights).items()}
        if any(weight < -1e-12 for weight in result.values()):
            raise ValueError("Negative target weights are not allowed")
        risky_total = sum(weight for asset, weight in result.items() if asset != "CASH")
        if risky_total > 1.0 + 1e-12:
            raise ValueError("Target weights imply leverage")
        return result

    def run(
        self,
        target_weight_function: TargetWeightFunction | None,
        *,
        initial_target_weights: dict[str, float] | None = None,
        initial_signal_timestamp: pd.Timestamp | None = None,
    ) -> BacktestResult:
        assets = list(self.prices.columns)
        quantities = {asset: 0.0 for asset in assets}
        cash = float(self.config.initial_cash)
        order_records: list[dict[str, Any]] = []
        fill_records: list[dict[str, Any]] = []
        position_records: list[dict[str, Any]] = []
        equity_records: list[dict[str, Any]] = []
        cash_records: list[tuple[pd.Timestamp, float]] = []
        pending_orders: list[dict[str, Any]] = []
        last_rebalance_timestamp: pd.Timestamp | None = None
        next_order_id = 1

        def create_orders(
            weights: dict[str, float], signal_timestamp: pd.Timestamp, prices: pd.Series
        ) -> list[dict[str, Any]]:
            nonlocal next_order_id
            equity = cash + sum(quantities[asset] * prices[asset] for asset in assets)
            created = []
            for asset in assets:
                target_weight = weights.get(asset, 0.0)
                desired_quantity = equity * target_weight / prices[asset]
                delta = desired_quantity - quantities[asset]
                if abs(delta) <= self.config.quantity_tolerance:
                    continue
                side = "BUY" if delta > 0 else "SELL"
                order = {
                    "order_id": next_order_id,
                    "signal_timestamp": signal_timestamp,
                    "symbol": asset,
                    "side": side,
                    "requested_quantity": abs(delta),
                    "target_weight": target_weight,
                }
                next_order_id += 1
                order_records.append(order.copy())
                created.append(order)
            return created

        def execute_orders(
            orders: list[dict[str, Any]], fill_timestamp: pd.Timestamp, prices: pd.Series
        ) -> None:
            nonlocal cash
            sells = [order for order in orders if order["side"] == "SELL"]
            buys = [order for order in orders if order["side"] == "BUY"]
            for order in sells:
                asset = order["symbol"]
                quantity = min(order["requested_quantity"], quantities[asset])
                execution_price = self.costs.execution_price(prices[asset], "SELL")
                fee = self.costs.fee(quantity, execution_price)
                cash += quantity * execution_price - fee
                quantities[asset] -= quantity
                if quantities[asset] < self.config.quantity_tolerance:
                    quantities[asset] = 0.0
                fill_records.append(
                    {
                        **order,
                        "fill_timestamp": fill_timestamp,
                        "filled_quantity": quantity,
                        "market_price": prices[asset],
                        "execution_price": execution_price,
                        "fee": fee,
                        "slippage_cost": quantity * (prices[asset] - execution_price),
                    }
                )

            required = sum(
                order["requested_quantity"]
                * self.costs.execution_price(prices[order["symbol"]], "BUY")
                * (1.0 + self.costs.fee_rate)
                for order in buys
            )
            scale = min(1.0, cash / required) if required > 0 else 1.0
            for order in buys:
                asset = order["symbol"]
                quantity = order["requested_quantity"] * scale
                execution_price = self.costs.execution_price(prices[asset], "BUY")
                fee = self.costs.fee(quantity, execution_price)
                total_cost = quantity * execution_price + fee
                cash -= total_cost
                quantities[asset] += quantity
                fill_records.append(
                    {
                        **order,
                        "fill_timestamp": fill_timestamp,
                        "filled_quantity": quantity,
                        "market_price": prices[asset],
                        "execution_price": execution_price,
                        "fee": fee,
                        "slippage_cost": quantity * (execution_price - prices[asset]),
                    }
                )
            if cash < -1e-8:
                raise RuntimeError(f"Negative cash invariant violated: {cash}")
            if cash < 0:
                cash = 0.0

        for location, timestamp in enumerate(self.prices.index):
            prices = self.prices.iloc[location]
            if pending_orders:
                execute_orders(pending_orders, timestamp, prices)
                pending_orders = []

            equity = cash + sum(quantities[asset] * prices[asset] for asset in assets)
            equity_records.append({"timestamp": timestamp, "equity": equity, "cash": cash})
            cash_records.append((timestamp, cash))
            for asset in assets:
                market_value = quantities[asset] * prices[asset]
                position_records.append(
                    {
                        "timestamp": timestamp,
                        "symbol": asset,
                        "quantity": quantities[asset],
                        "market_value": market_value,
                        "weight": market_value / equity if equity > 0 else 0.0,
                    }
                )

            if location == 0 and initial_target_weights is not None:
                initial = self._extract_weights(initial_target_weights)
                submitted_at = initial_signal_timestamp or timestamp
                pending_orders = create_orders(initial, submitted_at, prices)
            elif (
                target_weight_function is not None
                and self._is_last_available_bar_of_week(location)
                and (
                    last_rebalance_timestamp is None
                    or (timestamp - last_rebalance_timestamp).days
                    >= self.config.rebalance_interval_days
                )
            ):
                signal = target_weight_function(self.prices, timestamp)
                weights = self._extract_weights(signal)
                pending_orders = create_orders(weights, timestamp, prices)
                last_rebalance_timestamp = timestamp

        equity_curve = pd.DataFrame(equity_records).set_index("timestamp")
        cash_series = pd.Series(dict(cash_records), name="cash", dtype=float)
        cash_series.index.name = "timestamp"
        order_columns = [
            "order_id",
            "signal_timestamp",
            "symbol",
            "side",
            "requested_quantity",
            "target_weight",
        ]
        fill_columns = order_columns + [
            "fill_timestamp",
            "filled_quantity",
            "market_price",
            "execution_price",
            "fee",
            "slippage_cost",
        ]
        position_columns = ["timestamp", "symbol", "quantity", "market_value", "weight"]
        return BacktestResult(
            equity_curve=equity_curve,
            cash=cash_series,
            orders=pd.DataFrame(order_records, columns=order_columns),
            fills=pd.DataFrame(fill_records, columns=fill_columns),
            positions=pd.DataFrame(position_records, columns=position_columns),
        )
