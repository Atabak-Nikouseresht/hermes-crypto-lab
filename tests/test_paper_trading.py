from datetime import datetime, timezone

import duckdb
import numpy as np
import pandas as pd

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
        schedule_hour=9,
        schedule_minute=5,
        schedule_window_minutes=30,
        max_data_staleness_minutes=720,
        max_quote_staleness_minutes=5,
    )


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

    assert result.status == "KILL_SWITCH"
    assert "future quote timestamp" in result.message


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


def test_stale_market_data_activates_kill_switch(tmp_path):
    now = datetime(2024, 8, 5, 9, 10, tzinfo=timezone.utc)
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
            "VALUES ('abandoned', now(), 'RUNNING', 'PAPER', '2024-08-05T09:05Z')"
        )

    PaperTradingSystem(database, _config())

    with duckdb.connect(str(database), read_only=True) as connection:
        status, schedule_key = connection.execute(
            "SELECT status, schedule_key FROM paper_runs WHERE run_id='abandoned'"
        ).fetchone()
    assert status == "RECOVERED_ABORTED"
    assert schedule_key is None
