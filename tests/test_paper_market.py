import time
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

    def load_markets(self):
        self.calls.append(("load_markets", ""))
        return {}

    def market(self, symbol):
        return {
            "active": True,
            "limits": {"amount": {"min": 0.001, "max": 1000}, "cost": {"min": 5}},
            "info": {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                ]
            },
        }

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
    assert {name for name, _symbol in exchange.calls} == {
        "load_markets",
        "fetch_ohlcv",
        "fetch_ticker",
    }
    assert snapshot.symbol_rules["BTC/USDT"].min_notional == 5.0
    assert not hasattr(exchange, "create_order")


def test_public_snapshot_fetched_at_is_after_all_network_calls():
    now = datetime.now(timezone.utc)
    dates = pd.date_range(end=pd.Timestamp(now).normalize() - pd.Timedelta(days=1), periods=160, freq="D", tz="UTC")
    rows = [
        [int(timestamp.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 10.0]
        for timestamp in dates
    ]

    class DelayedExchange(FakePublicExchange):
        completed_at = None

        def fetch_ticker(self, symbol):
            result = super().fetch_ticker(symbol)
            time.sleep(0.01)
            self.completed_at = pd.Timestamp.now(tz="UTC")
            return result

    exchange = DelayedExchange(rows, int(pd.Timestamp(now).timestamp() * 1000))
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)),
        exchange=exchange,
        lookback_days=200,
        max_retries=0,
    )

    assert snapshot.fetched_at >= exchange.completed_at
