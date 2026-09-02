"""Versioned execution protocol and explicit timing-audit semantics."""

from __future__ import annotations

import pandas as pd

EXECUTION_PROTOCOL_VERSION = "paper-exec-v3-ask-bid-minspread-utc0010"
QUOTE_COHERENCE_CONTRACT_VERSION = "quote-coherence-v1-cross-asset-utc"
ORIGINAL_BACKTEST_PROTOCOL = "sealed-backtest-v1-next-daily-close"


def describe_execution_timing(
    *,
    signal_bar_open: pd.Timestamp,
    backtest_fill_bar_open: pd.Timestamp,
    paper_execution: pd.Timestamp,
) -> dict[str, object]:
    signal_bar_open = pd.Timestamp(signal_bar_open).tz_convert("UTC")
    backtest_fill_bar_open = pd.Timestamp(backtest_fill_bar_open).tz_convert("UTC")
    paper_execution = pd.Timestamp(paper_execution).tz_convert("UTC")
    signal_available = signal_bar_open + pd.Timedelta(days=1)
    backtest_available = backtest_fill_bar_open + pd.Timedelta(days=1)
    return {
        "signal_bar_open_utc": signal_bar_open.isoformat(),
        "signal_price_field": "daily close",
        "signal_available_at_utc": signal_available.isoformat(),
        "backtest_protocol": ORIGINAL_BACKTEST_PROTOCOL,
        "backtest_fill_bar_open_utc": backtest_fill_bar_open.isoformat(),
        "backtest_price_field": "next daily close",
        "backtest_price_available_at_utc": backtest_available.isoformat(),
        "paper_protocol": EXECUTION_PROTOCOL_VERSION,
        "paper_execution_at_utc": paper_execution.isoformat(),
        "classification": "EXECUTION_MODEL_MISMATCH",
        "lookahead_violation": bool(paper_execution < signal_available),
        "explanation": (
            "The sealed backtest fills at the next daily candle close, while forward paper "
            "execution uses a fresh executable bid/ask quote shortly after the prior candle finalizes."
        ),
    }
