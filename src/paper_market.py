"""Credential-free CCXT market-data adapter for paper trading."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import pandas as pd

from src.download_data import call_with_retry, create_exchange
from src.paper_broker import MarketSnapshot, PaperConfig, Quote, SymbolRules
from src.validate_data import rows_to_frame


class PublicMarketCapability(Protocol):
    """The complete exchange capability surface allowed in paper operations."""

    def load_markets(self) -> Any: ...

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> Any: ...

    def fetch_ticker(self, symbol: str) -> Any: ...

    def market(self, symbol: str) -> Any: ...

    def close(self) -> Any: ...


class PublicMarketClient:
    """Capability-limited facade over a general-purpose exchange client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def load_markets(self) -> Any:
        return self._client.load_markets()

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> Any:
        return self._client.fetch_ohlcv(
            symbol, timeframe=timeframe, since=since, limit=limit
        )

    def fetch_ticker(self, symbol: str) -> Any:
        return self._client.fetch_ticker(symbol)

    def market(self, symbol: str) -> Any:
        return self._client.market(symbol)

    def close(self) -> Any:
        close = getattr(self._client, "close", None)
        return close() if callable(close) else None


def create_public_market_client(exchange_id: str, timeout_ms: int) -> PublicMarketClient:
    return PublicMarketClient(create_exchange(exchange_id, timeout_ms))


def fetch_public_market_snapshot(
    config: PaperConfig,
    *,
    exchange: PublicMarketCapability | None = None,
    exchange_id: str = "binance",
    now: datetime | None = None,
    lookback_days: int = 260,
    max_retries: int = 5,
    backoff_base_seconds: float = 1.0,
    timeout_ms: int = 30_000,
) -> MarketSnapshot:
    """Fetch public OHLCV and ticker data; no trading method is ever called."""
    current = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert("UTC")
    since = current.normalize() - pd.Timedelta(days=lookback_days)
    since_ms = int(since.timestamp() * 1000)
    cutoff_ms = int(current.normalize().timestamp() * 1000)
    owned_exchange = exchange is None
    market = exchange or create_public_market_client(exchange_id, timeout_ms)
    close_series = []
    quotes: dict[str, Quote] = {}
    rules: dict[str, SymbolRules] = {}
    ohlcv: dict[str, pd.DataFrame] = {}
    try:
        call_with_retry(
            lambda: market.load_markets(),
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
        )
        for symbol in config.assets:
            rows = call_with_retry(
                lambda symbol=symbol: market.fetch_ohlcv(
                    symbol, timeframe="1d", since=since_ms, limit=lookback_days + 10
                ),
                max_retries=max_retries,
                backoff_base_seconds=backoff_base_seconds,
            )
            finalized = [row for row in rows if row[0] < cutoff_ms]
            if not finalized:
                raise ValueError(f"No finalized public OHLCV rows returned for {symbol}")
            frame = rows_to_frame(finalized)
            ohlcv[symbol] = frame.copy()
            close_series.append(
                pd.Series(
                    frame["close"].to_numpy(dtype=float),
                    index=frame["timestamp"],
                    name=symbol,
                )
            )
            ticker = call_with_retry(
                lambda symbol=symbol: market.fetch_ticker(symbol),
                max_retries=max_retries,
                backoff_base_seconds=backoff_base_seconds,
            )
            if ticker.get("bid") is None or ticker.get("ask") is None:
                raise ValueError(f"Executable bid/ask missing for {symbol}")
            bid = float(ticker["bid"])
            ask = float(ticker["ask"])
            informational_last = ticker.get("last") or ticker.get("close")
            last = (
                float(informational_last)
                if informational_last is not None
                else (bid + ask) / 2.0
            )
            ticker_ms = ticker.get("timestamp")
            if ticker_ms is None:
                raise ValueError(f"Quote timestamp missing for {symbol}")
            quote_time = pd.to_datetime(ticker_ms, unit="ms", utc=True)
            quotes[symbol] = Quote(bid=bid, ask=ask, last=last, timestamp=quote_time)
            market_info = market.market(symbol)
            filters = {
                item.get("filterType"): item
                for item in market_info.get("info", {}).get("filters", [])
            }
            lot = filters.get("LOT_SIZE", {})
            notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
            price_filter = filters.get("PRICE_FILTER", {})
            limits = market_info.get("limits", {})
            rules[symbol] = SymbolRules(
                active=bool(market_info.get("active")),
                min_quantity=float(lot.get("minQty") or limits.get("amount", {}).get("min") or 0.0),
                max_quantity=(
                    float(lot.get("maxQty") or limits.get("amount", {}).get("max"))
                    if (lot.get("maxQty") or limits.get("amount", {}).get("max")) is not None
                    else None
                ),
                step_size=float(lot.get("stepSize") or 0.0),
                min_notional=float(
                    notional.get("minNotional") or limits.get("cost", {}).get("min") or 0.0
                ),
                price_tick=float(price_filter.get("tickSize") or 0.0),
            )
    finally:
        if owned_exchange:
            close = getattr(market, "close", None)
            if callable(close):
                close()
    fetched_at = current if now is not None else pd.Timestamp.now(tz="UTC")
    closes = pd.concat(close_series, axis=1, join="inner").sort_index()
    return MarketSnapshot(
        closes=closes,
        quotes=quotes,
        fetched_at=fetched_at,
        symbol_rules=rules,
        ohlcv=ohlcv,
    )
