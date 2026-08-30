"""Persistent, research-only paper broker. It never sends exchange orders."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

import pandas as pd

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.paper_store import FINAL_EXECUTABLE_LEDGER_SEMANTICS, PaperStore
from src.strategy import StrategyConfig, generate_signal
from src.validate_data import validate_ohlcv


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    last: float
    timestamp: pd.Timestamp

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class SymbolRules:
    active: bool
    min_quantity: float
    max_quantity: float | None
    step_size: float
    min_notional: float
    price_tick: float


@dataclass(frozen=True)
class MarketSnapshot:
    closes: pd.DataFrame
    quotes: dict[str, Quote]
    fetched_at: pd.Timestamp
    symbol_rules: dict[str, SymbolRules] = field(default_factory=dict)
    ohlcv: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass(frozen=True)
class PaperConfig:
    assets: tuple[str, ...]
    account_id: str = "locked_strategy"
    initial_cash: float = 2_000.0
    accounting_currency: str = "USDT"
    exchange_id: str = "binance"
    lookback_days: int = 260
    fee_rate: float = 0.001
    minimum_spread_rate: float = 0.0002
    slippage_rate: float = 0.0005
    schedule_weekday: int = 0
    schedule_hour: int = 0
    schedule_minute: int = 5
    execution_target_minute: int = 10
    schedule_window_minutes: int = 15
    max_data_staleness_minutes: int = 30
    max_quote_staleness_minutes: int = 5
    quantity_tolerance: float = 1e-12
    rebalance_days: int = 7
    locked_candidate_id: str = "mw120_sw00_ma150_n2_r07_v30"
    require_exchange_rules: bool = False
    max_abs_daily_return: float = 0.75
    max_volume_ratio: float = 100.0
    strategy_config: StrategyConfig = field(
        default_factory=lambda: StrategyConfig(
            momentum_long_days=120,
            momentum_skip_days=0,
            btc_moving_average_days=150,
            max_assets=2,
            volatility_days=30,
        )
    )

    @classmethod
    def from_locked_candidate(
        cls,
        *,
        assets: tuple[str, ...],
        locked_candidate_id: str,
        **values: Any,
    ) -> PaperConfig:
        match = re.fullmatch(
            r"mw(?P<momentum>\d+)_sw(?P<skip>\d+)_ma(?P<trend>\d+)_"
            r"n(?P<assets>\d+)_r(?P<rebalance>\d+)_v(?P<volatility>\d+)",
            locked_candidate_id,
        )
        if match is None:
            raise ValueError(f"Invalid locked candidate ID: {locked_candidate_id}")
        parameters = {name: int(value) for name, value in match.groupdict().items()}
        strategy = replace(
            StrategyConfig(),
            momentum_long_days=parameters["momentum"],
            momentum_skip_days=parameters["skip"],
            btc_moving_average_days=parameters["trend"],
            max_assets=parameters["assets"],
            volatility_days=parameters["volatility"],
        )
        return cls(
            assets=assets,
            locked_candidate_id=locked_candidate_id,
            rebalance_days=parameters["rebalance"],
            strategy_config=strategy,
            **values,
        )

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not self.assets:
            raise ValueError("assets cannot be empty")
        for rate in (self.fee_rate, self.minimum_spread_rate, self.slippage_rate):
            if not math.isfinite(rate) or not 0 <= rate < 1:
                raise ValueError("cost rates must be in [0, 1)")
        if not 0 <= self.schedule_weekday <= 6:
            raise ValueError("schedule_weekday must be in [0, 6]")
        if not 0 <= self.schedule_hour <= 23:
            raise ValueError("schedule_hour must be in [0, 23]")
        if not 0 <= self.schedule_minute <= 59:
            raise ValueError("schedule_minute must be in [0, 59]")
        if not 0 <= self.execution_target_minute <= 59:
            raise ValueError("execution_target_minute must be in [0, 59]")
        if self.schedule_window_minutes <= 0:
            raise ValueError("schedule_window_minutes must be positive")
        window_end_minute = self.schedule_minute + self.schedule_window_minutes
        if not self.schedule_minute <= self.execution_target_minute <= window_end_minute:
            raise ValueError("execution_target_minute must fall within the schedule window")
        if self.max_data_staleness_minutes <= 0:
            raise ValueError("max_data_staleness_minutes must be positive")
        if self.max_quote_staleness_minutes <= 0:
            raise ValueError("max_quote_staleness_minutes must be positive")
        if self.max_quote_staleness_minutes > self.max_data_staleness_minutes:
            raise ValueError(
                "max_quote_staleness_minutes cannot exceed max_data_staleness_minutes"
            )
        minimum_lookback = (
            max(
                self.strategy_config.momentum_long_days
                + self.strategy_config.momentum_skip_days,
                self.strategy_config.btc_moving_average_days,
                self.strategy_config.volatility_days,
            )
            + 1
        )
        if self.lookback_days < minimum_lookback:
            raise ValueError(
                f"lookback_days must be at least {minimum_lookback} for the strategy"
            )
        if not math.isfinite(self.quantity_tolerance) or not 0 < self.quantity_tolerance < 1:
            raise ValueError("quantity_tolerance must be finite and in (0, 1)")
        if self.rebalance_days <= 0:
            raise ValueError("rebalance_days must be positive")


@dataclass(frozen=True)
class PaperRunResult:
    run_id: str
    status: str
    message: str
    proposed_orders: tuple[dict[str, Any], ...] = ()
    equity: float | None = None
    outcome: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class PaperTradingSystem:
    def __init__(self, database_path: Path, config: PaperConfig):
        self.config = config
        self._last_rejections: list[dict[str, Any]] = []
        self.store = PaperStore(
            database_path,
            account_id=config.account_id,
            initial_cash=config.initial_cash,
            quantity_tolerance=config.quantity_tolerance,
        )

    @staticmethod
    def _utc(value: datetime | pd.Timestamp) -> pd.Timestamp:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware")
        return timestamp.tz_convert("UTC")

    def _scheduled_key(self, now: pd.Timestamp) -> str | None:
        if now.weekday() != self.config.schedule_weekday:
            return None
        scheduled = now.normalize() + pd.Timedelta(
            hours=self.config.schedule_hour, minutes=self.config.schedule_minute
        )
        end = scheduled + pd.Timedelta(minutes=self.config.schedule_window_minutes)
        if scheduled <= now <= end:
            return scheduled.strftime("%Y-%m-%dT%H:%MZ")
        return None

    def _validate_snapshot(self, snapshot: MarketSnapshot, now: pd.Timestamp) -> str | None:
        closes = snapshot.closes
        if closes.index.tz is None:
            return "Invalid data: close timestamps are not timezone-aware"
        if closes.index.has_duplicates:
            return "Invalid data: duplicate close timestamps"
        if not closes.index.is_monotonic_increasing:
            return "Invalid data: close timestamps are not strictly increasing"
        if list(closes.columns) != list(self.config.assets):
            return "Missing data: asset columns do not match configured universe"
        required = max(
            self.config.strategy_config.momentum_long_days,
            self.config.strategy_config.btc_moving_average_days - 1,
            self.config.strategy_config.volatility_days,
        ) + 1
        if len(closes) < required:
            return f"Missing data: requires at least {required} daily bars"
        if closes.isna().any().any() or (closes <= 0).any().any():
            return "Invalid data: missing or non-positive close price"
        if not all(math.isfinite(float(value)) for value in closes.to_numpy().ravel()):
            return "Invalid data: non-finite close price"
        extreme_returns = closes.pct_change().abs().max()
        if (extreme_returns > self.config.max_abs_daily_return).any():
            return "Invalid data: extreme daily price change requires manual review"
        expected = pd.date_range(closes.index.min(), closes.index.max(), freq="D", tz="UTC")
        if not closes.index.equals(expected):
            return "Missing data: daily close calendar has gaps"
        latest = closes.index[-1].tz_convert("UTC")
        expected_latest = now.normalize() - pd.Timedelta(days=1)
        if latest > expected_latest:
            return "Invalid data: incomplete current daily bar included"
        if latest < expected_latest:
            return f"Stale data: expected finalized bar {expected_latest.isoformat()}, got {latest.isoformat()}"
        candle_age = now - (latest + pd.Timedelta(days=1))
        if (
            self._scheduled_key(now) is not None
            and candle_age > pd.Timedelta(minutes=self.config.max_data_staleness_minutes)
        ):
            return f"Stale data: latest finalized daily bar is {latest.isoformat()}"
        for asset in self.config.assets:
            if self.config.require_exchange_rules:
                rules = snapshot.symbol_rules.get(asset)
                if rules is None:
                    return f"Missing data: public exchange filters missing for {asset}"
                if not rules.active:
                    return f"Invalid data: Binance symbol is inactive for {asset}"
            frame = snapshot.ohlcv.get(asset)
            if frame is not None:
                validation = validate_ohlcv(frame)
                if not validation.is_valid:
                    return f"Invalid OHLCV for {asset}: {validation.summary}"
                if (frame["volume"] < 0).any() or not all(
                    math.isfinite(float(value)) for value in frame["volume"]
                ):
                    return f"Invalid volume for {asset}"
                volume_ratio = frame["volume"].replace(0, float("nan")).pct_change().abs()
                if (volume_ratio > self.config.max_volume_ratio).any():
                    return f"Extreme volume change requires manual review for {asset}"
            quote = snapshot.quotes.get(asset)
            if quote is None:
                return f"Missing data: quote missing for {asset}"
            if (
                not all(math.isfinite(value) and value > 0 for value in (quote.bid, quote.ask, quote.last))
                or quote.bid > quote.ask
            ):
                return f"Invalid data: malformed quote for {asset}"
            quote_time = self._utc(quote.timestamp)
            if quote_time > now:
                return f"Invalid data: future quote timestamp for {asset}"
            if now - quote_time > pd.Timedelta(
                minutes=self.config.max_quote_staleness_minutes
            ):
                return f"Stale data: quote for {asset}"
        return None

    def _mark_to_market(self, snapshot: MarketSnapshot) -> tuple[float, float, float]:
        account = self.store.account()
        positions = self.store.positions()
        positions_value = sum(
            state["quantity"] * snapshot.quotes[asset].mid
            for asset, state in positions.items()
        )
        return account["cash"], positions_value, account["cash"] + positions_value

    def _normalize_exchange_quantity(
        self,
        *,
        symbol: str,
        quantity: float,
        validation_price: float,
        snapshot: MarketSnapshot,
    ) -> tuple[float | None, str | None]:
        """Quantize down and validate a final executable quantity."""
        if not math.isfinite(quantity) or quantity <= self.config.quantity_tolerance:
            return None, "non_positive_quantity"
        rules = snapshot.symbol_rules.get(symbol)
        if rules is None:
            if self.config.require_exchange_rules:
                return None, "missing_exchange_rules"
            return quantity, None
        if not rules.active:
            return None, "inactive_symbol"
        if (
            not math.isfinite(validation_price)
            or validation_price <= 0
            or not math.isfinite(rules.min_quantity)
            or rules.min_quantity <= 0
            or not math.isfinite(rules.step_size)
            or rules.step_size <= 0
            or not math.isfinite(rules.min_notional)
            or rules.min_notional <= 0
            or (
                rules.max_quantity is not None
                and (
                    not math.isfinite(rules.max_quantity)
                    or rules.max_quantity <= 0
                    or rules.max_quantity < rules.min_quantity
                )
            )
        ):
            return None, "invalid_exchange_rules"
        try:
            quantity_decimal = Decimal(str(quantity))
            step_decimal = Decimal(str(rules.step_size))
            units = (quantity_decimal / step_decimal).to_integral_value(
                rounding=ROUND_FLOOR
            )
            normalized = float(units * step_decimal)
        except (InvalidOperation, OverflowError, ValueError):
            return None, "invalid_quantity"
        if normalized <= self.config.quantity_tolerance:
            return None, "non_positive_quantity"
        if normalized < rules.min_quantity:
            return None, "below_min_quantity"
        if rules.max_quantity is not None and normalized > rules.max_quantity:
            return None, "above_max_quantity"
        if normalized * validation_price < rules.min_notional:
            return None, "below_min_notional"
        return normalized, None

    def _reject_quantity(
        self,
        *,
        proposal: dict[str, Any],
        reason: str,
        notional: float,
        stage: str,
    ) -> None:
        if not hasattr(self, "_last_rejections"):
            self._last_rejections = []
        self._last_rejections.append(
            {
                "symbol": proposal["symbol"],
                "side": proposal["side"],
                "stage": stage,
                "reason": reason,
                "notional": notional,
            }
        )

    def _proposals(self, snapshot: MarketSnapshot) -> tuple[pd.Timestamp, list[dict[str, Any]]]:
        signal_timestamp = snapshot.closes.index[-1].tz_convert("UTC")
        signal = generate_signal(
            snapshot.closes,
            as_of=signal_timestamp,
            config=self.config.strategy_config,
        )
        account = self.store.account()
        positions = self.store.positions()
        equity = account["cash"] + sum(
            positions.get(asset, {}).get("quantity", 0.0) * snapshot.quotes[asset].mid
            for asset in self.config.assets
        )
        proposals = []
        self._last_rejections = []
        for asset in self.config.assets:
            current = positions.get(asset, {}).get("quantity", 0.0)
            target_weight = signal.target_weights.get(asset, 0.0)
            desired = equity * target_weight / snapshot.quotes[asset].mid
            delta = desired - current
            if abs(delta) <= self.config.quantity_tolerance:
                continue
            side = "BUY" if delta > 0 else "SELL"
            quantity, invalid_reason = self._normalize_exchange_quantity(
                symbol=asset,
                quantity=abs(delta),
                validation_price=snapshot.quotes[asset].mid,
                snapshot=snapshot,
            )
            if invalid_reason:
                self._reject_quantity(
                    proposal={"symbol": asset, "side": side},
                    reason=invalid_reason,
                    notional=abs(delta) * snapshot.quotes[asset].mid,
                    stage="PROPOSAL",
                )
                continue
            assert quantity is not None
            raw_key = (
                f"{self.config.account_id}|{signal_timestamp.isoformat()}|{asset}|"
                f"{self.config.locked_candidate_id}"
            )
            idempotency_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            proposals.append(
                {
                    "idempotency_key": idempotency_key,
                    "symbol": asset,
                    "side": side,
                    "requested_quantity": quantity,
                    "target_weight": target_weight,
                }
            )
        return signal_timestamp, proposals

    def _execution_terms(self, quote: Quote, side: str) -> dict[str, float]:
        mid = quote.mid
        full_spread = quote.ask - quote.bid
        observed_half_spread = max(full_spread / (2.0 * mid), 0.0)
        if side == "BUY":
            minimum_adverse_price = mid * (1.0 + self.config.minimum_spread_rate)
            base_executable_price = max(quote.ask, minimum_adverse_price)
            execution_price = base_executable_price * (1.0 + self.config.slippage_rate)
        else:
            minimum_adverse_price = mid * (1.0 - self.config.minimum_spread_rate)
            base_executable_price = min(quote.bid, minimum_adverse_price)
            execution_price = base_executable_price * (1.0 - self.config.slippage_rate)
        return {
            "mid": mid,
            "spread_price": base_executable_price,
            "base_executable_price": base_executable_price,
            "execution_price": execution_price,
            "half_spread": observed_half_spread,
            "full_spread": full_spread,
            "minimum_spread_applied": base_executable_price != (quote.ask if side == "BUY" else quote.bid),
            "execution_protocol_version": EXECUTION_PROTOCOL_VERSION,
        }

    def _persist_equity(
        self, connection, *, run_id: str, snapshot: MarketSnapshot, now: pd.Timestamp
    ) -> float:
        account = connection.execute(
            "SELECT cash FROM paper_accounts WHERE account_id=?",
            [self.config.account_id],
        ).fetchone()
        cash = float(account[0])
        rows = connection.execute(
            "SELECT symbol, quantity FROM paper_positions WHERE account_id=?",
            [self.config.account_id],
        ).fetchall()
        positions_value = sum(
            float(quantity) * snapshot.quotes[symbol].mid for symbol, quantity in rows
        )
        equity = cash + positions_value
        connection.execute(
            "INSERT INTO equity_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), run_id, self.config.account_id, cash, positions_value, equity, now],
        )
        return equity

    def _execute(
        self,
        *,
        run_id: str,
        signal_timestamp: pd.Timestamp,
        proposals: list[dict[str, Any]],
        snapshot: MarketSnapshot,
        now: pd.Timestamp,
    ) -> float:
        with self.store.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            cash = float(
                connection.execute(
                    "SELECT cash FROM paper_accounts WHERE account_id=?",
                    [self.config.account_id],
                ).fetchone()[0]
            )
            position_rows = connection.execute(
                "SELECT symbol, quantity, average_cost FROM paper_positions WHERE account_id=?",
                [self.config.account_id],
            ).fetchall()
            positions = {
                symbol: {"quantity": float(quantity), "average_cost": float(average_cost)}
                for symbol, quantity, average_cost in position_rows
            }
            finalized_close = signal_timestamp + pd.Timedelta(days=1)
            for proposal in proposals:
                quote = snapshot.quotes[proposal["symbol"]]
                connection.execute(
                    """
                    INSERT INTO paper_execution_context VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        run_id,
                        proposal["symbol"],
                        EXECUTION_PROTOCOL_VERSION,
                        signal_timestamp,
                        signal_timestamp,
                        finalized_close,
                        quote.timestamp,
                        quote.bid,
                        quote.ask,
                        quote.mid,
                        quote.ask - quote.bid,
                        now,
                        (now - finalized_close).total_seconds(),
                        (now - quote.timestamp).total_seconds(),
                    ],
                )
            sells = [proposal for proposal in proposals if proposal["side"] == "SELL"]
            buys = [proposal for proposal in proposals if proposal["side"] == "BUY"]

            executable: list[tuple[dict[str, Any], float, dict[str, float]]] = []
            for proposal in sells:
                held = positions.get(proposal["symbol"], {}).get("quantity", 0.0)
                terms = self._execution_terms(
                    snapshot.quotes[proposal["symbol"]], "SELL"
                )
                requested = min(proposal["requested_quantity"], held)
                quantity, invalid_reason = self._normalize_exchange_quantity(
                    symbol=proposal["symbol"],
                    quantity=requested,
                    validation_price=terms["execution_price"],
                    snapshot=snapshot,
                )
                if invalid_reason:
                    self._reject_quantity(
                        proposal=proposal,
                        reason=invalid_reason,
                        notional=requested * terms["execution_price"],
                        stage="FINAL",
                    )
                    continue
                assert quantity is not None
                executable.append((proposal, quantity, terms))
            buy_required = 0.0
            buy_terms = {}
            for proposal in buys:
                terms = self._execution_terms(snapshot.quotes[proposal["symbol"]], "BUY")
                buy_terms[proposal["idempotency_key"]] = terms
                buy_required += (
                    proposal["requested_quantity"]
                    * terms["execution_price"]
                    * (1.0 + self.config.fee_rate)
                )

            # Sells are persisted first and their proceeds are available to buys.
            def persist_fill(proposal: dict[str, Any], quantity: float, terms: dict[str, float]) -> None:
                nonlocal cash
                symbol = proposal["symbol"]
                side = proposal["side"]
                execution_price = terms["execution_price"]
                executed_notional = quantity * execution_price
                fee = executed_notional * self.config.fee_rate
                spread_cost = quantity * abs(terms["spread_price"] - terms["mid"])
                slippage_cost = quantity * abs(execution_price - terms["spread_price"])
                current = positions.get(symbol, {"quantity": 0.0, "average_cost": 0.0})
                if side == "SELL":
                    cash_delta = quantity * execution_price - fee
                    new_quantity = current["quantity"] - quantity
                    new_average = current["average_cost"] if new_quantity > self.config.quantity_tolerance else 0.0
                    quantity_delta = -quantity
                else:
                    cash_delta = -(executed_notional + fee)
                    new_quantity = current["quantity"] + quantity
                    new_average = (
                        current["quantity"] * current["average_cost"] + quantity * execution_price
                    ) / new_quantity
                    quantity_delta = quantity
                cash += cash_delta
                if abs(new_quantity) <= self.config.quantity_tolerance:
                    new_quantity = 0.0
                positions[symbol] = {"quantity": new_quantity, "average_cost": new_average}
                order_id = "ord_" + proposal["idempotency_key"][:28]
                fill_id = "fill_" + proposal["idempotency_key"][:27]
                connection.execute(
                    """
                    INSERT INTO paper_orders (
                        order_id, idempotency_key, run_id, account_id,
                        signal_timestamp_utc, symbol, side, requested_quantity,
                        target_weight, status, created_at_utc,
                        execution_protocol_version, ledger_semantics_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?, ?)
                    """,
                    [
                        order_id,
                        proposal["idempotency_key"],
                        run_id,
                        self.config.account_id,
                        signal_timestamp,
                        symbol,
                        side,
                        quantity,
                        proposal["target_weight"],
                        now,
                        EXECUTION_PROTOCOL_VERSION,
                        FINAL_EXECUTABLE_LEDGER_SEMANTICS,
                    ],
                )
                connection.execute(
                    "INSERT INTO paper_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        fill_id,
                        order_id,
                        run_id,
                        symbol,
                        side,
                        quantity,
                        terms["mid"],
                        execution_price,
                        spread_cost,
                        slippage_cost,
                        fee,
                        now,
                        EXECUTION_PROTOCOL_VERSION,
                    ],
                )
                connection.execute(
                    "INSERT INTO cash_ledger VALUES (?, ?, ?, 'FILL', ?, ?, ?)",
                    ["cash_" + fill_id, run_id, self.config.account_id, cash_delta, cash, now],
                )
                connection.execute(
                    "INSERT INTO position_ledger VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        "pos_" + fill_id,
                        run_id,
                        self.config.account_id,
                        symbol,
                        quantity_delta,
                        new_quantity,
                        now,
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (account_id, symbol) DO UPDATE SET
                        quantity=excluded.quantity,
                        average_cost=excluded.average_cost,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    [self.config.account_id, symbol, new_quantity, new_average, now],
                )

            for proposal, quantity, terms in executable:
                persist_fill(proposal, quantity, terms)

            # Recalculate after sell proceeds, then scale all buys proportionally.
            if buy_required > 0:
                scale = min(1.0, cash / buy_required)
                for proposal in buys:
                    terms = buy_terms[proposal["idempotency_key"]]
                    scaled = proposal["requested_quantity"] * scale
                    quantity, invalid_reason = self._normalize_exchange_quantity(
                        symbol=proposal["symbol"],
                        quantity=scaled,
                        validation_price=terms["execution_price"],
                        snapshot=snapshot,
                    )
                    if invalid_reason:
                        self._reject_quantity(
                            proposal=proposal,
                            reason=invalid_reason,
                            notional=scaled * terms["execution_price"],
                            stage="FINAL",
                        )
                        continue
                    assert quantity is not None
                    executed_notional = quantity * terms["execution_price"]
                    required_cash = executed_notional + (
                        executed_notional * self.config.fee_rate
                    )
                    if required_cash > cash:
                        self._reject_quantity(
                            proposal=proposal,
                            reason="insufficient_cash_after_rounding",
                            notional=quantity * terms["execution_price"],
                            stage="FINAL",
                        )
                        continue
                    persist_fill(proposal, quantity, terms)
            if cash < 0.0:
                connection.execute("ROLLBACK")
                raise RuntimeError(f"Negative cash invariant violated: {cash}")
            cash = max(0.0, cash)
            connection.execute(
                "UPDATE paper_accounts SET cash=?, updated_at_utc=? WHERE account_id=?",
                [cash, now, self.config.account_id],
            )
            equity = self._persist_equity(
                connection, run_id=run_id, snapshot=snapshot, now=now
            )
            for index, rejection in enumerate(self._last_rejections):
                connection.execute(
                    """
                    INSERT INTO paper_order_rejections VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        run_id,
                        index,
                        rejection["symbol"],
                        rejection["side"],
                        rejection.get("stage", "PROPOSAL"),
                        rejection["reason"],
                        rejection["notional"],
                        now,
                    ],
                )
            connection.execute("COMMIT")
        return equity

    def run(
        self,
        snapshot: MarketSnapshot,
        *,
        now: datetime,
        dry_run: bool,
    ) -> PaperRunResult:
        now_ts = self._utc(now)
        run_id = (
            "paper_"
            + now_ts.strftime("%Y%m%dT%H%M%S%fZ")
            + "_"
            + uuid.uuid4().hex[:8]
        )
        account = self.store.account()
        if account["status"] != "ACTIVE":
            return PaperRunResult(run_id, "KILL_SWITCH", "Paper account is halted")

        reconciliation = self.store.reconcile()
        if not reconciliation.valid:
            self.store.insert_run(
                run_id=run_id,
                started_at=now_ts.to_pydatetime(),
                mode="DRY_RUN" if dry_run else "PAPER",
                schedule_key=None,
                signal_timestamp=None,
                data_timestamp=None,
            )
            self.store.activate_kill_switch(
                reconciliation.message, run_id=run_id, now=now_ts.to_pydatetime()
            )
            self.store.finish_run(
                run_id=run_id,
                status="KILL_SWITCH",
                completed_at=now_ts.to_pydatetime(),
                message=reconciliation.message,
                reconciliation=reconciliation,
            )
            return PaperRunResult(run_id, "KILL_SWITCH", reconciliation.message)

        validation_error = self._validate_snapshot(snapshot, now_ts)
        if validation_error:
            self.store.insert_run(
                run_id=run_id,
                started_at=now_ts.to_pydatetime(),
                mode="DRY_RUN" if dry_run else "PAPER",
                schedule_key=None,
                signal_timestamp=None,
                data_timestamp=snapshot.closes.index[-1].to_pydatetime(),
            )
            self.store.activate_kill_switch(
                validation_error, run_id=run_id, now=now_ts.to_pydatetime()
            )
            self.store.finish_run(
                run_id=run_id,
                status="KILL_SWITCH",
                completed_at=now_ts.to_pydatetime(),
                message=validation_error,
                reconciliation=reconciliation,
            )
            return PaperRunResult(run_id, "KILL_SWITCH", validation_error)

        schedule_key = self._scheduled_key(now_ts)
        if schedule_key is None:
            self.store.insert_run(
                run_id=run_id,
                started_at=now_ts.to_pydatetime(),
                mode="DRY_RUN" if dry_run else "PAPER",
                schedule_key=None,
                signal_timestamp=None,
                data_timestamp=snapshot.closes.index[-1].to_pydatetime(),
            )
            with self.store.connect() as connection:
                equity = self._persist_equity(
                    connection, run_id=run_id, snapshot=snapshot, now=now_ts
                )
            reconciliation = self.store.reconcile()
            self.store.finish_run(
                run_id=run_id,
                status="NO_REBALANCE",
                completed_at=now_ts.to_pydatetime(),
                message="Market health checked; outside scheduled rebalance window",
                reconciliation=reconciliation,
            )
            return PaperRunResult(
                run_id,
                "NO_REBALANCE",
                "Market health checked; outside scheduled rebalance window",
                equity=equity,
            )

        signal_timestamp, proposals = self._proposals(snapshot)
        if not dry_run and self.store.schedule_exists(schedule_key):
            return PaperRunResult(
                run_id,
                "DUPLICATE_SCHEDULE",
                f"Schedule {schedule_key} was already executed",
            )

        self.store.insert_run(
            run_id=run_id,
            started_at=now_ts.to_pydatetime(),
            mode="DRY_RUN" if dry_run else "PAPER",
            schedule_key=None if dry_run else schedule_key,
            signal_timestamp=signal_timestamp.to_pydatetime(),
            data_timestamp=snapshot.closes.index[-1].to_pydatetime(),
        )
        if dry_run:
            with self.store.connect() as connection:
                equity = self._persist_equity(
                    connection, run_id=run_id, snapshot=snapshot, now=now_ts
                )
            reconciliation = self.store.reconcile()
            self.store.finish_run(
                run_id=run_id,
                status="DRY_RUN",
                completed_at=now_ts.to_pydatetime(),
                message=f"Generated {len(proposals)} proposals; no state-changing trades",
                reconciliation=reconciliation,
            )
            return PaperRunResult(
                run_id,
                "DRY_RUN",
                f"Generated {len(proposals)} proposals; no state-changing trades",
                tuple(proposals),
                equity,
            )

        equity = self._execute(
            run_id=run_id,
            signal_timestamp=signal_timestamp,
            proposals=proposals,
            snapshot=snapshot,
            now=now_ts,
        )
        with self.store.connect(read_only=True) as connection:
            executed_orders = int(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_orders WHERE run_id=?", [run_id]
                ).fetchone()[0]
            )
        rejected_orders = len(self.store.order_rejections(run_id))
        reconciliation = self.store.reconcile()
        if not reconciliation.valid:
            self.store.activate_kill_switch(
                reconciliation.message, run_id=run_id, now=now_ts.to_pydatetime()
            )
            status = "KILL_SWITCH"
            message = reconciliation.message
        else:
            status = "EXECUTED"
            message = (
                f"Executed {executed_orders} idempotent paper orders; "
                f"rejected {rejected_orders} across proposal and final execution validation"
            )
        self.store.finish_run(
            run_id=run_id,
            status=status,
            completed_at=now_ts.to_pydatetime(),
            message=message,
            reconciliation=reconciliation,
        )
        return PaperRunResult(
            run_id, status, message, tuple(proposals), equity
        )
