"""Persistent, research-only paper broker. It never sends exchange orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
from typing import Any
import uuid

import pandas as pd

from src.paper_store import PaperStore, ReconciliationResult
from src.strategy import StrategyConfig, generate_signal


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
class MarketSnapshot:
    closes: pd.DataFrame
    quotes: dict[str, Quote]
    fetched_at: pd.Timestamp


@dataclass(frozen=True)
class PaperConfig:
    assets: tuple[str, ...]
    account_id: str = "locked_strategy"
    initial_cash: float = 2_000.0
    fee_rate: float = 0.001
    minimum_spread_rate: float = 0.0002
    slippage_rate: float = 0.0005
    schedule_weekday: int = 0
    schedule_hour: int = 9
    schedule_minute: int = 5
    execution_target_minute: int = 10
    schedule_window_minutes: int = 30
    max_data_staleness_minutes: int = 720
    max_quote_staleness_minutes: int = 5
    quantity_tolerance: float = 1e-12
    rebalance_days: int = 7
    locked_candidate_id: str = "mw120_sw00_ma150_n2_r07_v30"
    strategy_config: StrategyConfig = field(
        default_factory=lambda: StrategyConfig(
            momentum_long_days=120,
            momentum_skip_days=0,
            btc_moving_average_days=150,
            max_assets=2,
            volatility_days=30,
        )
    )

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not self.assets:
            raise ValueError("assets cannot be empty")
        for rate in (self.fee_rate, self.minimum_spread_rate, self.slippage_rate):
            if not 0 <= rate < 1:
                raise ValueError("cost rates must be in [0, 1)")


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
        self.store = PaperStore(
            database_path,
            account_id=config.account_id,
            initial_cash=config.initial_cash,
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
        closes = snapshot.closes.sort_index()
        if closes.index.tz is None:
            return "Invalid data: close timestamps are not timezone-aware"
        if closes.index.has_duplicates:
            return "Invalid data: duplicate close timestamps"
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
        expected = pd.date_range(closes.index.min(), closes.index.max(), freq="D", tz="UTC")
        if not closes.index.equals(expected):
            return "Missing data: daily close calendar has gaps"
        latest = closes.index[-1].tz_convert("UTC")
        expected_latest = now.normalize() - pd.Timedelta(days=1)
        if latest > expected_latest:
            return "Invalid data: incomplete current daily bar included"
        candle_age = now - (latest + pd.Timedelta(days=1))
        if candle_age > pd.Timedelta(minutes=self.config.max_data_staleness_minutes):
            return f"Stale data: latest finalized daily bar is {latest.isoformat()}"
        for asset in self.config.assets:
            quote = snapshot.quotes.get(asset)
            if quote is None:
                return f"Missing data: quote missing for {asset}"
            if (
                not all(math.isfinite(value) and value > 0 for value in (quote.bid, quote.ask, quote.last))
                or quote.bid > quote.ask
            ):
                return f"Invalid data: malformed quote for {asset}"
            quote_time = self._utc(quote.timestamp)
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
        for asset in self.config.assets:
            current = positions.get(asset, {}).get("quantity", 0.0)
            target_weight = signal.target_weights.get(asset, 0.0)
            desired = equity * target_weight / snapshot.quotes[asset].mid
            delta = desired - current
            if abs(delta) <= self.config.quantity_tolerance:
                continue
            side = "BUY" if delta > 0 else "SELL"
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
                    "requested_quantity": abs(delta),
                    "target_weight": target_weight,
                }
            )
        return signal_timestamp, proposals

    def _execution_terms(self, quote: Quote, side: str) -> dict[str, float]:
        mid = quote.mid
        observed_half_spread = max((quote.ask - quote.bid) / (2.0 * mid), 0.0)
        half_spread = max(observed_half_spread, self.config.minimum_spread_rate / 2.0)
        if side == "BUY":
            spread_price = mid * (1.0 + half_spread)
            execution_price = spread_price * (1.0 + self.config.slippage_rate)
        else:
            spread_price = mid * (1.0 - half_spread)
            execution_price = spread_price * (1.0 - self.config.slippage_rate)
        return {
            "mid": mid,
            "spread_price": spread_price,
            "execution_price": execution_price,
            "half_spread": half_spread,
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
            sells = [proposal for proposal in proposals if proposal["side"] == "SELL"]
            buys = [proposal for proposal in proposals if proposal["side"] == "BUY"]

            executable: list[tuple[dict[str, Any], float, dict[str, float]]] = []
            for proposal in sells:
                held = positions.get(proposal["symbol"], {}).get("quantity", 0.0)
                quantity = min(proposal["requested_quantity"], held)
                if quantity > self.config.quantity_tolerance:
                    executable.append(
                        (proposal, quantity, self._execution_terms(snapshot.quotes[proposal["symbol"]], "SELL"))
                    )
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
                fee = quantity * execution_price * self.config.fee_rate
                spread_cost = quantity * abs(terms["spread_price"] - terms["mid"])
                slippage_cost = quantity * abs(execution_price - terms["spread_price"])
                current = positions.get(symbol, {"quantity": 0.0, "average_cost": 0.0})
                if side == "SELL":
                    cash_delta = quantity * execution_price - fee
                    new_quantity = current["quantity"] - quantity
                    new_average = current["average_cost"] if new_quantity > self.config.quantity_tolerance else 0.0
                    quantity_delta = -quantity
                else:
                    cash_delta = -(quantity * execution_price + fee)
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
                    "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?)",
                    [
                        order_id,
                        proposal["idempotency_key"],
                        run_id,
                        self.config.account_id,
                        signal_timestamp,
                        symbol,
                        side,
                        proposal["requested_quantity"],
                        proposal["target_weight"],
                        now,
                    ],
                )
                connection.execute(
                    "INSERT INTO paper_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    quantity = proposal["requested_quantity"] * scale
                    if quantity > self.config.quantity_tolerance:
                        persist_fill(
                            proposal,
                            quantity,
                            buy_terms[proposal["idempotency_key"]],
                        )
            if cash < -1e-8:
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
        reconciliation = self.store.reconcile()
        if not reconciliation.valid:
            self.store.activate_kill_switch(
                reconciliation.message, run_id=run_id, now=now_ts.to_pydatetime()
            )
            status = "KILL_SWITCH"
            message = reconciliation.message
        else:
            status = "EXECUTED"
            message = f"Executed {len(proposals)} idempotent paper orders"
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
