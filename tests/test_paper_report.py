from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem, Quote
from src.paper_report import write_weekly_paper_report


def test_weekly_paper_report_is_concise_and_discloses_virtual_only_mode(tmp_path):
    now = datetime(2024, 8, 6, 0, 10, tzinfo=timezone.utc)
    dates = pd.date_range(end="2024-08-04", periods=160, freq="D", tz="UTC")
    closes = pd.DataFrame(
        {"BTC/USDT": range(100, 260), "ETH/USDT": range(200, 360)}, index=dates
    )
    quotes = {
        asset: Quote(99.9, 100.1, 100.0, pd.Timestamp(now)) for asset in closes.columns
    }
    snapshot = MarketSnapshot(closes, quotes, pd.Timestamp(now))
    config = PaperConfig(assets=tuple(closes.columns))
    system = PaperTradingSystem(tmp_path / "paper.duckdb", config)
    result = system.run(snapshot, now=now, dry_run=True)
    result = replace(
        result,
        outcome="NO_REBALANCE",
        diagnostics={
            "regime": "RISK_ON",
            "btc_vs_trend": 0.072,
            "trend_window": 150,
            "momentum": {"BTC/USDT": 0.143, "ETH/USDT": 0.081},
            "eligibility": {"BTC/USDT": True, "ETH/USDT": True},
            "selected_assets": ["BTC/USDT", "ETH/USDT"],
            "current_weights": {"BTC/USDT": 0.0, "ETH/USDT": 0.0, "CASH": 1.0},
            "target_weights": {"BTC/USDT": 0.5, "ETH/USDT": 0.5, "CASH": 0.0},
            "proposed_orders": [],
            "turnover": 1.0,
        },
    )
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_order_rejections VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
            [
                result.run_id,
                "ETH/USDT",
                "BUY",
                "FINAL",
                "below_min_notional",
                4.5,
                now,
            ],
        )

    path = write_weekly_paper_report(
        system.store, result, snapshot, tmp_path / "reports", now=pd.Timestamp(now)
    )

    text = path.read_text(encoding="utf-8")
    assert "No real exchange orders" in text
    assert "2,000.00 USDT" in text
    assert result.status in text
    assert "Europe/Rome" in text
    assert "mw120_sw00_ma150_n2_r07_v30" in text
    assert "MA150" in text
    assert "RISK_ON" in text
    assert "Maximum drawdown" in text
    assert "Reconciliation" in text
    assert "below_min_notional" in text
    assert "FINAL" in text
