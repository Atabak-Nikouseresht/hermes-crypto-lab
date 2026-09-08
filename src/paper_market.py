"""Credential-free CCXT market-data adapter for paper trading."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from src.download_data import RETRYABLE_ERRORS, call_with_retry, create_exchange
from src.paper_broker import (
    MarketSnapshot,
    PaperConfig,
    Quote,
    RuleReferencePrice,
    SymbolRules,
)
from src.validate_data import rows_to_frame


class PublicMarketCapability(Protocol):
    """The complete exchange capability surface allowed in paper operations."""

    def load_markets(self) -> Any: ...

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> Any: ...

    def fetch_ticker(self, symbol: str) -> Any: ...

    def fetch_reference_price(self, symbol: str) -> Any: ...

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

    def fetch_reference_price(self, symbol: str) -> Any:
        """Narrow unauthenticated adapter for Binance's public endpoint."""
        market = self._client.market(symbol)
        market_id = market.get("id") if isinstance(market, dict) else None
        if not isinstance(market_id, str) or not market_id:
            raise ValueError(f"Binance market id missing for {symbol}")
        url = "https://api.binance.com/api/v3/referencePrice?" + urlencode(
            {"symbol": market_id}
        )
        try:
            with urlopen(url, timeout=self._client.timeout / 1000) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code == 400 and '"code":-2043' in body.replace(" ", ""):
                return None
            raise TransientPublicMarketError(str(error)) from error
        except (URLError, TimeoutError, OSError) as error:
            raise TransientPublicMarketError(str(error)) from error
        if not isinstance(payload, dict):
            raise ValueError(f"Malformed Binance reference price for {symbol}")
        return payload

    def market(self, symbol: str) -> Any:
        return self._client.market(symbol)

    def close(self) -> Any:
        close = getattr(self._client, "close", None)
        return close() if callable(close) else None


class TransientPublicMarketError(RuntimeError):
    """A retry-exhausted public transport or exchange-availability failure."""


def create_public_market_client(exchange_id: str, timeout_ms: int) -> PublicMarketClient:
    return PublicMarketClient(create_exchange(exchange_id, timeout_ms))


def _filter_decimal(filter_data: dict[str, Any], field: str) -> float | None:
    value = filter_data.get(field)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _raw_decimal(filter_data: dict[str, Any], field: str) -> Decimal | None:
    """Parse Binance's string representation without a float round trip."""
    value = filter_data.get(field)
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _filter_int(filter_data: dict[str, Any], field: str) -> int | None:
    value = filter_data.get(field)
    return value if type(value) is int and value >= 0 else None


def parse_binance_spot_symbol_rules(market_info: dict[str, Any]) -> SymbolRules:
    """Parse public exchangeInfo for prospective paper MARKET execution only.

    Binance applies its MARKET notional filters to an average/reference price;
    the broker rejects an applicable filter with a nonzero avgPriceMins rather
    than claiming current ticker evidence reconstructs that exchange value.
    """
    info = market_info.get("info")
    if not isinstance(info, dict):
        info = {}
    raw_filters = info.get("filters")
    if not isinstance(raw_filters, list):
        raw_filters = []
    filters = {
        item.get("filterType"): item
        for item in raw_filters
        if isinstance(item, dict)
    }
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    min_notional = filters.get("MIN_NOTIONAL", {})
    notional = filters.get("NOTIONAL", {})
    price_filter = filters.get("PRICE_FILTER", {})
    limits = market_info.get("limits") if isinstance(market_info.get("limits"), dict) else {}
    amount_limits = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
    cost_limits = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}

    status = info.get("status")
    spot_allowed = info.get("isSpotTradingAllowed")
    order_types = info.get("orderTypes")
    permissions = info.get("permissions")
    permission_sets = info.get("permissionSets")
    permissions_allow_spot = permissions is None or (
        isinstance(permissions, list) and (not permissions or "SPOT" in permissions)
    )
    # This is a public-only capability check, not an assertion that any
    # authenticated account is authorized. Binance evaluates entries inside a
    # group as OR and groups as AND; evaluate that rule against the only
    # capability Hermes can establish publicly: SPOT.
    permission_sets_allow_spot = True
    if permission_sets is not None:
        permission_sets_allow_spot = isinstance(permission_sets, list) and bool(permission_sets)
        if permission_sets_allow_spot:
            valid_groups = all(
                isinstance(group, list)
                and group
                and all(isinstance(permission, str) for permission in group)
                for group in permission_sets
            )
            public_capabilities = {"SPOT"}
            permission_sets_allow_spot = valid_groups and all(
                bool(public_capabilities.intersection(group))
                for group in permission_sets
            )
    active = bool(market_info.get("active")) and status == "TRADING" and spot_allowed is True
    active = active and permissions_allow_spot and permission_sets_allow_spot
    market_order_allowed = isinstance(order_types, list) and "MARKET" in order_types

    lot_min = _filter_decimal(lot, "minQty")
    lot_max = _filter_decimal(lot, "maxQty")
    lot_step = _filter_decimal(lot, "stepSize")
    market_min = _filter_decimal(market_lot, "minQty")
    market_max = _filter_decimal(market_lot, "maxQty")
    market_step = _filter_decimal(market_lot, "stepSize")
    return SymbolRules(
        active=active,
        min_quantity=lot_min or float(amount_limits.get("min") or 0.0),
        max_quantity=lot_max
        if lot_max is not None
        else (float(amount_limits["max"]) if amount_limits.get("max") is not None else None),
        step_size=lot_step or 0.0,
        min_notional=_filter_decimal(min_notional, "minNotional")
        or float(cost_limits.get("min") or 0.0),
        price_tick=_filter_decimal(price_filter, "tickSize") or 0.0,
        market_order_allowed=market_order_allowed,
        market_min_quantity=market_min,
        market_max_quantity=market_max,
        market_step_size=market_step,
        min_notional_applies_to_market=min_notional.get("applyToMarket") is True,
        min_notional_avg_price_mins=_filter_int(min_notional, "avgPriceMins") or 0,
        notional_min=_filter_decimal(notional, "minNotional"),
        notional_max=_filter_decimal(notional, "maxNotional"),
        notional_min_applies_to_market=notional.get("applyMinToMarket") is True,
        notional_max_applies_to_market=notional.get("applyMaxToMarket") is True,
        notional_avg_price_mins=_filter_int(notional, "avgPriceMins") or 0,
        raw_min_quantity=_raw_decimal(lot, "minQty"),
        raw_max_quantity=_raw_decimal(lot, "maxQty"),
        raw_step_size=_raw_decimal(lot, "stepSize"),
        raw_market_min_quantity=_raw_decimal(market_lot, "minQty"),
        raw_market_max_quantity=_raw_decimal(market_lot, "maxQty"),
        raw_market_step_size=_raw_decimal(market_lot, "stepSize"),
        raw_min_notional=_raw_decimal(min_notional, "minNotional"),
        raw_notional_min=_raw_decimal(notional, "minNotional"),
        raw_notional_max=_raw_decimal(notional, "maxNotional"),
    )


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
    rule_reference_prices: dict[str, RuleReferencePrice] = {}
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
            informational_last = ticker.get("last")
            if informational_last is None:
                informational_last = ticker.get("close")
            try:
                last = float(informational_last)
            except (TypeError, ValueError):
                last = (bid + ask) / 2.0
            if not math.isfinite(last) or last <= 0:
                last = (bid + ask) / 2.0
            ticker_ms = ticker.get("timestamp")
            if ticker_ms is None:
                raise ValueError(f"Quote timestamp missing for {symbol}")
            quote_time = pd.to_datetime(ticker_ms, unit="ms", utc=True)
            quotes[symbol] = Quote(bid=bid, ask=ask, last=last, timestamp=quote_time)
            reference_payload = call_with_retry(
                lambda symbol=symbol: market.fetch_reference_price(symbol),
                max_retries=max_retries,
                backoff_base_seconds=backoff_base_seconds,
            )
            if reference_payload is None:
                rule_reference_prices[symbol] = RuleReferencePrice(None, "LAST_FALLBACK")
            elif isinstance(reference_payload, dict):
                raw_reference = reference_payload.get("referencePrice")
                if raw_reference is None:
                    rule_reference_prices[symbol] = RuleReferencePrice(None, "LAST_FALLBACK")
                elif isinstance(raw_reference, str):
                    try:
                        reference = Decimal(raw_reference)
                    except InvalidOperation as error:
                        raise ValueError(f"Malformed Binance reference price for {symbol}") from error
                    if not reference.is_finite() or reference <= 0:
                        raise ValueError(f"Invalid Binance reference price for {symbol}")
                    raw_timestamp = reference_payload.get("timestamp")
                    if type(raw_timestamp) is not int or raw_timestamp < 0:
                        raise ValueError(f"Malformed Binance reference price timestamp for {symbol}")
                    rule_reference_prices[symbol] = RuleReferencePrice(
                        reference,
                        "REFERENCE_PRICE",
                        pd.to_datetime(raw_timestamp, unit="ms", utc=True),
                    )
                else:
                    raise ValueError(f"Malformed Binance reference price for {symbol}")
            else:
                raise ValueError(f"Malformed Binance reference price for {symbol}")
            rules[symbol] = parse_binance_spot_symbol_rules(market.market(symbol))
    except RETRYABLE_ERRORS as error:
        raise TransientPublicMarketError(str(error)) from error
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
        rule_reference_prices=rule_reference_prices,
    )
