from datetime import datetime, timezone

import pandas as pd
import pytest

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION, describe_execution_timing
from src.paper_broker import PaperConfig, PaperTradingSystem, Quote


def test_forward_protocol_uses_observed_ask_for_buys_and_bid_for_sells(tmp_path):
    config = PaperConfig(
        assets=("BTC/USDT",),
        slippage_rate=0.0005,
        schedule_hour=0,
        schedule_minute=5,
        execution_target_minute=10,
        schedule_window_minutes=15,
    )
    system = PaperTradingSystem(tmp_path / "paper.duckdb", config)
    quote = Quote(
        bid=99.0,
        ask=101.0,
        last=100.0,
        timestamp=pd.Timestamp("2026-08-24T00:10:00Z"),
    )

    buy = system._execution_terms(quote, "BUY")
    sell = system._execution_terms(quote, "SELL")

    assert buy["base_executable_price"] == 101.0
    assert buy["execution_price"] == pytest.approx(101.0 * 1.0005)
    assert sell["base_executable_price"] == 99.0
    assert sell["execution_price"] == pytest.approx(99.0 * 0.9995)
    assert buy["full_spread"] == 2.0
    assert buy["execution_protocol_version"] == EXECUTION_PROTOCOL_VERSION


def test_forward_protocol_enforces_minimum_adverse_spread_per_side(tmp_path):
    config = PaperConfig(
        assets=("BTC/USDT",),
        minimum_spread_rate=0.0002,
        slippage_rate=0.0005,
    )
    system = PaperTradingSystem(tmp_path / "paper.duckdb", config)
    quote = Quote(
        bid=99.999,
        ask=100.001,
        last=100.0,
        timestamp=pd.Timestamp("2026-08-24T00:10:00Z"),
    )

    buy = system._execution_terms(quote, "BUY")
    sell = system._execution_terms(quote, "SELL")

    assert buy["base_executable_price"] == pytest.approx(100.02)
    assert sell["base_executable_price"] == pytest.approx(99.98)
    assert buy["minimum_spread_applied"] is True
    assert sell["minimum_spread_applied"] is True


def test_execution_timing_audit_explicitly_labels_backtest_paper_mismatch():
    audit = describe_execution_timing(
        signal_bar_open=pd.Timestamp("2024-01-07T00:00:00Z"),
        backtest_fill_bar_open=pd.Timestamp("2024-01-08T00:00:00Z"),
        paper_execution=pd.Timestamp("2024-01-08T00:10:00Z"),
    )

    assert audit["signal_available_at_utc"] == "2024-01-08T00:00:00+00:00"
    assert audit["backtest_price_field"] == "next daily close"
    assert audit["backtest_price_available_at_utc"] == "2024-01-09T00:00:00+00:00"
    assert audit["paper_protocol"] == EXECUTION_PROTOCOL_VERSION
    assert audit["classification"] == "EXECUTION_MODEL_MISMATCH"
    assert audit["lookahead_violation"] is False
