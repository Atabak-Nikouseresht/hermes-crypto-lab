from datetime import datetime, timedelta, timezone
import json
from functools import partial
from io import BytesIO
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from src.download_data import call_with_retry
from src.paper_broker import PaperConfig
from src.paper_market import PublicMarketClient, TransientPublicMarketError, fetch_public_market_snapshot
from tests.test_paper_market import FakePublicExchange


START = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)


def _exchange(source_time=START):
    stamp = int(pd.Timestamp(START).timestamp() * 1000)
    exchange = FakePublicExchange([[stamp - 86400000, 100, 101, 99, 100, 10]], stamp)
    exchange.timeout = 1000
    exchange.fetch_reference_price = lambda symbol: {
        "symbol": symbol.replace("/", ""), "referencePrice": "100",
        "timestamp": int(pd.Timestamp(source_time).timestamp() * 1000),
    }
    return exchange


def test_clock1_default_receipt_clock_ignores_supplied_snapshot_start(monkeypatch):
    receipt = START + timedelta(seconds=2)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return receipt

    monkeypatch.setattr("src.paper_market.datetime", Clock)
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)), exchange=_exchange(receipt), now=START,
    )
    assert snapshot.rule_reference_prices["BTC/USDT"].acquired_at == pd.Timestamp(receipt)

class _Client:
    timeout = 1_000

    def market(self, _symbol):
        return {"id": "BTCUSDT"}


def _http_error(status, body):
    return HTTPError("https://api.binance.com", status, "error", None, BytesIO(body))


@pytest.mark.parametrize("status", [429, 500, 502])
def test_reference_price_retryable_http_statuses_are_typed_transient(monkeypatch, status):
    monkeypatch.setattr("src.paper_market.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(status, b"{}")))
    with pytest.raises(TransientPublicMarketError):
        PublicMarketClient(_Client()).fetch_reference_price("BTC/USDT")


@pytest.mark.parametrize("status", [400, 404])
def test_reference_price_permanent_http_statuses_are_terminal(monkeypatch, status):
    monkeypatch.setattr("src.paper_market.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(status, b'{"code":-1121,"msg":"Invalid symbol."}')))
    with pytest.raises(ValueError, match="Terminal Binance"):
        PublicMarketClient(_Client()).fetch_reference_price("BTC/USDT")


def test_reference_price_documented_no_reference_is_not_transient(monkeypatch):
    monkeypatch.setattr("src.paper_market.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(400, b'{"code":-2043,"msg":"No reference price"}')))
    assert PublicMarketClient(_Client()).fetch_reference_price("BTC/USDT") is None


@pytest.mark.parametrize("age_ms,valid", [(299999, True), (300000, True), (300001, False), (-1, False)])
def test_clock2_receipt_source_age_exact_boundary(age_ms, valid):
    receipt = START + timedelta(seconds=20)
    source = receipt - timedelta(milliseconds=age_ms)
    def fetch():
        return fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)), exchange=_exchange(source), now=START,
            acquisition_clock=lambda: receipt,
        )
    if valid:
        assert fetch().rule_reference_prices["BTC/USDT"].acquired_at == pd.Timestamp(receipt)
    else:
        with pytest.raises(ValueError, match="reference price timestamp"):
            fetch()


def test_clock3_receipt_sampled_after_response_before_metadata_per_symbol():
    exchange = _exchange()
    events = []
    times = iter([START + timedelta(seconds=1), START + timedelta(seconds=2)])
    original_reference, original_market = exchange.fetch_reference_price, exchange.market
    def reference(symbol):
        events.append("response:" + symbol)
        return original_reference(symbol)
    def clock():
        events.append("receipt")
        return next(times)
    def market(symbol):
        events.append("metadata:" + symbol)
        return original_market(symbol)
    exchange.fetch_reference_price, exchange.market = reference, market
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT", "ETH/USDT")), exchange=exchange, now=START,
        acquisition_clock=clock,
    )
    assert events == ["response:BTC/USDT", "receipt", "metadata:BTC/USDT",
                      "response:ETH/USDT", "receipt", "metadata:ETH/USDT"]
    assert snapshot.rule_reference_prices["ETH/USDT"].acquired_at > snapshot.rule_reference_prices["BTC/USDT"].acquired_at


@pytest.mark.parametrize("failure", [500, 502, "network", "timeout"])
@pytest.mark.parametrize("succeed", [False, True])
def test_http_single_inner_retry_layer_counts_exhaustion_and_success(monkeypatch, failure, succeed):
    calls, delays = [], []
    def request(*args, **kwargs):
        calls.append(args)
        if succeed and len(calls) == 3:
            return BytesIO(json.dumps(_exchange().fetch_reference_price("BTC/USDT")).encode())
        if failure == "network":
            raise URLError("offline")
        if failure == "timeout":
            raise TimeoutError("timeout")
        raise _http_error(failure, b"{}")
    monkeypatch.setattr("src.paper_market.urlopen", request)
    monkeypatch.setattr("src.paper_market.call_with_retry", partial(call_with_retry, sleep=delays.append))
    def fetch():
        return fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)), exchange=PublicMarketClient(_exchange()),
            now=START, acquisition_clock=lambda: START, max_retries=2, backoff_base_seconds=3,
        )
    if succeed:
        assert fetch().rule_reference_prices["BTC/USDT"].source == "REFERENCE_PRICE"
    else:
        with pytest.raises(TransientPublicMarketError):
            fetch()
    assert len(calls) == 3
    assert delays == [3, 6]


@pytest.mark.parametrize("status", [418, 429])
@pytest.mark.parametrize("header,expected", [("120", 120), ("0", 0), (None, None), ("-1", None), ("NaN", None), ("1\r\ninjected", None)])
def test_rate_limit_has_no_inner_retry_and_safe_retry_after(monkeypatch, status, header, expected):
    calls, delays = [], []
    def request(*args, **kwargs):
        calls.append(args)
        raise HTTPError("https://api.binance.com", status, "error",
                        {"Retry-After": header} if header is not None else {}, BytesIO(b"{}"))
    monkeypatch.setattr("src.paper_market.urlopen", request)
    monkeypatch.setattr("src.paper_market.call_with_retry", partial(call_with_retry, sleep=delays.append))
    with pytest.raises(TransientPublicMarketError) as raised:
        fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)), exchange=PublicMarketClient(_exchange()),
            now=START, acquisition_clock=lambda: START, max_retries=5,
        )
    assert len(calls) == 1
    assert delays == []
    assert raised.value.retry_after_seconds == expected
    assert raised.value.http_status == status
    assert "injected" not in str(raised.value)


@pytest.mark.parametrize("body", [b"bad json", b"[]", b'{"symbol":"BTCUSDT"}'])
def test_malformed_success_is_terminal_without_retries(monkeypatch, body):
    calls, delays = [], []
    def request(*args, **kwargs):
        calls.append(args)
        return BytesIO(body)
    monkeypatch.setattr("src.paper_market.urlopen", request)
    monkeypatch.setattr("src.paper_market.call_with_retry", partial(call_with_retry, sleep=delays.append))
    with pytest.raises(ValueError):
        fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)), exchange=PublicMarketClient(_exchange()),
            now=START, acquisition_clock=lambda: START,
        )
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@pytest.mark.parametrize("body", [b'{"code":-1121}', b'not JSON', b'{"code":"-2043"}', b'{"code":-2043.0}'])
def test_permanent_http_errors_are_single_call_terminal(monkeypatch, status, body):
    calls, delays = [], []
    def request(*args, **kwargs):
        calls.append(args)
        raise _http_error(status, body)
    monkeypatch.setattr("src.paper_market.urlopen", request)
    monkeypatch.setattr("src.paper_market.call_with_retry", partial(call_with_retry, sleep=delays.append))
    with pytest.raises(ValueError, match="Terminal Binance"):
        fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)), exchange=PublicMarketClient(_exchange()), now=START,
        )
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("elapsed_ms,valid", [(300000, True), (300001, False)])
def test_clock4_later_validation_does_not_refresh_acquisition(elapsed_ms, valid):
    from src.paper_broker import validate_reference_price_evidence
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)), exchange=_exchange(), now=START,
        acquisition_clock=lambda: START,
    )
    evidence = snapshot.rule_reference_prices["BTC/USDT"]
    error = validate_reference_price_evidence(
        evidence, now=pd.Timestamp(START + timedelta(milliseconds=elapsed_ms)), max_age_minutes=5,
    )
    assert (error is None) is valid
    assert evidence.acquired_at == pd.Timestamp(START)


def test_clock5_acquisition_age_is_independent_of_source_age():
    from src.paper_broker import RuleReferencePrice, validate_reference_price_evidence
    from decimal import Decimal
    receipt = pd.Timestamp(START)
    # Source was already old on receipt: later freshness cannot reset that age.
    evidence = RuleReferencePrice(Decimal("100"), "REFERENCE_PRICE",
                                  receipt - pd.Timedelta(seconds=299), receipt)
    assert validate_reference_price_evidence(evidence, now=receipt + pd.Timedelta(seconds=2), max_age_minutes=5) is not None


@pytest.mark.parametrize("absence", ["error", "null"])
def test_documented_absence_single_call_and_no_retry(monkeypatch, absence):
    calls, delays = [], []
    def request(*args, **kwargs):
        calls.append(args)
        if absence == "error":
            raise _http_error(400, b'{"code": -2043, "msg": "No reference price"}')
        return BytesIO(b'{"symbol":"BTCUSDT","referencePrice":null,"timestamp":1722816600000}')
    monkeypatch.setattr("src.paper_market.urlopen", request)
    monkeypatch.setattr("src.paper_market.call_with_retry", partial(call_with_retry, sleep=delays.append))
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)), exchange=PublicMarketClient(_exchange()),
        now=START, acquisition_clock=lambda: START, max_retries=5,
    )
    assert snapshot.rule_reference_prices["BTC/USDT"].source == "LAST_FALLBACK"
    assert len(calls) == 1
    assert delays == []
