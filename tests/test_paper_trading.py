from dataclasses import replace
from datetime import datetime, timezone

import duckdb
import numpy as np
import pandas as pd
import pytest

from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem, Quote


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
        schedule_hour=0,
        schedule_minute=5,
        schedule_window_minutes=30,
        max_data_staleness_minutes=120,
        max_quote_staleness_minutes=5,
    )


def test_duplicate_scheduled_run_cannot_create_duplicate_orders_or_fills(tmp_path):
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())

    first = system.run(_snapshot(now), now=now, dry_run=False)
    with duckdb.connect(str(database), read_only=True) as connection:
        counts_after_first = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
    second = system.run(_snapshot(now), now=now, dry_run=False)
    with duckdb.connect(str(database), read_only=True) as connection:
        counts_after_second = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills)"
        ).fetchone()
        minimum_cash = connection.execute("SELECT cash FROM paper_accounts").fetchone()[0]

    assert first.status == "EXECUTED"
    assert counts_after_first[0] > 0
    assert counts_after_first == counts_after_second
    assert second.status == "DUPLICATE_SCHEDULE"
    assert minimum_cash >= -1e-9


def test_corrupted_persistent_cash_state_activates_kill_switch_before_orders(tmp_path):
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
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
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
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


def test_stale_market_data_activates_kill_switch(tmp_path):
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    system = PaperTradingSystem(tmp_path / "paper.duckdb", _config())

    result = system.run(_snapshot(now, stale_days=2), now=now, dry_run=False)

    assert result.status == "KILL_SWITCH"
    assert "stale" in result.message.lower()


def test_restart_recovers_abandoned_running_record(tmp_path):
    database = tmp_path / "paper.duckdb"
    PaperTradingSystem(database, _config())
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "INSERT INTO paper_runs "
            "(run_id, started_at_utc, status, mode, schedule_key) "
            "VALUES ('abandoned', now(), 'RUNNING', 'PAPER', '2024-08-05T00:05Z')"
        )

    PaperTradingSystem(database, _config())

    with duckdb.connect(str(database), read_only=True) as connection:
        status, schedule_key = connection.execute(
            "SELECT status, schedule_key FROM paper_runs WHERE run_id='abandoned'"
        ).fetchone()
    assert status == "RECOVERED_ABORTED"
    assert schedule_key is None
