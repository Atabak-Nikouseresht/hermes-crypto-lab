from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

from src.paper_broker import (
    MarketSnapshot,
    PaperConfig,
    PaperRunResult,
    PaperTradingSystem,
    Quote,
    SymbolRules,
    classify_execution_outcome,
)

ASSETS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT"]


def _snapshot(now: datetime, stale_days: int = 0) -> MarketSnapshot:
    end = pd.Timestamp("2024-08-04", tz="UTC") - pd.Timedelta(days=stale_days)
    dates = pd.date_range(end=end, periods=230, freq="D", tz="UTC")
    day = np.arange(len(dates), dtype=float)
    closes = pd.DataFrame(
        {
            "BTC/USDT": 100 * np.exp(0.0010 * day),
            "ETH/USDT": 100 * np.exp(0.0015 * day),
            "BNB/USDT": 100 * np.exp(0.0020 * day),
            "XRP/USDT": 100 * np.exp(0.0007 * day),
            "TRX/USDT": 100 * np.exp(0.0006 * day),
        },
        index=dates,
    )
    quotes = {
        asset: Quote(
            bid=float(closes[asset].iloc[-1]) * 0.9999,
            ask=float(closes[asset].iloc[-1]) * 1.0001,
            last=float(closes[asset].iloc[-1]),
            timestamp=pd.Timestamp(now),
        )
        for asset in ASSETS
    }
    return MarketSnapshot(closes=closes, quotes=quotes, fetched_at=pd.Timestamp(now))


def _config() -> PaperConfig:
    return PaperConfig(
        assets=tuple(ASSETS),
        initial_cash=2_000.0,
        fee_rate=0.001,
        minimum_spread_rate=0.0002,
        slippage_rate=0.0005,
        schedule_weekday=0,
        schedule_hour=9,
        schedule_minute=5,
        schedule_window_minutes=30,
        max_data_staleness_minutes=720,
        max_quote_staleness_minutes=5,
    )


def test_quote_timestamp_skew_fails_closed(tmp_path):
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    system = PaperTradingSystem(
        tmp_path / "quote-skew.duckdb",
        replace(_config(), max_quote_timestamp_skew_seconds=30),
    )
    snapshot = _snapshot(now.to_pydatetime())
    snapshot.quotes["ETH/USDT"] = replace(
        snapshot.quotes["ETH/USDT"], timestamp=now - pd.Timedelta(seconds=31)
    )

    assert "timestamp skew" in system._validate_snapshot(snapshot, now).lower()


@pytest.mark.parametrize(
    ("proposed", "executed", "rejected", "expected"),
    [
        (0, 0, 0, "NO_REBALANCE_REQUIRED"),
        (2, 2, 0, "FULL_EXECUTION"),
        (2, 1, 1, "PARTIAL_EXECUTION"),
        (2, 0, 2, "EXECUTION_REJECTED"),
    ],
)
def test_execution_outcomes_are_explicit(proposed, executed, rejected, expected):
    assert classify_execution_outcome(proposed, executed, rejected) == expected


def test_paper_config_factory_derives_all_economic_parameters_from_candidate_id():
    config = PaperConfig.from_locked_candidate(
        assets=("BTC/USDT",),
        locked_candidate_id="mw120_sw00_ma150_n2_r07_v30",
    )

    assert config.strategy_config.momentum_long_days == 120
    assert config.strategy_config.momentum_skip_days == 0
    assert config.strategy_config.btc_moving_average_days == 150
    assert config.strategy_config.max_assets == 2
    assert config.rebalance_days == 7
    assert config.strategy_config.volatility_days == 30


def test_paper_config_factory_rejects_unparseable_candidate_identity():
    with pytest.raises(ValueError, match="locked candidate ID"):
        PaperConfig.from_locked_candidate(
            assets=("BTC/USDT",),
            locked_candidate_id="not-a-candidate",
        )


def _execute_scaled_buy(
    tmp_path,
    *,
    initial_cash: float,
    min_quantity: float,
    min_notional: float,
) -> tuple[PaperTradingSystem, MarketSnapshot]:
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    database = tmp_path / f"scaled-{initial_cash}-{min_quantity}-{min_notional}.duckdb"
    config = PaperConfig(
        assets=("BTC/USDT",),
        initial_cash=initial_cash,
        fee_rate=0.0,
        minimum_spread_rate=0.0,
        slippage_rate=0.0,
        require_exchange_rules=True,
    )
    system = PaperTradingSystem(database, config)
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(
            {"BTC/USDT": [100.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-08-04T00:00:00Z")]),
        ),
        quotes={
            "BTC/USDT": Quote(
                bid=100.0,
                ask=100.0,
                last=100.0,
                timestamp=now,
            )
        },
        fetched_at=now,
        symbol_rules={
            "BTC/USDT": SymbolRules(
                active=True,
                min_quantity=min_quantity,
                max_quantity=10.0,
                step_size=0.1,
                min_notional=min_notional,
                price_tick=0.01,
            )
        },
    )
    proposal = {
        "idempotency_key": "scaled-buy",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "requested_quantity": 1.0,
        "target_weight": 1.0,
    }
    system._execute(
        run_id="scaled-buy-run",
        signal_timestamp=pd.Timestamp("2024-08-04T00:00:00Z"),
        proposals=[proposal],
        snapshot=snapshot,
        now=now,
    )
    assert proposal["requested_quantity"] == pytest.approx(1.0)
    return system, snapshot


def test_scaled_buy_is_requantized_down_before_persistence(tmp_path):
    system, snapshot = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )

    with system.store.connect(read_only=True) as connection:
        requested, quantity, status, semantics = connection.execute(
            "SELECT o.requested_quantity, f.filled_quantity, o.status, "
            "o.ledger_semantics_version FROM paper_orders o "
            "JOIN paper_fills f USING (order_id)"
        ).fetchone()
        cash = connection.execute("SELECT cash FROM paper_accounts").fetchone()[0]

    step = snapshot.symbol_rules["BTC/USDT"].step_size
    assert requested == pytest.approx(0.8)
    assert quantity == pytest.approx(0.8)
    assert requested == pytest.approx(quantity)
    assert status == "FILLED"
    assert semantics == "final-executable-v1"
    assert quantity / step == pytest.approx(round(quantity / step))
    assert cash >= 0.0
    assert system.store.reconcile().valid


def test_fully_fundable_order_persists_executable_quantity(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=100.0,
        min_quantity=0.1,
        min_notional=1.0,
    )

    with system.store.connect(read_only=True) as connection:
        requested, filled = connection.execute(
            "SELECT o.requested_quantity, f.filled_quantity FROM paper_orders o "
            "JOIN paper_fills f USING (order_id)"
        ).fetchone()

    assert requested == pytest.approx(1.0)
    assert requested == pytest.approx(filled)
    assert system.store.reconcile().valid


def test_sell_restriction_and_step_normalization_persist_executable_quantity(tmp_path):
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    system = PaperTradingSystem(
        tmp_path / "normalized-sell.duckdb",
        PaperConfig(
            assets=("BTC/USDT",),
            initial_cash=1_000.0,
            fee_rate=0.0,
            minimum_spread_rate=0.0,
            slippage_rate=0.0,
            require_exchange_rules=True,
        ),
    )
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?)",
            [system.config.account_id, "BTC/USDT", 0.85, 90.0, now],
        )
        connection.execute(
            "INSERT INTO position_ledger VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["seed-position", "seed-run", system.config.account_id, "BTC/USDT", 0.85, 0.85, now],
        )
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(
            {"BTC/USDT": [100.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-08-04T00:00:00Z")]),
        ),
        quotes={"BTC/USDT": Quote(100.0, 100.0, 100.0, now)},
        fetched_at=now,
        symbol_rules={
            "BTC/USDT": SymbolRules(True, 0.1, 10.0, 0.1, 1.0, 0.01)
        },
    )
    proposal = {
        "idempotency_key": "normalized-sell",
        "symbol": "BTC/USDT",
        "side": "SELL",
        "requested_quantity": 1.0,
        "target_weight": 0.0,
    }

    system._execute(
        run_id="normalized-sell-run",
        signal_timestamp=pd.Timestamp("2024-08-04T00:00:00Z"),
        proposals=[proposal],
        snapshot=snapshot,
        now=now,
    )

    with system.store.connect(read_only=True) as connection:
        requested, filled, position = connection.execute(
            "SELECT o.requested_quantity, f.filled_quantity, "
            "(SELECT quantity FROM paper_positions WHERE symbol='BTC/USDT') "
            "FROM paper_orders o JOIN paper_fills f USING (order_id)"
        ).fetchone()
    assert requested == pytest.approx(0.8)
    assert requested == pytest.approx(filled)
    assert position == pytest.approx(0.05)
    assert system.store.reconcile().valid


def test_reconciliation_rejects_current_ledger_quantity_mismatch(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_orders SET requested_quantity=1.0")

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert "quantity mismatch" in reconciliation.message.lower()


@pytest.mark.parametrize(
    ("table", "column", "delta"),
    [
        ("position_ledger", "quantity_delta", 0.01),
        ("cash_ledger", "amount", 1.0),
        ("paper_fills", "execution_price", 1.0),
        ("paper_fills", "fee", 1.0),
        ("paper_fills", "spread_cost", 1.0),
        ("paper_fills", "slippage_cost", 1.0),
    ],
)
def test_reconciliation_independently_rejects_fill_accounting_tampering(
    tmp_path, table, column, delta
):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute(f"UPDATE {table} SET {column}={column}+?", [delta])

    assert not system.store.reconcile().valid


def test_reconciliation_rejects_coordinated_bogus_protocol_provenance(tmp_path):
    system, _result = _execute_scaled_buy(
        tmp_path, initial_cash=83.0, min_quantity=0.1, min_notional=1.0
    )
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_orders SET execution_protocol_version='bogus-protocol'"
        )
        connection.execute(
            "UPDATE paper_fills SET execution_protocol_version='bogus-protocol'"
        )
        connection.execute(
            "UPDATE paper_execution_context SET execution_protocol_version='bogus-protocol'"
        )

    assert not system.store.reconcile().valid


def test_reconciliation_rejects_ledger_provenance_link_tampering(tmp_path):
    system, _result = _execute_scaled_buy(
        tmp_path, initial_cash=83.0, min_quantity=0.1, min_notional=1.0
    )
    with system.store.connect() as connection:
        connection.execute("UPDATE cash_ledger SET account_id='other-account'")

    assert not system.store.reconcile().valid


def test_reconciliation_recomputes_costs_instead_of_accepting_coordinated_tampering(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        fill_id, quantity, price = connection.execute(
            "SELECT fill_id, filled_quantity, execution_price FROM paper_fills"
        ).fetchone()
        changed_price = float(price) + 1.0
        changed_fee = float(quantity) * changed_price * system.config.fee_rate
        connection.execute(
            "UPDATE paper_fills SET execution_price=?, fee=?, slippage_cost=slippage_cost+?",
            [changed_price, changed_fee, float(quantity)],
        )
        connection.execute(
            "UPDATE cash_ledger SET amount=-(? * ? + ?) WHERE event_id=?",
            [float(quantity), changed_price, changed_fee, f"cash_{fill_id}"],
        )
        cash = connection.execute("SELECT SUM(amount) FROM cash_ledger").fetchone()[0]
        connection.execute("UPDATE paper_accounts SET cash=?", [cash])

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert "spread/slippage" in reconciliation.message.lower()


def test_reconciliation_rejects_unknown_ledger_semantics_marker(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_orders SET ledger_semantics_version='unsupported-v99'"
        )

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert "unsupported ledger semantics" in reconciliation.message.lower()


def test_reconciliation_rejects_missing_semantics_marker_for_new_order(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_orders SET ledger_semantics_version=NULL")

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert "missing ledger semantics" in reconciliation.message.lower()


@pytest.mark.parametrize(
    ("table", "column", "message"),
    [
        ("paper_fills", "filled_quantity", "invalid fill"),
        ("paper_orders", "requested_quantity", "invalid current order quantity"),
    ],
)
def test_reconciliation_rejects_nonfinite_current_quantities(
    tmp_path, table, column, message
):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", [float("nan")])

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert message in reconciliation.message.lower()


@pytest.mark.parametrize(
    "protocol",
    ["paper-exec-v2-ask-bid-utc0010", "paper-exec-v3-ask-bid-minspread-utc0010"],
)
def test_reconciliation_accepts_preserved_legacy_quantity_semantics(tmp_path, protocol):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_orders SET requested_quantity=1.0, "
            "ledger_semantics_version=NULL, execution_protocol_version=?",
            [protocol],
        )
        connection.execute(
            "UPDATE paper_fills SET execution_protocol_version=?",
            [protocol],
        )
        connection.execute("DELETE FROM paper_legacy_order_semantics")
        connection.execute("DELETE FROM paper_schema_versions WHERE version=6")

    system = PaperTradingSystem(system.store.path, system.config)

    assert system.store.reconcile().valid


def test_schema_v7_classifies_unmarked_orders_from_interim_v6(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_orders SET requested_quantity=1.0, "
            "ledger_semantics_version=NULL"
        )
        connection.execute("DELETE FROM paper_legacy_order_semantics")
        connection.execute("DELETE FROM paper_schema_versions WHERE version=7")

    system = PaperTradingSystem(system.store.path, system.config)

    with system.store.connect(read_only=True) as connection:
        preserved = connection.execute(
            "SELECT COUNT(*) FROM paper_legacy_order_semantics"
        ).fetchone()[0]
    assert preserved == 1
    assert system.store.reconcile().valid


def test_schema_v8_adds_rejected_order_diagnostics_without_rewriting_runs(tmp_path):
    path = tmp_path / "schema_v8.duckdb"
    system = PaperTradingSystem(path, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs VALUES "
            "('historical-run','2024-08-01T00:00:00Z','2024-08-01T00:01:00Z',"
            "'EXECUTED','PAPER',NULL,NULL,NULL,'historical','{}')"
        )
        connection.execute("ALTER TABLE paper_run_diagnostics DROP COLUMN rejected_orders")
        connection.execute("DELETE FROM paper_schema_versions WHERE version=8")

    migrated = PaperTradingSystem(path, _config())
    with migrated.store.connect(read_only=True) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('paper_run_diagnostics')"
            ).fetchall()
        }
        historical = connection.execute(
            "SELECT status, message FROM paper_runs WHERE run_id='historical-run'"
        ).fetchone()
        versions = connection.execute(
            "SELECT COUNT(*) FROM paper_schema_versions WHERE version=8"
        ).fetchone()[0]

    assert "rejected_orders" in columns
    assert historical == ("EXECUTED", "historical")
    assert versions == 1


def test_schema_v9_adds_persistent_rejection_audit_without_rewriting_runs(tmp_path):
    path = tmp_path / "schema-v9.duckdb"
    system = PaperTradingSystem(path, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs VALUES "
            "('historical-run','2024-08-01T00:00:00Z','2024-08-01T00:01:00Z',"
            "'EXECUTED','PAPER',NULL,NULL,NULL,'historical','{}')"
        )
        connection.execute("DROP TABLE paper_order_rejections")
        connection.execute("DELETE FROM paper_schema_versions WHERE version=9")

    migrated = PaperTradingSystem(path, _config())
    with migrated.store.connect(read_only=True) as connection:
        historical = connection.execute(
            "SELECT status, message FROM paper_runs WHERE run_id='historical-run'"
        ).fetchone()
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('paper_order_rejections')"
            ).fetchall()
        }
        description = connection.execute(
            "SELECT description FROM paper_schema_versions WHERE version=9"
        ).fetchone()[0]

    assert historical == ("EXECUTED", "historical")
    assert {"run_id", "symbol", "side", "stage", "reason", "notional"} <= columns
    assert description == "persist run-attributable paper order rejection audit trail"


def test_forward_evidence_migration_preserves_historical_runs(tmp_path):
    path = tmp_path / "migration_v10.duckdb"
    system = PaperTradingSystem(path, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs "
            "(run_id, started_at_utc, completed_at_utc, status, mode, message, reconciliation) "
            "VALUES ('historical-run', now(), now(), 'EXECUTED', 'PAPER', 'historical', '{}')"
        )
        connection.execute("DROP TABLE paper_forward_execution_evidence")
        connection.execute("DELETE FROM paper_schema_versions WHERE version=10")

    migrated = PaperTradingSystem(path, _config())
    with migrated.store.connect(read_only=True) as connection:
        historical = connection.execute(
            "SELECT status, message FROM paper_runs WHERE run_id='historical-run'"
        ).fetchone()
        description = connection.execute(
            "SELECT description FROM paper_schema_versions WHERE version=10"
        ).fetchone()[0]
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM paper_forward_execution_evidence"
        ).fetchone()[0]

    assert historical == ("EXECUTED", "historical")
    assert description == "atomically persist forward execution evidence"
    assert evidence_count == 0


def test_reconciliation_rejects_filled_order_without_fill(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute("DELETE FROM paper_fills")

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert "without fill" in reconciliation.message.lower()


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("symbol", "ETH/USDT", "symbol mismatch"),
        ("side", "SELL", "side mismatch"),
        ("run_id", "other-run", "run mismatch"),
        ("execution_protocol_version", "other-protocol", "protocol mismatch"),
    ],
)
def test_reconciliation_rejects_current_order_fill_provenance_mismatch(
    tmp_path, column, value, message
):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=83.0,
        min_quantity=0.1,
        min_notional=1.0,
    )
    with system.store.connect() as connection:
        connection.execute(f"UPDATE paper_fills SET {column}=?", [value])

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert message in reconciliation.message.lower()


def test_scaled_buy_below_minimum_quantity_is_rejected(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=15.0,
        min_quantity=0.2,
        min_notional=1.0,
    )

    with system.store.connect(read_only=True) as connection:
        orders, fills, cash = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), "
            "(SELECT COUNT(*) FROM paper_fills), "
            "(SELECT cash FROM paper_accounts)"
        ).fetchone()

    assert (orders, fills) == (0, 0)
    assert cash == pytest.approx(15.0)
    assert any(
        rejection["reason"] == "below_min_quantity"
        for rejection in system._last_rejections
    )


def test_run_message_counts_pre_execution_and_final_rejections(tmp_path, monkeypatch):
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    config = PaperConfig(
        assets=("BTC/USDT",),
        initial_cash=15.0,
        fee_rate=0.0,
        minimum_spread_rate=0.0,
        slippage_rate=0.0,
        schedule_hour=9,
        max_data_staleness_minutes=720,
        require_exchange_rules=True,
    )
    system = PaperTradingSystem(tmp_path / "rejections.duckdb", config)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    history = pd.date_range(end="2024-08-04T00:00:00Z", periods=200, freq="D")
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(
            {"BTC/USDT": [100.0] * len(history)},
            index=history,
        ),
        quotes={
            "BTC/USDT": Quote(100.0, 100.0, 100.0, now),
        },
        fetched_at=now,
        symbol_rules={
            "BTC/USDT": SymbolRules(True, 0.2, 10.0, 0.1, 1.0, 0.01),
        },
    )
    proposal = {
        "idempotency_key": "final-rejection",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "requested_quantity": 1.0,
        "target_weight": 1.0,
    }

    def proposals(_snapshot):
        system._last_rejections = [
            {
                "symbol": "OTHER/USDT",
                "side": "BUY",
                "reason": "below_min_notional",
                "notional": 0.5,
                "requested_quantity": 0.05,
                "target_weight": 0.5,
                "idempotency_key": "proposal-rejection",
            }
        ]
        return pd.Timestamp("2024-08-04T00:00:00Z"), [proposal]

    monkeypatch.setattr(system, "_proposals", proposals)

    result = system.run(
        snapshot,
        now=now.to_pydatetime(),
        dry_run=False,
        forward_diagnostics={"target_weights": {"BTC/USDT": 1.0}},
    )
    from src.paper_forward import finalize_forward_run

    finalize_forward_run(
        system,
        result,
        snapshot,
        now=now,
        diagnostics={"rejected_orders": [system._last_rejections[0]]},
    )
    with system.store.connect(read_only=True) as connection:
        rejection_rows = connection.execute(
            """
            SELECT symbol, side, stage, reason, notional,
                   requested_quantity, target_weight, idempotency_key
            FROM paper_order_rejections WHERE run_id=? ORDER BY rejection_index
            """,
            [result.run_id],
        ).fetchall()
        persisted_rejections = json.loads(
            connection.execute(
                "SELECT rejected_orders FROM paper_run_diagnostics WHERE run_id=?",
                [result.run_id],
            ).fetchone()[0]
        )
        execution_outcome = connection.execute(
            "SELECT execution_outcome FROM paper_execution_outcomes WHERE run_id=?",
            [result.run_id],
        ).fetchone()[0]
        target_deviation = json.loads(
            connection.execute(
                "SELECT target_deviation FROM paper_run_diagnostics WHERE run_id=?",
                [result.run_id],
            ).fetchone()[0]
        )

    assert "rejected 2" in result.message
    assert result.outcome == "EXECUTION_REJECTED"
    assert execution_outcome == "EXECUTION_REJECTED"
    assert rejection_rows[0][:5] == (
        "OTHER/USDT",
        "BUY",
        "PROPOSAL",
        "below_min_notional",
        0.5,
    )
    assert all(value is not None for value in rejection_rows[0][5:])
    assert rejection_rows[1] == (
        "BTC/USDT",
        "BUY",
        "FINAL",
        "below_min_quantity",
        15.0,
        1.0,
        1.0,
        "final-rejection",
    )
    assert [item["reason"] for item in persisted_rejections] == [
        "below_min_notional",
        "below_min_quantity",
    ]
    assert {item["reason"] for item in system._last_rejections} == {
        "below_min_notional",
        "below_min_quantity",
    }
    assert target_deviation["realized_weights"]["CASH"] == pytest.approx(1.0)
    assert target_deviation["l1_weight_error"] == pytest.approx(2.0)


def test_reconciliation_rejects_orphan_order_rejection_audit_rows(tmp_path):
    system = PaperTradingSystem(tmp_path / "orphan-rejection.duckdb", _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_order_rejections "
            "(run_id, rejection_index, symbol, side, stage, reason, notional, rejected_at_utc) "
            "VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
            [
                "missing-run",
                "BTC/USDT",
                "BUY",
                "FINAL",
                "below_min_notional",
                4.0,
                datetime.now(timezone.utc),
            ],
        )

    reconciliation = system.store.reconcile()

    assert not reconciliation.valid
    assert "orphan or invalid order rejection" in reconciliation.message.lower()


def test_scaled_buy_below_minimum_notional_is_rejected(tmp_path):
    system, _ = _execute_scaled_buy(
        tmp_path,
        initial_cash=35.0,
        min_quantity=0.1,
        min_notional=50.0,
    )

    with system.store.connect(read_only=True) as connection:
        orders, fills, cash = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), "
            "(SELECT COUNT(*) FROM paper_fills), "
            "(SELECT cash FROM paper_accounts)"
        ).fetchone()

    assert (orders, fills) == (0, 0)
    assert cash == pytest.approx(35.0)
    assert any(
        rejection["reason"] == "below_min_notional"
        for rejection in system._last_rejections
    )


@pytest.mark.parametrize(
    "rules",
    [
        SymbolRules(True, float("nan"), 10.0, 0.1, 1.0, 0.01),
        SymbolRules(True, 0.0, 10.0, 0.1, 1.0, 0.01),
        SymbolRules(True, 0.1, float("nan"), 0.1, 1.0, 0.01),
        SymbolRules(True, 0.1, 10.0, 0.1, float("nan"), 0.01),
        SymbolRules(True, 0.1, 10.0, 0.1, 0.0, 0.01),
        SymbolRules(True, -0.1, 10.0, 0.1, 1.0, 0.01),
        SymbolRules(True, 0.2, 0.1, 0.1, 1.0, 0.01),
        SymbolRules(False, 0.1, 10.0, 0.1, 1.0, 0.01),
    ],
)
def test_malformed_exchange_rules_fail_closed_before_fill(tmp_path, rules):
    system = PaperTradingSystem(
        tmp_path / "invalid-rules.duckdb",
        PaperConfig(assets=("BTC/USDT",), require_exchange_rules=True),
    )
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(
            {"BTC/USDT": [100.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-08-04T00:00:00Z")]),
        ),
        quotes={"BTC/USDT": Quote(100.0, 100.0, 100.0, now)},
        fetched_at=now,
        symbol_rules={"BTC/USDT": rules},
    )

    quantity, reason = system._normalize_exchange_quantity(
        symbol="BTC/USDT",
        quantity=1.0,
        validation_price=100.0,
        snapshot=snapshot,
    )

    assert quantity is None
    assert reason in {"inactive_symbol", "invalid_exchange_rules"}


def test_execution_message_counts_only_persisted_orders(monkeypatch, tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    snapshot = MarketSnapshot(
        closes=snapshot.closes,
        quotes=snapshot.quotes,
        fetched_at=snapshot.fetched_at,
        symbol_rules={
            asset: SymbolRules(True, 0.2, 10.0, 0.1, 1.0, 0.01)
            for asset in ASSETS
        },
    )
    config = PaperConfig(
        assets=tuple(ASSETS),
        initial_cash=15.0,
        fee_rate=0.0,
        minimum_spread_rate=0.0,
        slippage_rate=0.0,
        schedule_hour=9,
        schedule_minute=5,
        schedule_window_minutes=30,
        max_data_staleness_minutes=720,
        require_exchange_rules=True,
    )
    system = PaperTradingSystem(tmp_path / "rejected-message.duckdb", config)
    proposal = {
        "idempotency_key": "message-rejection",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "requested_quantity": 1.0,
        "target_weight": 1.0,
    }
    monkeypatch.setattr(
        system,
        "_proposals",
        lambda _snapshot: (pd.Timestamp("2024-08-04T00:00:00Z"), [proposal]),
    )

    result = system.run(snapshot, now=now, dry_run=False)

    assert result.status == "EXECUTED"
    assert "Executed 0" in result.message
    assert "rejected 1" in result.message


def test_floating_point_scaling_never_persists_negative_cash(tmp_path):
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    initial_cash = 763_901.6550981523
    price = 2.1628355930693526
    system = PaperTradingSystem(
        tmp_path / "floating-cash.duckdb",
        PaperConfig(
            assets=("BTC/USDT",),
            initial_cash=initial_cash,
            fee_rate=0.003,
            minimum_spread_rate=0.0,
            slippage_rate=0.0,
            require_exchange_rules=True,
        ),
    )
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(
            {"BTC/USDT": [price]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-08-04T00:00:00Z")]),
        ),
        quotes={"BTC/USDT": Quote(price, price, price, now)},
        fetched_at=now,
        symbol_rules={
            "BTC/USDT": SymbolRules(True, 1e-10, 1e9, 1e-10, 1e-10, 1e-10)
        },
    )
    proposal = {
        "idempotency_key": "floating-cash",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "requested_quantity": 686_284.7789865133,
        "target_weight": 1.0,
    }

    system._execute(
        run_id="floating-cash-run",
        signal_timestamp=pd.Timestamp("2024-08-04T00:00:00Z"),
        proposals=[proposal],
        snapshot=snapshot,
        now=now,
    )

    with system.store.connect(read_only=True) as connection:
        minimum_ledger_cash = connection.execute(
            "SELECT MIN(balance_after) FROM cash_ledger"
        ).fetchone()[0]
        account_cash = connection.execute("SELECT cash FROM paper_accounts").fetchone()[0]
    assert minimum_ledger_cash >= 0.0
    assert account_cash >= 0.0


def test_duplicate_scheduled_run_cannot_create_duplicate_orders_or_fills(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())

    first = system.run(_snapshot(now), now=now, dry_run=False)
    with duckdb.connect(str(database), read_only=True) as connection:
        counts_after_first = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
        protocols = connection.execute(
            "SELECT DISTINCT execution_protocol_version FROM paper_fills"
        ).fetchall()
        context_count = connection.execute(
            "SELECT COUNT(*) FROM paper_execution_context"
        ).fetchone()[0]
    second = system.run(_snapshot(now), now=now, dry_run=False)
    with duckdb.connect(str(database), read_only=True) as connection:
        counts_after_second = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
        minimum_cash = connection.execute("SELECT cash FROM paper_accounts").fetchone()[0]

    assert first.status == "EXECUTED"
    assert counts_after_first[0] > 0
    assert protocols == [("paper-exec-v3-ask-bid-minspread-utc0010",)]
    assert context_count == counts_after_first[0]
    assert counts_after_first == counts_after_second
    assert second.status == "DUPLICATE_SCHEDULE"
    assert minimum_cash >= -1e-9


def test_duplicate_schedule_rejects_before_proposal_generation(tmp_path, monkeypatch):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "paper.duckdb", _config())
    assert system.run(_snapshot(now), now=now, dry_run=False).status == "EXECUTED"
    monkeypatch.setattr(
        system,
        "_proposals",
        lambda _snapshot: (_ for _ in ()).throw(AssertionError("proposal work ran")),
    )

    result = system.run(_snapshot(now), now=now, dry_run=False)

    assert result.status == "DUPLICATE_SCHEDULE"


def test_quote_timestamp_after_execution_time_fails_closed(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    original = snapshot.quotes["BTC/USDT"]
    snapshot.quotes["BTC/USDT"] = Quote(
        bid=original.bid,
        ask=original.ask,
        last=original.last,
        timestamp=pd.Timestamp(now) + pd.Timedelta(milliseconds=1),
    )
    system = PaperTradingSystem(tmp_path / "paper.duckdb", _config())

    result = system.run(snapshot, now=now, dry_run=True)

    assert result.status == "DATA_HALT"
    assert "future quote timestamp" in result.message
    assert system.store.account()["status"] == "ACTIVE"


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (
            Quote(
                ask=99.0,
                bid=100.0,
                last=99.5,
                timestamp=pd.Timestamp("2024-08-05T09:10:00Z"),
            ),
            "malformed quote",
        ),
        (
            Quote(
                ask=101.0,
                bid=99.0,
                last=100.0,
                timestamp=pd.Timestamp("2024-08-05T09:00:00Z"),
            ),
            "Stale data",
        ),
    ],
)
def test_invalid_executable_quote_evidence_causes_retryable_data_halt(tmp_path, quote, expected):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    snapshot = _snapshot(now)
    snapshot.quotes["BTC/USDT"] = quote
    system = PaperTradingSystem(tmp_path / f"{expected}.duckdb", _config())

    result = system.run(snapshot, now=now, dry_run=False)

    assert result.status == "DATA_HALT"
    assert expected in result.message
    assert system.store.account()["status"] == "ACTIVE"


def test_corrupted_persistent_cash_state_activates_kill_switch_before_orders(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with duckdb.connect(str(database)) as connection:
        connection.execute("UPDATE paper_accounts SET cash=1999.0")

    result = system.run(_snapshot(now), now=now, dry_run=False)

    with duckdb.connect(str(database), read_only=True) as connection:
        status, order_count, incident_count = connection.execute(
            "SELECT (SELECT status FROM paper_accounts), "
            "(SELECT COUNT(*) FROM paper_orders), "
            "(SELECT COUNT(*) FROM paper_incidents)"
        ).fetchone()
    assert result.status == "KILL_SWITCH"
    assert status == "HALTED"
    assert order_count == 0
    assert incident_count == 1


def test_dry_run_generates_proposals_without_persisting_trades(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())

    result = system.run(_snapshot(now), now=now, dry_run=True)

    with duckdb.connect(str(database), read_only=True) as connection:
        orders, fills, cash = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), "
            "(SELECT COUNT(*) FROM paper_fills), (SELECT cash FROM paper_accounts)"
        ).fetchone()
    assert result.status == "DRY_RUN"
    assert result.proposed_orders
    assert (orders, fills, cash) == (0, 0, 2_000.0)


def test_scheduled_dry_run_does_not_consume_real_paper_window(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )

    dry = system.run(_snapshot(now), now=now, dry_run=True)
    from src.paper_forward import build_forward_diagnostics, finalize_forward_run

    finalize_forward_run(
        system,
        dry,
        _snapshot(now),
        now=now,
        diagnostics=build_forward_diagnostics(system, _snapshot(now)),
    )
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM forward_schedule_windows").fetchone()[0] == 0
    paper = system.run(_snapshot(now), now=now, dry_run=False)

    assert dry.status == "DRY_RUN"
    assert paper.status == "EXECUTED"
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] > 0


def test_stale_market_data_causes_retryable_data_halt(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "paper.duckdb", _config())

    result = system.run(_snapshot(now, stale_days=2), now=now, dry_run=False)

    assert result.status == "DATA_HALT"
    assert "stale" in result.message.lower()
    assert system.store.account()["status"] == "ACTIVE"


def test_data_halt_does_not_persist_synthetic_forward_observation_or_baseline(tmp_path):
    from src.paper_forward import finalize_forward_run

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "data_halt.duckdb", _config())
    snapshot = _snapshot(now, stale_days=2)
    result = system.run(snapshot, now=now, dry_run=False)

    finalized = finalize_forward_run(system, result, snapshot, now=now, diagnostics={})

    assert finalized.outcome == "DATA_QUALITY_FAILURE"
    assert system.store.committed_forward_evidence(result.run_id) is None
    with system.store.connect(read_only=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    assert "forward_observations" not in tables
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM forward_baselines").fetchone()[0] == 0


def test_restart_recovers_abandoned_running_record(tmp_path):
    database = tmp_path / "paper.duckdb"
    PaperTradingSystem(database, _config())
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "INSERT INTO paper_runs "
            "(run_id, started_at_utc, status, mode, schedule_key) "
            "VALUES ('abandoned', now(), 'RUNNING', 'PAPER', '2024-08-05T09:05Z')"
        )

    PaperTradingSystem(database, _config())

    with duckdb.connect(str(database), read_only=True) as connection:
        status, schedule_key = connection.execute(
            "SELECT status, schedule_key FROM paper_runs WHERE run_id='abandoned'"
        ).fetchone()
    assert status == "RECOVERED_ABORTED"
    assert schedule_key is None


def test_restart_preserves_schedule_for_committed_zero_fill_equity(tmp_path):
    database = tmp_path / "zero_fill_crash.duckdb"
    system = PaperTradingSystem(database, _config())
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    schedule_key = system._scheduled_key(pd.Timestamp(now))
    system.store.insert_run(
        run_id="zero-fill-committed",
        started_at=now,
        mode="PAPER",
        schedule_key=schedule_key,
        signal_timestamp=now,
        data_timestamp=now,
    )
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO equity_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                "equity-zero-fill",
                "zero-fill-committed",
                system.config.account_id,
                2_000.0,
                0.0,
                2_000.0,
                now,
            ],
        )

    PaperTradingSystem(database, _config())

    with duckdb.connect(str(database), read_only=True) as connection:
        status, persisted_schedule = connection.execute(
            "SELECT status, schedule_key FROM paper_runs WHERE run_id='zero-fill-committed'"
        ).fetchone()
    assert status == "RECOVERED_COMMITTED"
    assert persisted_schedule == schedule_key


def test_outside_schedule_health_check_persists_equity_without_trade(tmp_path):
    now = datetime(2024, 8, 5, 9, 40, tzinfo=timezone.utc)
    config = PaperConfig(
        assets=tuple(ASSETS),
        initial_cash=2_000.0,
        schedule_hour=9,
        schedule_minute=5,
        schedule_window_minutes=30,
        max_data_staleness_minutes=5_000,
    )
    system = PaperTradingSystem(tmp_path / "outside.duckdb", config)

    result = system.run(_snapshot(now), now=now, dry_run=False)

    with system.store.connect(read_only=True) as connection:
        orders, fills, equity = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), "
            "(SELECT COUNT(*) FROM paper_fills), "
            "(SELECT COUNT(*) FROM equity_snapshots WHERE run_id=?)",
            [result.run_id],
        ).fetchone()
        outcome = connection.execute(
            "SELECT execution_outcome FROM paper_execution_outcomes WHERE run_id=?",
            [result.run_id],
        ).fetchone()[0]
    assert result.status == "NO_REBALANCE", result.message
    assert result.outcome == "NO_REBALANCE_REQUIRED"
    assert outcome == "NO_REBALANCE_REQUIRED"
    assert (orders, fills, equity) == (0, 0, 1)


def test_snapshot_validation_reports_structural_market_data_failures(tmp_path):
    now = pd.Timestamp("2024-08-05T09:10:00Z")
    system = PaperTradingSystem(tmp_path / "validation.duckdb", _config())
    snapshot = _snapshot(now.to_pydatetime())
    closes = snapshot.closes

    cases = [
        (closes.copy().set_axis(closes.index.tz_localize(None)), "not timezone-aware"),
        (pd.concat([closes, closes.iloc[[-1]]]), "duplicate close timestamps"),
        (closes.iloc[::-1], "not strictly increasing"),
        (closes.drop(columns=closes.columns[-1]), "asset columns"),
        (closes.iloc[-20:], "requires at least"),
    ]
    for invalid_closes, expected in cases:
        message = system._validate_snapshot(replace(snapshot, closes=invalid_closes), now)
        assert message is not None
        assert expected in message


def test_operational_failure_is_terminal_idempotent_schedule_evidence(tmp_path):
    from src.paper_forward import commit_operational_failure

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "failure.duckdb", _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )

    first = commit_operational_failure(
        system,
        outcome="DATA_QUALITY_FAILURE",
        message="public market unavailable",
        now=now,
    )
    second = commit_operational_failure(
        system,
        outcome="DATA_QUALITY_FAILURE",
        message="must not replace evidence",
        now=now,
    )

    with system.store.connect(read_only=True) as connection:
        run_count, window_count, incident_count = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_runs), "
            "(SELECT COUNT(*) FROM forward_schedule_windows), "
            "(SELECT COUNT(*) FROM forward_incidents)"
        ).fetchone()
    assert first.status == "DATA_QUALITY_FAILURE"
    assert second.status == "DUPLICATE_SCHEDULE"
    assert (run_count, window_count, incident_count) == (1, 1, 1)
    assert system.store.account()["status"] == "ACTIVE"


@pytest.mark.parametrize(
    ("status", "message", "diagnostics", "expected"),
    [
        ("KILL_SWITCH", "persistent state mismatch", {}, "RECONCILIATION_FAILURE"),
        ("KILL_SWITCH", "stale quote data", {}, "DATA_QUALITY_FAILURE"),
        ("DATA_HALT", "stale quote data", {}, "DATA_QUALITY_FAILURE"),
        ("KILL_SWITCH", "operator halt", {}, "KILL_SWITCH_ACTIVATED"),
        ("DUPLICATE_SCHEDULE", "duplicate", {}, "NO_REBALANCE"),
        ("DRY_RUN", "proposal", {"proposed_orders": [{}]}, "NO_ELIGIBLE_ASSET"),
        ("DRY_RUN", "cash", {}, "CASH_ONLY"),
        ("UNKNOWN", "failure", {}, "EXECUTION_ERROR"),
    ],
)
def test_forward_outcome_classification_covers_operational_terminal_states(
    status, message, diagnostics, expected
):
    from src.paper_forward import classify_outcome

    result = PaperRunResult("run", status, message)
    assert classify_outcome(SimpleNamespace(), result, diagnostics) == expected


def test_restart_recovers_committed_run_without_replaying_fills(tmp_path, monkeypatch):
    from src.paper_forward import build_forward_diagnostics, recover_committed_forward_evidence

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    original_finish = system.store.finish_run
    monkeypatch.setattr(
        system.store,
        "finish_run",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("crash after commit")),
    )

    snapshot = _snapshot(now)
    diagnostics = build_forward_diagnostics(system, snapshot)
    with pytest.raises(RuntimeError, match="crash after commit"):
        system.run(
            snapshot,
            now=now,
            dry_run=False,
            forward_diagnostics=diagnostics,
        )

    with duckdb.connect(str(database), read_only=True) as connection:
        run_id = connection.execute("SELECT run_id FROM paper_runs").fetchone()[0]
        fill_ids_before = connection.execute(
            "SELECT fill_id FROM paper_fills ORDER BY fill_id"
        ).fetchall()
        equity_count_before = connection.execute(
            "SELECT COUNT(*) FROM equity_snapshots WHERE run_id=?", [run_id]
        ).fetchone()[0]
    monkeypatch.setattr(system.store, "finish_run", original_finish)

    restarted = PaperTradingSystem(database, _config())
    recovered = recover_committed_forward_evidence(restarted, now=now)
    recovered_again = recover_committed_forward_evidence(restarted, now=now)

    with duckdb.connect(str(database), read_only=True) as connection:
        status = connection.execute(
            "SELECT status FROM paper_runs WHERE run_id=?", [run_id]
        ).fetchone()[0]
        fill_ids_after = connection.execute(
            "SELECT fill_id FROM paper_fills ORDER BY fill_id"
        ).fetchall()
        equity_count_after = connection.execute(
            "SELECT COUNT(*) FROM equity_snapshots WHERE run_id=?", [run_id]
        ).fetchone()[0]
        outcome = connection.execute(
            "SELECT outcome FROM paper_run_diagnostics WHERE run_id=?", [run_id]
        ).fetchone()[0]
        execution_outcome = connection.execute(
            "SELECT execution_outcome FROM paper_execution_outcomes WHERE run_id=?",
            [run_id],
        ).fetchone()[0]
        window = connection.execute(
            "SELECT run_id, outcome FROM forward_schedule_windows"
        ).fetchone()
        observations = connection.execute(
            "SELECT COUNT(*) FROM forward_market_observations WHERE run_id=?", [run_id]
        ).fetchone()[0]
        incomplete_incidents = connection.execute(
            "SELECT COUNT(*) FROM forward_incidents "
            "WHERE run_id=? AND incident_type='RECOVERED_COMMITTED_INCOMPLETE_EVIDENCE'",
            [run_id],
        ).fetchone()[0]
    assert recovered == 1
    assert recovered_again == 0
    assert status == "RECOVERED_COMMITTED"
    assert fill_ids_after == fill_ids_before
    assert equity_count_before == equity_count_after == 1
    assert outcome == "PAPER_TRADE_COMPLETED"
    assert execution_outcome == "FULL_EXECUTION"
    assert window == (run_id, "PAPER_TRADE_COMPLETED")
    assert observations == len(system.config.assets)
    assert incomplete_incidents == 0


def test_forward_evidence_failure_rolls_back_with_paper_transaction(tmp_path, monkeypatch):
    from src.paper_forward import build_forward_diagnostics

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "evidence_rollback.duckdb"
    system = PaperTradingSystem(database, _config())
    snapshot = _snapshot(now)
    diagnostics = build_forward_diagnostics(system, snapshot)
    original = system.store.record_committed_forward_evidence

    def interrupt_after_evidence(connection, **kwargs):
        original(connection, **kwargs)
        raise RuntimeError("crash before transaction commit")

    monkeypatch.setattr(
        system.store, "record_committed_forward_evidence", interrupt_after_evidence
    )

    with pytest.raises(RuntimeError, match="crash before transaction commit"):
        system.run(
            snapshot,
            now=now,
            dry_run=False,
            forward_diagnostics=diagnostics,
        )

    restarted = PaperTradingSystem(database, _config())
    with restarted.store.connect(read_only=True) as connection:
        counts = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM paper_orders), "
            "(SELECT COUNT(*) FROM paper_fills), "
            "(SELECT COUNT(*) FROM equity_snapshots), "
            "(SELECT COUNT(*) FROM paper_forward_execution_evidence), "
            "(SELECT COUNT(*) FROM forward_market_observations)"
        ).fetchone()
        run_state = connection.execute(
            "SELECT status, schedule_key FROM paper_runs"
        ).fetchone()
    assert counts == (0, 0, 0, 0, 0)
    assert run_state == ("RECOVERED_ABORTED", None)


def test_recovers_terminal_run_missing_post_commit_evidence(tmp_path):
    from src.paper_forward import recover_committed_forward_evidence

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    result = system.run(_snapshot(now), now=now, dry_run=False)

    assert recover_committed_forward_evidence(system, now=now) == 1
    assert recover_committed_forward_evidence(system, now=now) == 0
    with duckdb.connect(str(database), read_only=True) as connection:
        outcome, window_run = connection.execute(
            "SELECT d.outcome, w.run_id FROM paper_run_diagnostics d "
            "JOIN forward_schedule_windows w ON w.run_id=d.run_id WHERE d.run_id=?",
            [result.run_id],
        ).fetchone()
        baseline_count = connection.execute(
            "SELECT COUNT(*) FROM forward_baselines WHERE run_id=?",
            [result.run_id],
        ).fetchone()[0]
    assert outcome == "RECOVERED_COMMITTED_INCOMPLETE_EVIDENCE"
    assert window_run == result.run_id
    assert baseline_count == 1
    recovery_kwargs = {
        "reports_dir": tmp_path / "reports",
        "notification_target": "telegram:test",
    }
    assert recover_committed_forward_evidence(system, now=now, **recovery_kwargs) == 1
    assert recover_committed_forward_evidence(system, now=now, **recovery_kwargs) == 0
    with system.store.connect(read_only=True) as connection:
        notification_status = connection.execute(
            "SELECT status FROM paper_notifications WHERE run_id=?",
            [result.run_id],
        ).fetchone()[0]
    assert notification_status == "PENDING"


def test_recovers_post_finalization_crash_as_incomplete_delivery_incident(tmp_path):
    from src.paper_forward import finalize_forward_run, recover_committed_forward_evidence

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    database = tmp_path / "delivery_crash.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    snapshot = _snapshot(now)
    result = system.run(snapshot, now=now, dry_run=False)
    finalize_forward_run(system, result, snapshot, now=now, diagnostics={})

    recovery_kwargs = {
        "reports_dir": tmp_path / "reports",
        "notification_target": "telegram:test",
    }
    assert recover_committed_forward_evidence(system, now=now) == 1
    assert recover_committed_forward_evidence(system, now=now) == 0
    assert recover_committed_forward_evidence(system, now=now, **recovery_kwargs) == 1
    assert recover_committed_forward_evidence(system, now=now, **recovery_kwargs) == 0
    with system.store.connect(read_only=True) as connection:
        incident_types = connection.execute(
            "SELECT incident_type FROM forward_incidents WHERE run_id=?",
            [result.run_id],
        ).fetchall()
        notification = connection.execute(
            "SELECT status, report_path, attempt_count FROM paper_notifications WHERE run_id=?",
            [result.run_id],
        ).fetchone()
    assert incident_types == [("RECOVERED_COMMITTED_INCOMPLETE_DELIVERY",)]
    assert notification[0] == "PENDING"
    assert Path(notification[1]).is_file()
    assert notification[2] == 0


def test_delivery_recovery_retries_after_report_creation_crash(tmp_path, monkeypatch):
    import src.paper_forward as paper_forward

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "report_crash.duckdb", _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    snapshot = _snapshot(now)
    result = system.run(snapshot, now=now, dry_run=False)
    real_writer = paper_forward.write_recovered_committed_report

    def crash_before_report(*_args, **_kwargs):
        raise RuntimeError("simulated report interruption")

    monkeypatch.setattr(paper_forward, "write_recovered_committed_report", crash_before_report)
    recovery_kwargs = {
        "reports_dir": tmp_path / "reports",
        "notification_target": "telegram:test",
    }
    with pytest.raises(RuntimeError, match="simulated report interruption"):
        paper_forward.recover_committed_forward_evidence(
            system, now=now, **recovery_kwargs
        )

    monkeypatch.setattr(paper_forward, "write_recovered_committed_report", real_writer)
    assert (
        paper_forward.recover_committed_forward_evidence(
            system, now=now, **recovery_kwargs
        )
        == 1
    )
    assert (
        paper_forward.recover_committed_forward_evidence(
            system, now=now, **recovery_kwargs
        )
        == 0
    )
    with system.store.connect(read_only=True) as connection:
        notification = connection.execute(
            "SELECT status, report_path FROM paper_notifications WHERE run_id=?",
            [result.run_id],
        ).fetchone()
    assert notification[0] == "PENDING"
    assert Path(notification[1]).is_file()


def test_delivery_recovery_reuses_report_after_pending_registration_crash(
    tmp_path, monkeypatch
):
    import src.paper_forward as paper_forward

    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "pending_crash.duckdb", _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2024-08-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    snapshot = _snapshot(now)
    result = system.run(snapshot, now=now, dry_run=False)
    paper_forward.finalize_forward_run(system, result, snapshot, now=now, diagnostics={})
    real_register = paper_forward.NotificationService.register_pending

    def crash_before_pending(*_args, **_kwargs):
        raise RuntimeError("simulated pending interruption")

    monkeypatch.setattr(
        paper_forward.NotificationService, "register_pending", crash_before_pending
    )
    recovery_kwargs = {
        "reports_dir": tmp_path / "reports",
        "notification_target": "telegram:test",
    }
    with pytest.raises(RuntimeError, match="simulated pending interruption"):
        paper_forward.recover_committed_forward_evidence(
            system, now=now, **recovery_kwargs
        )

    reports = list((tmp_path / "reports").glob("paper_recovered_*.md"))
    assert len(reports) == 1
    monkeypatch.setattr(
        paper_forward.NotificationService, "register_pending", real_register
    )
    assert (
        paper_forward.recover_committed_forward_evidence(
            system, now=now, **recovery_kwargs
        )
        == 1
    )
    assert list((tmp_path / "reports").glob("paper_recovered_*.md")) == reports
    with system.store.connect(read_only=True) as connection:
        status = connection.execute(
            "SELECT status FROM paper_notifications WHERE run_id=?",
            [result.run_id],
        ).fetchone()[0]
    assert status == "PENDING"
