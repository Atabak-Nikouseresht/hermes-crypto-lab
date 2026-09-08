import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.paper_broker import PaperConfig, PaperTradingSystem
from src.paper_market import (
    create_public_market_client,
    fetch_public_market_snapshot,
    parse_binance_spot_symbol_rules,
)


def _binance_market_info(*, filters, **info):
    return {
        "active": True,
        "limits": {"amount": {"min": 0.001, "max": 1000}, "cost": {"min": 5}},
        "info": {
            "status": "TRADING",
            "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET"],
            "permissions": ["SPOT"],
            "filters": filters,
            **info,
        },
    }


def test_binance_market_rules_preserve_market_specific_filter_semantics():
    rules = parse_binance_spot_symbol_rules(
        _binance_market_info(
            filters=[
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "maxQty": "10", "stepSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": False, "avgPriceMins": 0},
                {"filterType": "NOTIONAL", "minNotional": "10", "maxNotional": "100", "applyMinToMarket": True, "applyMaxToMarket": True, "avgPriceMins": 0},
                {"filterType": "PRICE_FILTER", "minPrice": "0", "maxPrice": "0", "tickSize": "0.01"},
                {"filterType": "UNRELATED_FILTER", "value": "ignored"},
            ]
        )
    )

    assert rules.active and rules.market_order_allowed
    assert (rules.market_min_quantity, rules.market_max_quantity, rules.market_step_size) == (
        0.01,
        10.0,
        0.01,
    )
    assert not rules.min_notional_applies_to_market
    assert (rules.notional_min, rules.notional_max) == (10.0, 100.0)
    assert rules.notional_min_applies_to_market and rules.notional_max_applies_to_market


@pytest.mark.parametrize(
    ("permission_sets", "expected"),
    [
        ([["SPOT", "MARGIN"]], True),
        ([["SPOT", "MARGIN"], ["TRD_GRP_004", "TRD_GRP_005"]], False),
        ([["MARGIN"]], False),
        ("malformed", False),
        ([["SPOT", 4]], False),
    ],
)
def test_public_spot_capability_handles_permission_sets_without_claiming_account_auth(
    permission_sets, expected
):
    rules = parse_binance_spot_symbol_rules(
        _binance_market_info(
            permissions=[], permissionSets=permission_sets,
            filters=[{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10", "stepSize": "0.001"}],
        )
    )

    assert rules.active is expected


@pytest.mark.parametrize(
    ("overrides", "active", "market_allowed"),
    [
        ({"status": "BREAK"}, False, True),
        ({"isSpotTradingAllowed": False}, False, True),
        ({"orderTypes": ["LIMIT"]}, True, False),
        ({"permissions": ["MARGIN"]}, False, True),
        ({"permissions": []}, True, True),
    ],
)
def test_binance_market_rules_fail_closed_for_ineligible_market_execution(
    overrides, active, market_allowed
):
    rules = parse_binance_spot_symbol_rules(
        _binance_market_info(
            filters=[
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True, "avgPriceMins": 0},
            ],
            **overrides,
        )
    )

    assert rules.active is active
    assert rules.market_order_allowed is market_allowed


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
            "id": symbol.replace("/", ""),
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

    def fetch_reference_price(self, symbol):
        self.calls.append(("fetch_reference_price", symbol))
        return {
            "symbol": symbol.replace("/", ""),
            "referencePrice": "100.0",
            "timestamp": self.now_ms,
        }


def test_public_market_client_exposes_no_private_exchange_capability(monkeypatch):
    class GeneralPurposeExchange:
        def load_markets(self):
            return {}

        def fetch_ohlcv(self, *args, **kwargs):
            return []

        def fetch_ticker(self, symbol):
            return {"symbol": symbol}

        def market(self, symbol):
            return {"symbol": symbol}

        def close(self):
            return None

        def create_order(self, *args, **kwargs):
            raise AssertionError("must not be exposed")

        def fetch_balance(self):
            raise AssertionError("must not be exposed")

    monkeypatch.setattr(
        "src.paper_market.create_exchange", lambda _exchange_id, _timeout: GeneralPurposeExchange()
    )

    client = create_public_market_client("binance", 30_000)

    assert client.load_markets() == {}
    assert not hasattr(client, "create_order")
    assert not hasattr(client, "fetch_balance")


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
        "fetch_reference_price",
    }
    assert snapshot.symbol_rules["BTC/USDT"].min_notional == 5.0
    assert not hasattr(exchange, "create_order")


def test_public_snapshot_derives_informational_last_from_valid_bid_ask():
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    dates = pd.date_range(end="2024-08-04", periods=160, freq="D", tz="UTC")
    rows = [
        [int(timestamp.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 10.0]
        for timestamp in dates
    ]

    class MissingLastExchange(FakePublicExchange):
        def fetch_ticker(self, symbol):
            self.calls.append(("fetch_ticker", symbol))
            return {"bid": 99.0, "ask": 101.0, "timestamp": self.now_ms}

    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)),
        exchange=MissingLastExchange(rows, int(pd.Timestamp(now).timestamp() * 1000)),
        now=now,
        lookback_days=200,
        max_retries=0,
    )

    assert snapshot.quotes["BTC/USDT"].last == 100.0


@pytest.mark.parametrize("informational_last", [0.0, -1.0, float("nan"), float("inf")])
def test_public_snapshot_replaces_invalid_informational_last_with_midpoint(
    informational_last,
):
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    dates = pd.date_range(end="2024-08-04", periods=160, freq="D", tz="UTC")
    rows = [
        [int(timestamp.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 10.0]
        for timestamp in dates
    ]

    class InvalidLastExchange(FakePublicExchange):
        def fetch_ticker(self, symbol):
            self.calls.append(("fetch_ticker", symbol))
            return {
                "bid": 99.0,
                "ask": 101.0,
                "last": informational_last,
                "timestamp": self.now_ms,
            }

    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)),
        exchange=InvalidLastExchange(rows, int(pd.Timestamp(now).timestamp() * 1000)),
        now=now,
        lookback_days=200,
        max_retries=0,
    )

    assert snapshot.quotes["BTC/USDT"].last == 100.0


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (0.0, 101.0),
        (-1.0, 101.0),
        (float("nan"), 101.0),
        (99.0, 0.0),
        (99.0, float("inf")),
        (101.0, 99.0),
        (1e308, 1e308),
    ],
)
def test_public_snapshot_still_rejects_invalid_executable_quote_sides(tmp_path, bid, ask):
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    dates = pd.date_range(end="2024-08-04", periods=160, freq="D", tz="UTC")
    rows = [
        [int(timestamp.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 10.0]
        for timestamp in dates
    ]

    class InvalidExecutableSideExchange(FakePublicExchange):
        def fetch_ticker(self, symbol):
            self.calls.append(("fetch_ticker", symbol))
            return {"bid": bid, "ask": ask, "last": 100.0, "timestamp": self.now_ms}

    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)),
        exchange=InvalidExecutableSideExchange(
            rows, int(pd.Timestamp(now).timestamp() * 1000)
        ),
        now=now,
        lookback_days=200,
        max_retries=0,
    )

    system = PaperTradingSystem(tmp_path / "paper.duckdb", PaperConfig(assets=("BTC/USDT",)))
    assert system._validate_snapshot(snapshot, pd.Timestamp(now)) is not None


@pytest.mark.parametrize("missing_field", ["bid", "ask"])
def test_public_snapshot_rejects_missing_executable_quote_side(missing_field):
    now = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    dates = pd.date_range(end="2024-08-04", periods=160, freq="D", tz="UTC")
    rows = [
        [int(timestamp.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 10.0]
        for timestamp in dates
    ]

    class MissingExecutableSideExchange(FakePublicExchange):
        def fetch_ticker(self, symbol):
            ticker = super().fetch_ticker(symbol)
            ticker.pop(missing_field)
            return ticker

    with pytest.raises(ValueError, match="Executable bid/ask missing"):
        fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)),
            exchange=MissingExecutableSideExchange(
                rows, int(pd.Timestamp(now).timestamp() * 1000)
            ),
            now=now,
            lookback_days=200,
            max_retries=0,
        )


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
