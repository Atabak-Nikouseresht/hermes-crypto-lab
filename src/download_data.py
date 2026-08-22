from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

import ccxt

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
RETRYABLE_ERRORS = (
    ccxt.RateLimitExceeded,
    ccxt.DDoSProtection,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.NetworkError,
)


def call_with_retry(
    operation: Callable[[], T],
    *,
    max_retries: int,
    backoff_base_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except RETRYABLE_ERRORS as error:
            if attempt == max_retries:
                LOGGER.exception("Operation failed after %d retries", max_retries)
                raise
            delay = backoff_base_seconds * (2**attempt)
            LOGGER.warning(
                "Retryable exchange error (%s); retry %d/%d in %.2fs",
                type(error).__name__,
                attempt + 1,
                max_retries,
                delay,
            )
            sleep(delay)
    raise RuntimeError("Unreachable retry state")


def create_exchange(exchange_id: str, timeout_ms: int) -> ccxt.Exchange:
    try:
        exchange_class = getattr(ccxt, exchange_id)
    except AttributeError as error:
        raise ValueError(f"Unsupported CCXT exchange: {exchange_id}") from error
    exchange = exchange_class(
        {
            "enableRateLimit": True,
            "timeout": timeout_ms,
            "options": {"defaultType": "spot"},
        }
    )
    return exchange


def download_daily_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    *,
    since_iso: str,
    timeframe: str = "1d",
    limit: int = 1000,
    max_retries: int = 5,
    backoff_base_seconds: float = 1.0,
) -> list[list[float]]:
    since_ms = exchange.parse8601(since_iso)
    if since_ms is None:
        raise ValueError(f"Invalid UTC start timestamp: {since_iso}")
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    now = datetime.now(timezone.utc)
    cutoff = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    rows: list[list[float]] = []
    cursor = since_ms

    LOGGER.info("Downloading %s %s from %s to %s", symbol, timeframe, since_iso, cutoff.isoformat())
    while cursor < cutoff_ms:
        batch = call_with_retry(
            lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit),
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
        )
        if not batch:
            break
        finalized = [row for row in batch if row[0] < cutoff_ms]
        rows.extend(finalized)
        last_timestamp = batch[-1][0]
        next_cursor = last_timestamp + timeframe_ms
        if next_cursor <= cursor:
            raise RuntimeError(f"Pagination made no progress for {symbol}")
        cursor = next_cursor
        LOGGER.info("Fetched %d rows for %s (total=%d)", len(finalized), symbol, len(rows))
        if len(batch) < limit:
            break
    LOGGER.info("Completed %s with %d finalized candles", symbol, len(rows))
    return rows
