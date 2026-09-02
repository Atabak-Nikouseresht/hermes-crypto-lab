"""Persistent DuckDB state and append-only ledgers for paper trading."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION


FINAL_EXECUTABLE_LEDGER_SEMANTICS = "final-executable-v1"


@dataclass(frozen=True)
class ReconciliationResult:
    valid: bool
    message: str


class PaperStore:
    def __init__(
        self,
        path: Path,
        *,
        account_id: str,
        initial_cash: float,
        quantity_tolerance: float = 1e-7,
        fee_rate: float = 0.001,
        minimum_spread_rate: float = 0.0005,
        slippage_rate: float = 0.0005,
    ):
        self.path = Path(path)
        self.account_id = account_id
        self.quantity_tolerance = quantity_tolerance
        self.fee_rate = fee_rate
        self.minimum_spread_rate = minimum_spread_rate
        self.slippage_rate = slippage_rate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(initial_cash)
        self.recover_abandoned_runs()

    def connect(self, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(str(self.path), read_only=read_only)
        connection.execute("SET TimeZone='UTC'")
        return connection

    def _initialize(self, initial_cash: float) -> None:
        now = datetime.now(timezone.utc)
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account_id VARCHAR PRIMARY KEY,
                    initial_cash DOUBLE NOT NULL,
                    cash DOUBLE NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL,
                    updated_at_utc TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cash_ledger (
                    event_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR,
                    account_id VARCHAR NOT NULL,
                    event_type VARCHAR NOT NULL,
                    amount DOUBLE NOT NULL,
                    balance_after DOUBLE NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    account_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    quantity DOUBLE NOT NULL,
                    average_cost DOUBLE NOT NULL,
                    updated_at_utc TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (account_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS position_ledger (
                    event_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    account_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    quantity_delta DOUBLE NOT NULL,
                    quantity_after DOUBLE NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id VARCHAR PRIMARY KEY,
                    idempotency_key VARCHAR UNIQUE NOT NULL,
                    run_id VARCHAR NOT NULL,
                    account_id VARCHAR NOT NULL,
                    signal_timestamp_utc TIMESTAMPTZ NOT NULL,
                    symbol VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    requested_quantity DOUBLE NOT NULL,
                    target_weight DOUBLE NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL,
                    execution_protocol_version VARCHAR,
                    ledger_semantics_version VARCHAR
                );
                CREATE TABLE IF NOT EXISTS paper_fills (
                    fill_id VARCHAR PRIMARY KEY,
                    order_id VARCHAR UNIQUE NOT NULL,
                    run_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    filled_quantity DOUBLE NOT NULL,
                    mid_price DOUBLE NOT NULL,
                    execution_price DOUBLE NOT NULL,
                    spread_cost DOUBLE NOT NULL,
                    slippage_cost DOUBLE NOT NULL,
                    fee DOUBLE NOT NULL,
                    filled_at_utc TIMESTAMPTZ NOT NULL,
                    execution_protocol_version VARCHAR
                );
                CREATE TABLE IF NOT EXISTS paper_legacy_order_semantics (
                    order_id VARCHAR PRIMARY KEY,
                    preserved_at_utc TIMESTAMPTZ NOT NULL,
                    execution_protocol_version VARCHAR
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    snapshot_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR UNIQUE NOT NULL,
                    account_id VARCHAR NOT NULL,
                    cash DOUBLE NOT NULL,
                    positions_value DOUBLE NOT NULL,
                    equity DOUBLE NOT NULL,
                    snapshot_at_utc TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_runs (
                    run_id VARCHAR PRIMARY KEY,
                    started_at_utc TIMESTAMPTZ NOT NULL,
                    completed_at_utc TIMESTAMPTZ,
                    status VARCHAR NOT NULL,
                    mode VARCHAR NOT NULL,
                    schedule_key VARCHAR UNIQUE,
                    signal_timestamp_utc TIMESTAMPTZ,
                    data_timestamp_utc TIMESTAMPTZ,
                    message VARCHAR,
                    reconciliation JSON
                );
                CREATE TABLE IF NOT EXISTS paper_incidents (
                    incident_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR,
                    account_id VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL,
                    cleared_at_utc TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS paper_schema_versions (
                    version INTEGER PRIMARY KEY,
                    applied_at_utc TIMESTAMPTZ NOT NULL,
                    description VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_incidents (
                    incident_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR,
                    incident_type VARCHAR NOT NULL,
                    scheduled_for_utc TIMESTAMPTZ,
                    reason VARCHAR NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL,
                    resolved_at_utc TIMESTAMPTZ,
                    UNIQUE (incident_type, scheduled_for_utc)
                );
                CREATE TABLE IF NOT EXISTS forward_experiments (
                    experiment_id VARCHAR PRIMARY KEY,
                    started_at_utc TIMESTAMPTZ NOT NULL,
                    locked_candidate_id VARCHAR NOT NULL,
                    locked_strategy_hash VARCHAR NOT NULL,
                    governance_hash VARCHAR UNIQUE NOT NULL,
                    specification JSON NOT NULL,
                    status VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_baselines (
                    experiment_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR UNIQUE NOT NULL,
                    observed_at_utc TIMESTAMPTZ NOT NULL,
                    equity DOUBLE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_run_diagnostics (
                    run_id VARCHAR PRIMARY KEY,
                    outcome VARCHAR NOT NULL,
                    regime VARCHAR,
                    btc_vs_trend DOUBLE,
                    momentum JSON,
                    eligibility JSON,
                    selected_assets JSON,
                    current_weights JSON,
                    target_weights JSON,
                    proposed_orders JSON,
                    turnover DOUBLE,
                    kill_switch_active BOOLEAN NOT NULL,
                    reconciliation_valid BOOLEAN NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL,
                    rejected_orders JSON,
                    target_deviation JSON
                );
                CREATE TABLE IF NOT EXISTS forward_market_observations (
                    run_id VARCHAR NOT NULL,
                    observed_at_utc TIMESTAMPTZ NOT NULL,
                    symbol VARCHAR NOT NULL,
                    price DOUBLE NOT NULL,
                    PRIMARY KEY (run_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS forward_schedule_windows (
                    schedule_key VARCHAR PRIMARY KEY,
                    scheduled_for_utc TIMESTAMPTZ NOT NULL,
                    run_id VARCHAR,
                    outcome VARCHAR NOT NULL,
                    created_at_utc TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_experiment_windows (
                    experiment_id VARCHAR NOT NULL,
                    schedule_key VARCHAR NOT NULL,
                    PRIMARY KEY (experiment_id, schedule_key)
                );
                CREATE TABLE IF NOT EXISTS forward_experiment_incidents (
                    experiment_id VARCHAR NOT NULL,
                    incident_id VARCHAR NOT NULL,
                    PRIMARY KEY (experiment_id, incident_id)
                );
                CREATE TABLE IF NOT EXISTS paper_notifications (
                    run_id VARCHAR PRIMARY KEY,
                    target VARCHAR NOT NULL,
                    report_path VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    last_error VARCHAR,
                    created_at_utc TIMESTAMPTZ NOT NULL,
                    updated_at_utc TIMESTAMPTZ NOT NULL,
                    delivered_at_utc TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS notification_attempts (
                    attempt_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    attempted_at_utc TIMESTAMPTZ NOT NULL,
                    status VARCHAR NOT NULL,
                    error VARCHAR
                );
                CREATE TABLE IF NOT EXISTS paper_execution_context (
                    run_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    execution_protocol_version VARCHAR NOT NULL,
                    signal_timestamp_utc TIMESTAMPTZ NOT NULL,
                    finalized_candle_open_utc TIMESTAMPTZ NOT NULL,
                    finalized_candle_close_utc TIMESTAMPTZ NOT NULL,
                    quote_timestamp_utc TIMESTAMPTZ NOT NULL,
                    bid DOUBLE NOT NULL,
                    ask DOUBLE NOT NULL,
                    midpoint DOUBLE NOT NULL,
                    full_spread DOUBLE NOT NULL,
                    execution_timestamp_utc TIMESTAMPTZ NOT NULL,
                    execution_delay_seconds DOUBLE NOT NULL,
                    data_age_seconds DOUBLE NOT NULL,
                    PRIMARY KEY (run_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS paper_order_rejections (
                    run_id VARCHAR NOT NULL,
                    rejection_index INTEGER NOT NULL,
                    symbol VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    stage VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    notional DOUBLE NOT NULL,
                    rejected_at_utc TIMESTAMPTZ NOT NULL,
                    requested_quantity DOUBLE,
                    target_weight DOUBLE,
                    idempotency_key VARCHAR,
                    PRIMARY KEY (run_id, rejection_index)
                );
                CREATE TABLE IF NOT EXISTS paper_forward_execution_evidence (
                    run_id VARCHAR PRIMARY KEY,
                    captured_at_utc TIMESTAMPTZ NOT NULL,
                    diagnostics JSON NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_execution_outcomes (
                    run_id VARCHAR PRIMARY KEY,
                    execution_outcome VARCHAR NOT NULL,
                    recorded_at_utc TIMESTAMPTZ NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES (2, ?, 'forward paper operations')",
                [now],
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES (3, ?, 'forward baseline and monthly benchmark alignment')",
                [now],
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES (4, ?, 'experiment-scoped windows and incidents')",
                [now],
            )
            connection.execute(
                "ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS execution_protocol_version VARCHAR"
            )
            connection.execute(
                "ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS execution_protocol_version VARCHAR"
            )
            connection.execute(
                "ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS ledger_semantics_version VARCHAR"
            )
            connection.execute(
                "ALTER TABLE paper_run_diagnostics "
                "ADD COLUMN IF NOT EXISTS rejected_orders JSON"
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES "
                "(8, ?, 'persist proposal and final execution rejection diagnostics')",
                [now],
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES "
                "(9, ?, 'persist run-attributable paper order rejection audit trail')",
                [now],
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES "
                "(10, ?, 'atomically persist forward execution evidence')",
                [now],
            )

            connection.execute(
                "ALTER TABLE paper_run_diagnostics ADD COLUMN IF NOT EXISTS target_deviation JSON"
            )
            connection.execute(
                "ALTER TABLE paper_order_rejections ADD COLUMN IF NOT EXISTS requested_quantity DOUBLE"
            )
            connection.execute(
                "ALTER TABLE paper_order_rejections ADD COLUMN IF NOT EXISTS target_weight DOUBLE"
            )
            connection.execute(
                "ALTER TABLE paper_order_rejections ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR"
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES "
                "(11, ?, 'explicit execution outcomes and post-execution deviation audit')",
                [now],
            )
            connection.execute(
                "INSERT OR IGNORE INTO paper_schema_versions VALUES (5, ?, 'versioned ask-bid execution context')",
                [now],
            )
            schema_v6 = connection.execute(
                "SELECT 1 FROM paper_schema_versions WHERE version=6"
            ).fetchone()
            if schema_v6 is None:
                connection.execute(
                    """
                    INSERT INTO paper_legacy_order_semantics
                    SELECT order_id, ?, execution_protocol_version FROM paper_orders
                    """,
                    [now],
                )
                connection.execute(
                    "INSERT INTO paper_schema_versions VALUES "
                    "(6, ?, 'final executable order quantity ledger semantics')",
                    [now],
                )
            schema_v7 = connection.execute(
                "SELECT 1 FROM paper_schema_versions WHERE version=7"
            ).fetchone()
            if schema_v7 is None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_legacy_order_semantics
                    SELECT order_id, ?, execution_protocol_version
                    FROM paper_orders WHERE ledger_semantics_version IS NULL
                    """,
                    [now],
                )
                connection.execute(
                    "INSERT INTO paper_schema_versions VALUES "
                    "(7, ?, 'explicit preservation of pre-adoption ledger semantics')",
                    [now],
                )
            existing = connection.execute(
                "SELECT initial_cash FROM paper_accounts WHERE account_id=?",
                [self.account_id],
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO paper_accounts VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
                    [self.account_id, initial_cash, initial_cash, now, now],
                )
                connection.execute(
                    "INSERT INTO cash_ledger VALUES (?, NULL, ?, 'INITIAL_CAPITAL', ?, ?, ?)",
                    [f"initial:{self.account_id}", self.account_id, initial_cash, initial_cash, now],
                )
            elif abs(float(existing[0]) - initial_cash) > 1e-9:
                raise ValueError("Configured initial cash differs from persistent account")

    def recover_abandoned_runs(self) -> int:
        """Recover interrupted runs without replaying committed fills.

        An equity snapshot is the final marker inside the execution transaction, including
        valid zero-fill decisions. Only runs without that marker release their schedule.
        """
        now = datetime.now(timezone.utc)
        with self.connect() as connection:
            running = connection.execute(
                "SELECT run_id FROM paper_runs WHERE status='RUNNING'"
            ).fetchall()
            for (run_id,) in running:
                equity_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM equity_snapshots WHERE run_id=?", [run_id]
                    ).fetchone()[0]
                )
                if equity_count:
                    connection.execute(
                        """
                        UPDATE paper_runs
                        SET status='RECOVERED_COMMITTED', completed_at_utc=?,
                            message='Recovered committed paper state after process restart'
                        WHERE run_id=?
                        """,
                        [now, run_id],
                    )
                else:
                    connection.execute(
                        """
                        UPDATE paper_runs
                        SET status='RECOVERED_ABORTED', completed_at_utc=?, schedule_key=NULL,
                            message='Recovered uncommitted run; schedule released for retry'
                        WHERE run_id=?
                        """,
                        [now, run_id],
                    )
        return len(running)

    def account(self) -> dict[str, Any]:
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT initial_cash, cash, status FROM paper_accounts WHERE account_id=?",
                [self.account_id],
            ).fetchone()
        return {"initial_cash": float(row[0]), "cash": float(row[1]), "status": row[2]}

    def positions(self) -> dict[str, dict[str, float]]:
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT symbol, quantity, average_cost FROM paper_positions WHERE account_id=?",
                [self.account_id],
            ).fetchall()
        return {
            symbol: {"quantity": float(quantity), "average_cost": float(average_cost)}
            for symbol, quantity, average_cost in rows
        }

    def reconcile(self, tolerance: float = 1e-7) -> ReconciliationResult:
        with self.connect(read_only=True) as connection:
            account = connection.execute(
                "SELECT cash FROM paper_accounts WHERE account_id=?", [self.account_id]
            ).fetchone()
            ledger_cash = connection.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM cash_ledger WHERE account_id=?",
                [self.account_id],
            ).fetchone()[0]
            if account is None:
                return ReconciliationResult(False, "Paper account is missing")
            cash = float(account[0])
            if cash < -tolerance:
                return ReconciliationResult(False, f"Negative persistent cash: {cash}")
            if abs(cash - float(ledger_cash)) > tolerance:
                return ReconciliationResult(
                    False, f"Cash mismatch: account={cash}, ledger={ledger_cash}"
                )
            current = {
                symbol: float(quantity)
                for symbol, quantity in connection.execute(
                    "SELECT symbol, quantity FROM paper_positions WHERE account_id=?",
                    [self.account_id],
                ).fetchall()
            }
            ledger = {
                symbol: float(quantity)
                for symbol, quantity in connection.execute(
                    """
                    SELECT symbol, COALESCE(SUM(quantity_delta), 0)
                    FROM position_ledger WHERE account_id=? GROUP BY symbol
                    """,
                    [self.account_id],
                ).fetchall()
            }
            for symbol in set(current) | set(ledger):
                if current.get(symbol, 0.0) < -tolerance:
                    return ReconciliationResult(False, f"Negative position for {symbol}")
                if abs(current.get(symbol, 0.0) - ledger.get(symbol, 0.0)) > tolerance:
                    return ReconciliationResult(False, f"Position mismatch for {symbol}")
            orphan = connection.execute(
                """
                SELECT COUNT(*) FROM paper_fills f
                LEFT JOIN paper_orders o ON o.order_id=f.order_id
                WHERE o.order_id IS NULL OR f.filled_quantity <= 0
                """
            ).fetchone()[0]
            if orphan:
                return ReconciliationResult(False, "Orphan or invalid fill detected")
            invalid_rejection = connection.execute(
                """
                SELECT COUNT(*) FROM paper_order_rejections rejection
                LEFT JOIN paper_runs run ON run.run_id=rejection.run_id
                WHERE run.run_id IS NULL
                   OR rejection.side NOT IN ('BUY', 'SELL')
                   OR rejection.stage NOT IN ('PROPOSAL', 'FINAL', 'FINAL_CASH')
                   OR NOT isfinite(rejection.notional)
                   OR rejection.notional < 0
                """
            ).fetchone()[0]
            if invalid_rejection:
                return ReconciliationResult(False, "Orphan or invalid order rejection detected")
            missing_fill = connection.execute(
                """
                SELECT COUNT(*) FROM paper_orders o
                LEFT JOIN paper_fills f ON f.order_id=o.order_id
                WHERE o.status='FILLED' AND f.order_id IS NULL
                """
            ).fetchone()[0]
            if missing_fill:
                return ReconciliationResult(False, "FILLED order without fill detected")
            unexpected_fill = connection.execute(
                """
                SELECT COUNT(*) FROM paper_orders o
                JOIN paper_fills f ON f.order_id=o.order_id
                WHERE o.status<>'FILLED'
                """
            ).fetchone()[0]
            if unexpected_fill:
                return ReconciliationResult(False, "Fill attached to non-FILLED order detected")
            relationships = connection.execute(
                """
                SELECT o.order_id, o.symbol, f.symbol, o.side, f.side, o.run_id, f.run_id,
                       o.requested_quantity, f.filled_quantity,
                       o.execution_protocol_version, f.execution_protocol_version,
                       o.ledger_semantics_version,
                       c.execution_protocol_version,
                       legacy.order_id
                FROM paper_orders o
                JOIN paper_fills f ON f.order_id=o.order_id
                LEFT JOIN paper_execution_context c
                  ON c.run_id=o.run_id AND c.symbol=o.symbol
                LEFT JOIN paper_legacy_order_semantics legacy
                  ON legacy.order_id=o.order_id
                """
            ).fetchall()
            for (
                order_id,
                order_symbol,
                fill_symbol,
                order_side,
                fill_side,
                order_run,
                fill_run,
                requested_quantity,
                filled_quantity,
                order_protocol,
                fill_protocol,
                ledger_semantics,
                context_protocol,
                legacy_order_id,
            ) in relationships:
                if not math.isfinite(float(filled_quantity)) or float(filled_quantity) <= 0:
                    return ReconciliationResult(
                        False, f"Invalid fill quantity for {order_id}"
                    )
                if order_symbol != fill_symbol:
                    return ReconciliationResult(
                        False, f"Order/fill symbol mismatch for {order_id}"
                    )
                if order_side != fill_side:
                    return ReconciliationResult(
                        False, f"Order/fill side mismatch for {order_id}"
                    )
                if order_run != fill_run:
                    return ReconciliationResult(
                        False, f"Order/fill run mismatch for {order_id}"
                    )
                if (
                    order_protocol is not None
                    and fill_protocol is not None
                    and order_protocol != fill_protocol
                ):
                    return ReconciliationResult(
                        False, f"Order/fill protocol mismatch for {order_id}"
                    )
                if ledger_semantics not in {
                    None,
                    FINAL_EXECUTABLE_LEDGER_SEMANTICS,
                }:
                    return ReconciliationResult(
                        False, f"Unsupported ledger semantics for {order_id}"
                    )
                if legacy_order_id is not None:
                    if ledger_semantics is not None:
                        return ReconciliationResult(
                            False, f"Preserved legacy semantics altered for {order_id}"
                        )
                    continue
                if ledger_semantics is None:
                    return ReconciliationResult(
                        False, f"Missing ledger semantics for current order {order_id}"
                    )
                if ledger_semantics == FINAL_EXECUTABLE_LEDGER_SEMANTICS:
                    if (
                        not math.isfinite(float(requested_quantity))
                        or float(requested_quantity) <= self.quantity_tolerance
                    ):
                        return ReconciliationResult(
                            False, f"Invalid current order quantity for {order_id}"
                        )
                    if abs(float(requested_quantity) - float(filled_quantity)) > (
                        self.quantity_tolerance
                    ):
                        return ReconciliationResult(
                            False, f"Order/fill quantity mismatch for {order_id}"
                        )
                    if (
                        order_protocol is None
                        or fill_protocol != order_protocol
                        or context_protocol != order_protocol
                        or order_protocol != EXECUTION_PROTOCOL_VERSION
                    ):
                        return ReconciliationResult(
                            False, f"Current ledger protocol provenance mismatch for {order_id}"
                        )
            accounting_rows = connection.execute(
                """
                SELECT o.order_id, o.run_id, o.account_id, o.symbol, o.side,
                       f.run_id, f.filled_quantity, f.mid_price,
                       f.execution_price, f.spread_cost, f.slippage_cost, f.fee,
                       c.bid, c.ask, c.midpoint,
                       cash.run_id, cash.account_id, cash.amount,
                       position.run_id, position.account_id, position.symbol,
                       position.quantity_delta
                FROM paper_orders o
                JOIN paper_fills f ON f.order_id=o.order_id
                JOIN paper_execution_context c
                  ON c.run_id=o.run_id AND c.symbol=o.symbol
                LEFT JOIN cash_ledger cash ON cash.event_id='cash_' || f.fill_id
                LEFT JOIN position_ledger position ON position.event_id='pos_' || f.fill_id
                WHERE o.ledger_semantics_version=?
                """,
                [FINAL_EXECUTABLE_LEDGER_SEMANTICS],
            ).fetchall()
            for row in accounting_rows:
                (
                    order_id,
                    order_run_id,
                    order_account_id,
                    order_symbol,
                    side,
                    fill_run_id,
                    quantity,
                    fill_mid,
                    execution_price,
                    spread_cost,
                    slippage_cost,
                    fee,
                    bid,
                    ask,
                    context_mid,
                    cash_run_id,
                    cash_account_id,
                    cash_delta,
                    position_run_id,
                    position_account_id,
                    position_symbol,
                    position_delta,
                ) = row
                if (
                    fill_run_id != order_run_id
                    or cash_run_id != order_run_id
                    or position_run_id != order_run_id
                    or cash_account_id != order_account_id
                    or position_account_id != order_account_id
                    or position_symbol != order_symbol
                ):
                    return ReconciliationResult(
                        False, f"Fill/ledger provenance mismatch for {order_id}"
                    )
                values = (
                    quantity,
                    fill_mid,
                    execution_price,
                    spread_cost,
                    slippage_cost,
                    fee,
                    bid,
                    ask,
                    context_mid,
                    cash_delta,
                    position_delta,
                )
                if any(value is None or not math.isfinite(float(value)) for value in values):
                    return ReconciliationResult(False, f"Non-finite fill accounting for {order_id}")
                quantity = float(quantity)
                fill_mid = float(fill_mid)
                execution_price = float(execution_price)
                spread_cost = float(spread_cost)
                slippage_cost = float(slippage_cost)
                fee = float(fee)
                context_mid = float(context_mid)
                expected_position = quantity if side == "BUY" else -quantity
                expected_cash = (
                    -(quantity * execution_price + fee)
                    if side == "BUY"
                    else quantity * execution_price - fee
                )

                def differs(actual: float, expected: float) -> bool:
                    return abs(actual - expected) > tolerance * max(1.0, abs(expected))

                if differs(fill_mid, context_mid):
                    return ReconciliationResult(False, f"Fill midpoint mismatch for {order_id}")
                if differs(float(position_delta), expected_position):
                    return ReconciliationResult(False, f"Fill/position ledger mismatch for {order_id}")
                if differs(float(cash_delta), expected_cash):
                    return ReconciliationResult(False, f"Fill/cash ledger mismatch for {order_id}")
                if min(execution_price, spread_cost, slippage_cost, fee) < 0:
                    return ReconciliationResult(False, f"Invalid fill cost for {order_id}")
                if side == "BUY":
                    expected_spread_price = max(
                        float(ask), context_mid * (1.0 + self.minimum_spread_rate)
                    )
                    expected_execution_price = expected_spread_price * (1.0 + self.slippage_rate)
                else:
                    expected_spread_price = min(
                        float(bid), context_mid * (1.0 - self.minimum_spread_rate)
                    )
                    expected_execution_price = expected_spread_price * (1.0 - self.slippage_rate)
                expected_spread = quantity * abs(expected_spread_price - context_mid)
                expected_slippage = quantity * abs(
                    expected_execution_price - expected_spread_price
                )
                expected_fee = quantity * expected_execution_price * self.fee_rate
                if (
                    differs(execution_price, expected_execution_price)
                    or differs(spread_cost, expected_spread)
                    or differs(slippage_cost, expected_slippage)
                    or differs(fee, expected_fee)
                ):
                    return ReconciliationResult(False, f"Fill spread/slippage mismatch for {order_id}")
        return ReconciliationResult(True, "cash, positions, orders and fills reconcile")

    def activate_kill_switch(
        self, reason: str, *, run_id: str | None, now: datetime
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                "UPDATE paper_accounts SET status='HALTED', updated_at_utc=? WHERE account_id=?",
                [now, self.account_id],
            )
            connection.execute(
                "INSERT INTO paper_incidents VALUES (?, ?, ?, ?, ?, NULL)",
                [str(uuid.uuid4()), run_id, self.account_id, reason, now],
            )
            connection.execute("COMMIT")

    def reset_kill_switch(self, *, now: datetime) -> None:
        reconciliation = self.reconcile()
        if not reconciliation.valid:
            raise ValueError(f"Cannot reset corrupted state: {reconciliation.message}")
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                "UPDATE paper_accounts SET status='ACTIVE', updated_at_utc=? WHERE account_id=?",
                [now, self.account_id],
            )
            connection.execute(
                "UPDATE paper_incidents SET cleared_at_utc=? WHERE account_id=? AND cleared_at_utc IS NULL",
                [now, self.account_id],
            )
            connection.execute("COMMIT")

    def schedule_exists(self, schedule_key: str) -> bool:
        with self.connect(read_only=True) as connection:
            return bool(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_runs WHERE schedule_key=?", [schedule_key]
                ).fetchone()[0]
            )

    def forward_window_exists(self, schedule_key: str) -> bool:
        with self.connect(read_only=True) as connection:
            return bool(
                connection.execute(
                    "SELECT COUNT(*) FROM forward_schedule_windows WHERE schedule_key=?",
                    [schedule_key],
                ).fetchone()[0]
            )

    @staticmethod
    def _active_experiment_id(connection) -> str:
        rows = connection.execute(
            "SELECT experiment_id FROM forward_experiments WHERE status='ACTIVE'"
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(f"Expected exactly one active forward experiment, found {len(rows)}")
        return str(rows[0][0])

    def record_forward_window(
        self,
        *,
        schedule_key: str,
        scheduled_for: datetime,
        run_id: str | None,
        outcome: str,
        now: datetime,
    ) -> None:
        for attempt in range(2):
            try:
                self._record_forward_window_once(
                    schedule_key=schedule_key,
                    scheduled_for=scheduled_for,
                    run_id=run_id,
                    outcome=outcome,
                    now=now,
                )
                return
            except (duckdb.ConstraintException, duckdb.TransactionException):
                if attempt == 1:
                    raise

    def _record_forward_window_once(
        self,
        *,
        schedule_key: str,
        scheduled_for: datetime,
        run_id: str | None,
        outcome: str,
        now: datetime,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                experiment_id = self._active_experiment_id(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO forward_schedule_windows VALUES (?, ?, ?, ?, ?)",
                    [schedule_key, scheduled_for, run_id, outcome, now],
                )
                connection.execute(
                    "INSERT OR IGNORE INTO forward_experiment_windows VALUES (?, ?)",
                    [experiment_id, schedule_key],
                )
                if run_id is not None:
                    connection.execute(
                        """
                        UPDATE forward_schedule_windows SET run_id=?, outcome=?
                        WHERE schedule_key=? AND (run_id IS NULL OR run_id=?)
                        """,
                        [run_id, outcome, schedule_key, run_id],
                    )
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except duckdb.TransactionException:
                    pass
                raise

    def record_forward_incident(
        self,
        *,
        incident_type: str,
        reason: str,
        now: datetime,
        run_id: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> None:
        for attempt in range(2):
            try:
                self._record_forward_incident_once(
                    incident_type=incident_type,
                    reason=reason,
                    now=now,
                    run_id=run_id,
                    scheduled_for=scheduled_for,
                )
                return
            except (duckdb.ConstraintException, duckdb.TransactionException):
                if attempt == 1:
                    raise

    def _record_forward_incident_once(
        self,
        *,
        incident_type: str,
        reason: str,
        now: datetime,
        run_id: str | None,
        scheduled_for: datetime | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                experiment_id = self._active_experiment_id(connection)
                existing = None
                if scheduled_for is not None:
                    existing = connection.execute(
                        """
                        SELECT incident_id FROM forward_incidents
                        WHERE incident_type=? AND scheduled_for_utc=?
                        ORDER BY created_at_utc, incident_id
                        LIMIT 1
                        """,
                        [incident_type, scheduled_for],
                    ).fetchone()
                if existing is None:
                    proposed_incident_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT OR IGNORE INTO forward_incidents VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        [
                            proposed_incident_id,
                            run_id,
                            incident_type,
                            scheduled_for,
                            reason,
                            now,
                        ],
                    )
                    if scheduled_for is None:
                        existing = (proposed_incident_id,)
                    else:
                        existing = connection.execute(
                            """
                            SELECT incident_id FROM forward_incidents
                            WHERE incident_type=? AND scheduled_for_utc=?
                            ORDER BY created_at_utc, incident_id
                            LIMIT 1
                            """,
                            [incident_type, scheduled_for],
                        ).fetchone()
                if existing is None:
                    raise RuntimeError("Incident persistence did not produce an incident row")
                incident_id = str(existing[0])
                connection.execute(
                    "INSERT OR IGNORE INTO forward_experiment_incidents VALUES (?, ?)",
                    [experiment_id, incident_id],
                )
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except duckdb.TransactionException:
                    pass
                raise

    def ensure_forward_baseline(self, *, run_id: str) -> None:
        with self.connect() as connection:
            experiment_id = self._active_experiment_id(connection)
            existing = connection.execute(
                "SELECT run_id FROM forward_baselines WHERE experiment_id=?",
                [experiment_id],
            ).fetchone()
            if existing is not None:
                return
            snapshot = connection.execute(
                "SELECT snapshot_at_utc, equity FROM equity_snapshots WHERE run_id=?",
                [run_id],
            ).fetchone()
            observation_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM forward_market_observations WHERE run_id=?",
                    [run_id],
                ).fetchone()[0]
            )
            if snapshot is None or observation_count == 0:
                raise ValueError("Cannot establish baseline without equity and market observations")
            connection.execute(
                "INSERT INTO forward_baselines VALUES (?, ?, ?, ?)",
                [experiment_id, run_id, snapshot[0], snapshot[1]],
            )

    def ensure_recovered_forward_baseline(self, *, run_id: str) -> None:
        """Anchor exact committed equity when interrupted market evidence is unavailable."""
        with self.connect() as connection:
            experiment_id = self._active_experiment_id(connection)
            existing = connection.execute(
                "SELECT run_id FROM forward_baselines WHERE experiment_id=?",
                [experiment_id],
            ).fetchone()
            if existing is not None:
                return
            snapshot = connection.execute(
                "SELECT snapshot_at_utc, equity FROM equity_snapshots WHERE run_id=?",
                [run_id],
            ).fetchone()
            if snapshot is None:
                raise ValueError("Cannot recover baseline without committed equity")
            connection.execute(
                "INSERT INTO forward_baselines VALUES (?, ?, ?, ?)",
                [experiment_id, run_id, snapshot[0], snapshot[1]],
            )

    def record_forward_details(
        self,
        *,
        run_id: str,
        outcome: str,
        diagnostics: dict[str, Any],
        observed_prices: dict[str, float],
        observed_at: datetime,
        kill_switch_active: bool,
        reconciliation_valid: bool,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_run_diagnostics (
                    run_id, outcome, regime, btc_vs_trend, momentum, eligibility,
                    selected_assets, current_weights, target_weights, proposed_orders,
                    turnover, kill_switch_active, reconciliation_valid, created_at_utc,
                    rejected_orders, target_deviation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    outcome,
                    diagnostics.get("regime"),
                    diagnostics.get("btc_vs_trend"),
                    json.dumps(diagnostics.get("momentum", {}), sort_keys=True),
                    json.dumps(diagnostics.get("eligibility", {}), sort_keys=True),
                    json.dumps(diagnostics.get("selected_assets", [])),
                    json.dumps(diagnostics.get("current_weights", {}), sort_keys=True),
                    json.dumps(diagnostics.get("target_weights", {}), sort_keys=True),
                    json.dumps(diagnostics.get("proposed_orders", []), sort_keys=True),
                    float(diagnostics.get("turnover", 0.0)),
                    kill_switch_active,
                    reconciliation_valid,
                    observed_at,
                    json.dumps(diagnostics.get("rejected_orders", []), sort_keys=True),
                    json.dumps(diagnostics.get("target_deviation", {}), sort_keys=True),
                ],
            )
            for symbol, price in observed_prices.items():
                connection.execute(
                    "INSERT OR IGNORE INTO forward_market_observations VALUES (?, ?, ?, ?)",
                    [run_id, observed_at, symbol, float(price)],
                )
            connection.execute("COMMIT")

    def record_committed_forward_evidence(
        self,
        connection,
        *,
        run_id: str,
        diagnostics: dict[str, Any],
        observed_prices: dict[str, float],
        observed_at: datetime,
    ) -> None:
        """Stage exact forward evidence inside the paper execution transaction."""
        connection.execute(
            "INSERT INTO paper_forward_execution_evidence VALUES (?, ?, ?)",
            [run_id, observed_at, json.dumps(diagnostics, sort_keys=True)],
        )
        for symbol, price in observed_prices.items():
            connection.execute(
                "INSERT INTO forward_market_observations VALUES (?, ?, ?, ?)",
                [run_id, observed_at, symbol, float(price)],
            )

    def committed_forward_evidence(
        self, run_id: str
    ) -> tuple[dict[str, Any], dict[str, float], datetime] | None:
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT diagnostics, captured_at_utc "
                "FROM paper_forward_execution_evidence WHERE run_id=?",
                [run_id],
            ).fetchone()
            if row is None:
                return None
            observations = connection.execute(
                "SELECT symbol, price FROM forward_market_observations WHERE run_id=?",
                [run_id],
            ).fetchall()
        diagnostics = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
        return diagnostics, {symbol: float(price) for symbol, price in observations}, row[1]

    def insert_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        mode: str,
        schedule_key: str | None,
        signal_timestamp: datetime | None,
        data_timestamp: datetime | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_runs
                (run_id, started_at_utc, completed_at_utc, status, mode, schedule_key,
                 signal_timestamp_utc, data_timestamp_utc, message, reconciliation)
                VALUES (?, ?, NULL, 'RUNNING', ?, ?, ?, ?, NULL, NULL)
                """,
                [run_id, started_at, mode, schedule_key, signal_timestamp, data_timestamp],
            )

    def order_rejections(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT symbol, side, stage, reason, notional
                FROM paper_order_rejections
                WHERE run_id=? ORDER BY rejection_index
                """,
                [run_id],
            ).fetchall()
            legacy = None
            if not rows:
                legacy = connection.execute(
                    "SELECT rejected_orders FROM paper_run_diagnostics WHERE run_id=?",
                    [run_id],
                ).fetchone()
        normalized = [
            {
                "symbol": symbol,
                "side": side,
                "stage": stage,
                "reason": reason,
                "notional": float(notional),
            }
            for symbol, side, stage, reason, notional in rows
        ]
        if normalized or legacy is None or legacy[0] is None:
            return normalized
        return [
            {**item, "stage": item.get("stage", "LEGACY_DIAGNOSTIC")}
            for item in json.loads(legacy[0])
        ]

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        completed_at: datetime,
        message: str,
        reconciliation: ReconciliationResult,
        execution_outcome: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_runs
                SET completed_at_utc=?, status=?, message=?, reconciliation=?
                WHERE run_id=?
                """,
                [
                    completed_at,
                    status,
                    message,
                    json.dumps(asdict_reconciliation(reconciliation), sort_keys=True),
                    run_id,
                ],
            )
            if execution_outcome is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO paper_execution_outcomes VALUES (?, ?, ?)",
                    [run_id, execution_outcome, completed_at],
                )


def asdict_reconciliation(result: ReconciliationResult) -> dict[str, Any]:
    return {"valid": result.valid, "message": result.message}
