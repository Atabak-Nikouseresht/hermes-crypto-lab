"""Read-only live canary for the Binance public schemas consumed by paper market data."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from typing import Any, Callable
from http.client import HTTPException
from urllib.error import HTTPError, URLError

import pandas as pd

from src.binance_public_api import (
    BinancePublicRequestError,
    build_binance_public_url,
    open_public_binance_url,
)
from src.paper_market import (
    parse_binance_price_range_execution_rule,
    parse_binance_reference_price,
)

CANARY_SYMBOL = "BTCUSDT"
REQUEST_TIMEOUT_SECONDS = 10
REFERENCE_PRICE_PATH = "/api/v3/referencePrice"
EXECUTION_RULES_PATH = "/api/v3/executionRules"


class BinanceCanaryError(RuntimeError):
    """Base class for safe, concise live-canary failures."""

    status = "CANARY_ERROR"
    exit_code = 1

    def __init__(self, message: str, *, endpoint: str | None = None) -> None:
        self.endpoint = endpoint
        super().__init__(message)


class BinanceAPISchemaIncompatibleError(BinanceCanaryError):
    """The live public API no longer matches a production parser contract."""

    status = "API_SCHEMA_INCOMPATIBLE"
    exit_code = 2


class BinanceAPITemporarilyUnavailableError(BinanceCanaryError):
    """The live public API or network is temporarily unavailable."""

    status = "API_TEMPORARILY_UNAVAILABLE"
    exit_code = 3


class BinanceAPIAccessRestrictedError(BinanceCanaryError):
    """The public API explicitly restricted access to the request."""

    status = "API_ACCESS_RESTRICTED"
    exit_code = 4

def _request_json(
    path: str,
    params: dict[str, str],
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Any:
    """Perform one bounded unauthenticated GET; never retry or follow redirects."""
    try:
        url = build_binance_public_url(path, params)
    except BinancePublicRequestError as error:
        raise BinanceAPISchemaIncompatibleError(
            "API_SCHEMA_INCOMPATIBLE: canary request is outside the reviewed public allowlist",
            endpoint=path,
        ) from error
    try:
        with open_public_binance_url(url, timeout=timeout) as response:
            body = response.read()
    except HTTPError as error:
        if error.code == 451:
            raise BinanceAPIAccessRestrictedError(
                "API_ACCESS_RESTRICTED: Binance returned HTTP 451", endpoint=path
            ) from error
        if error.code in {418, 429} or 500 <= error.code <= 599:
            raise BinanceAPITemporarilyUnavailableError(
                f"API_TEMPORARILY_UNAVAILABLE: Binance returned HTTP {error.code}",
                endpoint=path,
            ) from error
        if error.code == 400 and path == REFERENCE_PRICE_PATH:
            try:
                error_payload = json.loads(error.read(64_000).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = None
            except (HTTPException, OSError, URLError) as transport_error:
                raise BinanceAPITemporarilyUnavailableError(
                    "API_TEMPORARILY_UNAVAILABLE: reading Binance error response failed at the transport layer",
                    endpoint=path,
                ) from transport_error
            if (
                type(error_payload) is dict
                and type(error_payload.get("code")) is int
                and error_payload["code"] == -2043
            ):
                return None
        raise BinanceAPISchemaIncompatibleError(
            f"API_SCHEMA_INCOMPATIBLE: Binance returned HTTP {error.code}",
            endpoint=path,
        ) from error
    except (HTTPException, URLError, TimeoutError, OSError) as error:
        raise BinanceAPITemporarilyUnavailableError(
            "API_TEMPORARILY_UNAVAILABLE: Binance public request failed at the transport layer",
            endpoint=path,
        ) from error
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BinanceAPISchemaIncompatibleError(
            "API_SCHEMA_INCOMPATIBLE: Binance returned malformed JSON",
            endpoint=path,
        ) from error


def run_canary(
    *,
    get_json: Callable[..., Any] | None = None,
    acquired_at: pd.Timestamp | datetime | None = None,
) -> dict[str, object]:
    """Validate only the two live public response schemas used by production."""
    request = get_json or _request_json
    timestamp = pd.Timestamp(acquired_at or datetime.now(timezone.utc))
    if timestamp.tzinfo is None:
        raise BinanceAPISchemaIncompatibleError(
            "API_SCHEMA_INCOMPATIBLE: canary receipt timestamp must be timezone-aware"
        )
    timestamp = timestamp.tz_convert("UTC")

    try:
        reference = parse_binance_reference_price(
            symbol="BTC/USDT",
            native_symbol=CANARY_SYMBOL,
            payload=request(
                REFERENCE_PRICE_PATH,
                {"symbol": CANARY_SYMBOL},
                timeout=REQUEST_TIMEOUT_SECONDS,
            ),
            acquired_at=timestamp,
        )
    except BinanceCanaryError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise BinanceAPISchemaIncompatibleError(
            "API_SCHEMA_INCOMPATIBLE: referencePrice response schema changed",
            endpoint=REFERENCE_PRICE_PATH,
        ) from error

    try:
        execution_rules = parse_binance_price_range_execution_rule(
            symbol="BTC/USDT",
            native_symbol=CANARY_SYMBOL,
            payload=request(
                EXECUTION_RULES_PATH,
                {"symbol": CANARY_SYMBOL},
                timeout=REQUEST_TIMEOUT_SECONDS,
            ),
            acquired_at=timestamp,
        )
    except BinanceCanaryError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise BinanceAPISchemaIncompatibleError(
            "API_SCHEMA_INCOMPATIBLE: executionRules response schema changed",
            endpoint=EXECUTION_RULES_PATH,
        ) from error

    return {
        "status": "PASS",
        "checked_at_utc": timestamp.isoformat(),
        "symbol": CANARY_SYMBOL,
        "reference_price_status": reference.source,
        "execution_rules_status": execution_rules.status,
        "endpoint_statuses": {
            REFERENCE_PRICE_PATH: "PARSED",
            EXECUTION_RULES_PATH: "PARSED",
        },
    }


def main() -> int:
    try:
        result = run_canary()
        result["exit_code"] = 0
        print(json.dumps(result, indent=2, sort_keys=True))
    except BinanceCanaryError as error:
        print(
            json.dumps(
                {
                    "status": error.status,
                    "exit_code": error.exit_code,
                    "failed_endpoint": error.endpoint,
                    "error": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return error.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
