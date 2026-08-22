"""Persistent DuckDB state and append-only ledgers for paper trading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

import duckdb


@dataclass(frozen=True)
class ReconciliationResult:
    valid: bool
    message: str


class PaperStore:
    def __init__(self, path: Path, *, account_id: str, initial_cash: float):
        self.path = Path(path)
        self.account_id = account_id
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
                    created_at_utc TIMESTAMPTZ NOT NULL
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
                    filled_at_utc TIMESTAMPTZ NOT NULL
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
                """
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

        A run with fills committed keeps its schedule key and is marked committed.
        A run with no fills releases the schedule key so the weekly decision can retry.
        """
        now = datetime.now(timezone.utc)
        with self.connect() as connection:
            running = connection.execute(
                "SELECT run_id FROM paper_runs WHERE status='RUNNING'"
            ).fetchall()
            for (run_id,) in running:
                fill_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM paper_fills WHERE run_id=?", [run_id]
                    ).fetchone()[0]
                )
                if fill_count:
                    connection.execute(
                        """
                        UPDATE paper_runs
                        SET status='RECOVERED_COMMITTED', completed_at_utc=?,
                            message='Recovered committed fills after process restart'
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

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        completed_at: datetime,
        message: str,
        reconciliation: ReconciliationResult,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_runs SET completed_at_utc=?, status=?, message=?, reconciliation=?
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


def asdict_reconciliation(result: ReconciliationResult) -> dict[str, Any]:
    return {"valid": result.valid, "message": result.message}
