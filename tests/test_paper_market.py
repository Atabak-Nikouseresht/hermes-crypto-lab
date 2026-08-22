from datetime import datetime, timezone

import pandas as pd

from src.paper_broker import PaperConfig
from src.paper_market import fetch_public_market_snapshot


class FakePublicExchange:
    def __init__(self, rows, now_ms):
        self.rows = rows
        self.now_ms = now_ms
        self.calls = []

    def parse8601(self, value):
        return int(pd.Timestamp(value).timestamp() * 1000)

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls.append(("fetch_ohlcv", symbol))
        return self.rows

    def fetch_ticker(self, symbol):
        self.calls.append(("fetch_ticker", symbol))
        return {"bid": 99.9, "ask": 100.1, "last": 100.0, "timestamp": self.now_ms}


def test_public_snapshot_uses_only_market_data_methods():
    assets = ("BTC/USDT", "ETH/USDT")
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    dates = pd.date_range(end="2024-08-04", periods=160, freq="D", tz="UTC")
    rows = [
        [int(timestamp.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 10.0]
        for timestamp in dates
    ]
    exchange = FakePublicExchange(rows, int(pd.Timestamp(now).timestamp() * 1000))
    config = PaperConfig(assets=assets)

    snapshot = fetch_public_market_snapshot(
        config,
        exchange=exchange,
        now=now,
        lookback_days=200,
        max_retries=0,
    )

    assert list(snapshot.closes.columns) == list(assets)
    assert snapshot.closes.index[-1] == pd.Timestamp("2024-08-04", tz="UTC")
    assert {name for name, _symbol in exchange.calls} == {"fetch_ohlcv", "fetch_ticker"}
    assert not hasattr(exchange, "create_order")
