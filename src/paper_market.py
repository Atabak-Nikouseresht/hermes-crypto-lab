"""Credential-free CCXT market-data adapter for paper trading."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import ccxt
import pandas as pd

from src.download_data import RETRYABLE_ERRORS, call_with_retry, create_exchange
from src.paper_broker import (
    MarketSnapshot,
    PaperConfig,
    PriceRangeRuleEvidence,
    Quote,
    RuleReferencePrice,
    SymbolRules,
    validate_reference_price_evidence,
)
from src.validate_data import rows_to_frame


class PublicMarketCapability(Protocol):
    """The complete exchange capability surface allowed in paper operations."""

    def load_markets(self) -> Any: ...

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> Any: ...

    def fetch_ticker(self, symbol: str) -> Any: ...

    def fetch_reference_price(self, symbol: str) -> Any: ...

    def fetch_execution_rules(self, symbol: str) -> Any: ...

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
            try:
                error_payload = json.loads(body)
            except json.JSONDecodeError:
                error_payload = None
            if (
                error.code == 400 and isinstance(error_payload, dict)
                and type(error_payload.get("code")) is int and error_payload["code"] == -2043
            ):
                return None
            if error.code in {418, 429}:
                raw_retry_after = error.headers.get("Retry-After") if error.headers else None
                retry_after = (
                    int(raw_retry_after)
                    if isinstance(raw_retry_after, str) and len(raw_retry_after) <= 10
                    and raw_retry_after.isascii() and raw_retry_after.isdecimal()
                    else None
                )
                raise TransientPublicMarketError(
                    f"Binance reference-price HTTP {error.code}; Retry-After seconds: {retry_after}",
                    http_status=error.code, retry_after_seconds=retry_after,
                ) from error
            if 500 <= error.code <= 599:
                raise _RetryableReferenceTransportError(str(error)) from error
            raise ValueError(f"Terminal Binance reference-price HTTP {error.code} for {symbol}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise _RetryableReferenceTransportError(str(error)) from error
        if not isinstance(payload, dict):
            raise ValueError(f"Malformed Binance reference price for {symbol}")
        return payload

    def fetch_execution_rules(self, symbol: str) -> Any:
        """Narrow unauthenticated adapter for Binance's public executionRules endpoint."""
        market = self._client.market(symbol)
        market_id = market.get("id") if isinstance(market, dict) else None
        if not isinstance(market_id, str) or not market_id:
            raise ValueError(f"Binance market id missing for {symbol}")
        url = "https://api.binance.com/api/v3/executionRules?" + urlencode({"symbol": market_id})
        try:
            with urlopen(url, timeout=self._client.timeout / 1000) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code in {418, 429}:
                raise TransientPublicMarketError(
                    f"Binance execution-rules HTTP {error.code}", http_status=error.code
                ) from error
            if 500 <= error.code <= 599:
                raise _RetryableReferenceTransportError(str(error)) from error
            raise ValueError(f"Terminal Binance execution-rules HTTP {error.code} for {symbol}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise _RetryableReferenceTransportError(str(error)) from error
        if not isinstance(payload, dict):
            raise ValueError(f"Malformed Binance execution rules for {symbol}")
        return payload

    def market(self, symbol: str) -> Any:
        return self._client.market(symbol)

    def close(self) -> Any:
        close = getattr(self._client, "close", None)
        return close() if callable(close) else None


class TransientPublicMarketError(RuntimeError):
    """Public failure eligible for governed outer retry, not a fallback price."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


class _RetryableReferenceTransportError(TransientPublicMarketError, ccxt.NetworkError):
    """Bridge urllib to the existing single call_with_retry layer, never rate bans."""


def create_public_market_client(exchange_id: str, timeout_ms: int) -> PublicMarketClient:
    return PublicMarketClient(create_exchange(exchange_id, timeout_ms))


def _raw_decimal(filter_data: dict[str, Any], field: str) -> Decimal | None:
    """Parse Binance's string representation without a float round trip."""
    if field not in filter_data:
        return None
    value = filter_data[field]
    if not isinstance(value, str):
        raise ValueError(f"Invalid Binance rule decimal {field}: expected string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"Invalid Binance rule decimal {field}") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"Invalid Binance rule decimal {field}: expected finite nonnegative value")
    return parsed


def _filter_int(filter_data: dict[str, Any], field: str) -> int | None:
    if field not in filter_data:
        return None
    value = filter_data[field]
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid Binance rule integer {field}: expected nonnegative integer")
    return value


def _filter_bool(filter_data: dict[str, Any], field: str) -> bool | None:
    if field not in filter_data:
        return None
    value = filter_data[field]
    if type(value) is not bool:
        raise ValueError(f"Invalid Binance rule boolean {field}: expected boolean")
    return value


def parse_binance_price_range_execution_rule(
    *, symbol: str, native_symbol: str, payload: dict[str, Any], acquired_at: pd.Timestamp
) -> PriceRangeRuleEvidence:
    """Parse one native-symbol executionRules payload without float conversion."""
    symbol_rules = payload.get("symbolRules")
    if not isinstance(symbol_rules, list) or len(symbol_rules) != 1:
        raise ValueError(f"Malformed Binance execution rules for {symbol}")
    symbol_rule = symbol_rules[0]
    if not isinstance(symbol_rule, dict) or symbol_rule.get("symbol") != native_symbol:
        raise ValueError(f"Mismatched Binance execution-rules symbol for {symbol}")
    rules = symbol_rule.get("rules")
    if not isinstance(rules, list):
        raise ValueError(f"Malformed Binance execution rules for {symbol}")
    if any(not isinstance(rule, dict) or not isinstance(rule.get("ruleType"), str) for rule in rules):
        raise ValueError(f"Malformed Binance execution rules for {symbol}")
    price_rules = [rule for rule in rules if rule["ruleType"] == "PRICE_RANGE"]
    if len(price_rules) > 1:
        raise ValueError(f"Duplicate Binance PRICE_RANGE rule for {symbol}")
    raw_timestamp = payload.get("timestamp", payload.get("serverTime"))
    if raw_timestamp is not None and (type(raw_timestamp) is not int or raw_timestamp < 0):
        raise ValueError(f"Malformed Binance execution-rules timestamp for {symbol}")
    source_timestamp = (
        pd.to_datetime(raw_timestamp, unit="ms", utc=True)
        if raw_timestamp is not None
        else None
    )
    if not price_rules:
        return PriceRangeRuleEvidence(
            symbol=symbol, native_symbol=native_symbol, status="PRICE_RANGE_ABSENT",
            price_range_present=False, source_timestamp=source_timestamp, acquired_at=acquired_at,
        )
    price_rule = price_rules[0]
    multipliers = {
        field: _raw_decimal(price_rule, field)
        for field in (
            "bidLimitMultUp", "bidLimitMultDown", "askLimitMultUp", "askLimitMultDown"
        )
    }
    if any(value is not None and value <= 0 for value in multipliers.values()):
        raise ValueError(f"Invalid Binance PRICE_RANGE multiplier for {symbol}")
    return PriceRangeRuleEvidence(
        symbol=symbol, native_symbol=native_symbol, status="PRICE_RANGE_PRESENT",
        price_range_present=True,
        bid_limit_mult_up=multipliers["bidLimitMultUp"],
        bid_limit_mult_down=multipliers["bidLimitMultDown"],
        ask_limit_mult_up=multipliers["askLimitMultUp"],
        ask_limit_mult_down=multipliers["askLimitMultDown"],
        source_timestamp=source_timestamp, acquired_at=acquired_at,
    )


def parse_binance_spot_symbol_rules(market_info: dict[str, Any]) -> SymbolRules:
    """Parse public exchangeInfo for prospective paper MARKET execution only.

    Binance applies its MARKET notional filters to an average/reference price;
    the broker rejects an applicable filter with a nonzero avgPriceMins rather
    than claiming current ticker evidence reconstructs that exchange value.
    """
    info = market_info.get("info")
    if not isinstance(info, dict):
        info = {}
    raw_filters = info.get("filters", [])
    if not isinstance(raw_filters, list):
        raise ValueError("Invalid Binance rule filters: expected list")
    filters: dict[str, dict[str, Any]] = {}
    for item in raw_filters:
        if not isinstance(item, dict):
            raise ValueError("Invalid Binance rule filter entry")
        filter_type = item.get("filterType")
        if not isinstance(filter_type, str):
            raise ValueError("Invalid Binance rule filter type")
        if filter_type in filters:
            raise ValueError(f"Duplicate Binance rule filter {filter_type}")
        filters[filter_type] = item
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
        isinstance(permissions, list)
        and all(isinstance(permission, str) for permission in permissions)
        and (not permissions or "SPOT" in permissions)
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
    market_order_allowed = (
        isinstance(order_types, list)
        and all(isinstance(order_type, str) for order_type in order_types)
        and "MARKET" in order_types
    )

    # Retain an explicit Binance zero.  A zero filter value disables that
    # bound; it must not be mistaken for a missing field and replaced by a
    # CCXT convenience limit.
    _raw_decimal(price_filter, "minPrice")
    _raw_decimal(price_filter, "maxPrice")
    price_tick_raw = _raw_decimal(price_filter, "tickSize")
    lot_min_raw = _raw_decimal(lot, "minQty")
    lot_max_raw = _raw_decimal(lot, "maxQty")
    lot_step_raw = _raw_decimal(lot, "stepSize")
    market_min_raw = _raw_decimal(market_lot, "minQty")
    market_max_raw = _raw_decimal(market_lot, "maxQty")
    market_step_raw = _raw_decimal(market_lot, "stepSize")
    min_notional_raw = _raw_decimal(min_notional, "minNotional")
    notional_min_raw = _raw_decimal(notional, "minNotional")
    notional_max_raw = _raw_decimal(notional, "maxNotional")
    return SymbolRules(
        active=active,
        min_quantity=(float(lot_min_raw) if lot_min_raw is not None else float(amount_limits.get("min") or 0.0)),
        max_quantity=(float(lot_max_raw) if lot_max_raw is not None else None)
        if lot_max_raw is not None
        else (float(amount_limits["max"]) if amount_limits.get("max") is not None else None),
        step_size=float(lot_step_raw) if lot_step_raw is not None else 0.0,
        min_notional=(float(min_notional_raw) if min_notional_raw is not None else float(cost_limits.get("min") or 0.0)),
        price_tick=float(price_tick_raw) if price_tick_raw is not None else 0.0,
        market_order_allowed=market_order_allowed,
        market_min_quantity=float(market_min_raw) if market_min_raw is not None else None,
        market_max_quantity=float(market_max_raw) if market_max_raw is not None else None,
        market_step_size=float(market_step_raw) if market_step_raw is not None else None,
        min_notional_applies_to_market=_filter_bool(min_notional, "applyToMarket") is True,
        min_notional_avg_price_mins=_filter_int(min_notional, "avgPriceMins") or 0,
        notional_min=float(notional_min_raw) if notional_min_raw is not None else None,
        notional_max=float(notional_max_raw) if notional_max_raw is not None else None,
        notional_min_applies_to_market=_filter_bool(notional, "applyMinToMarket") is True,
        notional_max_applies_to_market=_filter_bool(notional, "applyMaxToMarket") is True,
        notional_avg_price_mins=_filter_int(notional, "avgPriceMins") or 0,
        raw_min_quantity=lot_min_raw,
        raw_max_quantity=lot_max_raw,
        raw_step_size=lot_step_raw,
        raw_market_min_quantity=market_min_raw,
        raw_market_max_quantity=market_max_raw,
        raw_market_step_size=market_step_raw,
        raw_min_notional=min_notional_raw,
        raw_notional_min=notional_min_raw,
        raw_notional_max=notional_max_raw,
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
    acquisition_clock: Callable[[], datetime] | None = None,
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
    price_range_rules: dict[str, PriceRangeRuleEvidence] = {}
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
            # Receipt is independent of the caller's snapshot-start/candle clock.
            acquired_at = pd.Timestamp(
                acquisition_clock() if acquisition_clock is not None else datetime.now(timezone.utc)
            ).tz_convert("UTC")
            market_info = market.market(symbol)
            if reference_payload is None:
                rule_reference_prices[symbol] = RuleReferencePrice(None, "LAST_FALLBACK")
            elif isinstance(reference_payload, dict):
                expected_symbol = market_info.get("id") if isinstance(market_info, dict) else None
                if (
                    not isinstance(expected_symbol, str)
                    or reference_payload.get("symbol") != expected_symbol
                ):
                    raise ValueError(f"Mismatched Binance reference price symbol for {symbol}")
                if "referencePrice" not in reference_payload:
                    raise ValueError(f"Malformed Binance reference price for {symbol}")
                raw_reference = reference_payload["referencePrice"]
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
                    reference_timestamp = pd.to_datetime(raw_timestamp, unit="ms", utc=True)
                    evidence = RuleReferencePrice(
                        reference,
                        "REFERENCE_PRICE",
                        reference_timestamp,
                        acquired_at,
                    )
                    # Binance documents referencePrice as continually changing,
                    # but publishes no fixed REST update interval.  The governed
                    # future-only evidence contract reuses quote freshness.
                    evidence_error = validate_reference_price_evidence(
                        evidence,
                        now=acquired_at,
                        max_age_minutes=config.max_quote_staleness_minutes,
                    )
                    if evidence_error is not None:
                        raise ValueError(f"{evidence_error} for {symbol}")
                    rule_reference_prices[symbol] = evidence
                else:
                    raise ValueError(f"Malformed Binance reference price for {symbol}")
            else:
                raise ValueError(f"Malformed Binance reference price for {symbol}")
            rules[symbol] = parse_binance_spot_symbol_rules(market_info)
            expected_symbol = market_info.get("id") if isinstance(market_info, dict) else None
            execution_rules_method = getattr(market, "fetch_execution_rules", None)
            if not config.require_execution_rule_evidence or config.exchange_id != "binance":
                price_range_rules[symbol] = PriceRangeRuleEvidence(
                    symbol=symbol, native_symbol=None, status="RULE_NOT_APPLICABLE",
                    price_range_present=None,
                )
            elif not callable(execution_rules_method) or not isinstance(expected_symbol, str):
                price_range_rules[symbol] = PriceRangeRuleEvidence(
                    symbol=symbol, native_symbol=expected_symbol if isinstance(expected_symbol, str) else None,
                    status="TRANSPORT_FAILURE", price_range_present=None,
                    acquired_at=pd.Timestamp(
                        acquisition_clock() if acquisition_clock is not None else datetime.now(timezone.utc)
                    ).tz_convert("UTC"),
                )
            else:
                try:
                    execution_payload = call_with_retry(
                        lambda method=execution_rules_method, symbol=symbol: method(symbol),
                        max_retries=max_retries, backoff_base_seconds=backoff_base_seconds,
                    )
                    execution_acquired_at = pd.Timestamp(
                        acquisition_clock() if acquisition_clock is not None else datetime.now(timezone.utc)
                    ).tz_convert("UTC")
                    price_range_rules[symbol] = parse_binance_price_range_execution_rule(
                        symbol=symbol, native_symbol=expected_symbol, payload=execution_payload,
                        acquired_at=execution_acquired_at,
                    )
                except TransientPublicMarketError:
                    price_range_rules[symbol] = PriceRangeRuleEvidence(
                        symbol=symbol, native_symbol=expected_symbol,
                        status="TRANSPORT_FAILURE", price_range_present=None,
                        acquired_at=pd.Timestamp(
                            acquisition_clock() if acquisition_clock is not None else datetime.now(timezone.utc)
                        ).tz_convert("UTC"),
                    )
                except (TypeError, ValueError, InvalidOperation):
                    price_range_rules[symbol] = PriceRangeRuleEvidence(
                        symbol=symbol, native_symbol=expected_symbol,
                        status="MALFORMED_RESPONSE", price_range_present=None,
                        acquired_at=pd.Timestamp(
                            acquisition_clock() if acquisition_clock is not None else datetime.now(timezone.utc)
                        ).tz_convert("UTC"),
                    )
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
        price_range_rules=price_range_rules,
    )
