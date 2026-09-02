from datetime import datetime, timezone

import ccxt
import pytest

from src.download_data import call_with_retry, download_daily_ohlcv


def test_retryable_exchange_error_retries_with_exponential_backoff():
    attempts = 0
    sleeps = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ccxt.RequestTimeout("temporary")
        return "ok"

    assert call_with_retry(
        operation, max_retries=2, backoff_base_seconds=0.5, sleep=sleeps.append
    ) == "ok"
    assert attempts == 3
    assert sleeps == [0.5, 1.0]


def test_retryable_exchange_error_raises_after_exhaustion():
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise ccxt.ExchangeNotAvailable("offline")

    with pytest.raises(ccxt.ExchangeNotAvailable):
        call_with_retry(
            operation, max_retries=1, backoff_base_seconds=0.0, sleep=lambda _delay: None
        )
    assert attempts == 2


class FakeExchange:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.calls = []

    def parse8601(self, _value):
        return int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

    def parse_timeframe(self, _value):
        return 86_400

    def fetch_ohlcv(self, symbol, *, timeframe, since, limit):
        self.calls.append((symbol, timeframe, since, limit))
        return next(self.batches)


def _row(timestamp_ms):
    return [timestamp_ms, 1.0, 1.0, 1.0, 1.0, 1.0]


def test_download_stops_on_empty_batch():
    exchange = FakeExchange([[]])

    assert download_daily_ohlcv(exchange, "BTC/USDT", since_iso="2024-01-01T00:00:00Z") == []
    assert len(exchange.calls) == 1


def test_download_rejects_non_advancing_pagination():
    cursor = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    exchange = FakeExchange([[_row(cursor - 86_400_000)]])

    with pytest.raises(RuntimeError, match="no progress"):
        download_daily_ohlcv(exchange, "BTC/USDT", since_iso="2024-01-01T00:00:00Z")


def test_download_excludes_unfinalized_candles():
    finalized = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
    future = int(datetime(2999, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    exchange = FakeExchange([[_row(finalized), _row(future)]])

    rows = download_daily_ohlcv(
        exchange,
        "BTC/USDT",
        since_iso="2024-01-01T00:00:00Z",
        limit=10,
    )

    assert rows == [_row(finalized)]
