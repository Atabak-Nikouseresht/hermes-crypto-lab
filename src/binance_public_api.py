"""Strict credential-free transport for direct Binance public REST calls."""

from __future__ import annotations

from collections.abc import Mapping
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BINANCE_PUBLIC_HOST = "api.binance.com"
BINANCE_PUBLIC_BASE_URL = f"https://{BINANCE_PUBLIC_HOST}"
ALLOWED_BINANCE_PUBLIC_HOSTS = frozenset({BINANCE_PUBLIC_HOST})
ALLOWED_BINANCE_PUBLIC_PATH_QUERY_KEYS = {
    "/api/v3/referencePrice": frozenset({"symbol"}),
    "/api/v3/executionRules": frozenset({"symbol"}),
    # Used only by the independent, read-only public-data cross-check.
    "/api/v3/klines": frozenset({"symbol", "interval", "startTime", "limit"}),
}
ALLOWED_BINANCE_PUBLIC_PATHS = frozenset(ALLOWED_BINANCE_PUBLIC_PATH_QUERY_KEYS)
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_UNSIGNED_INTEGER = re.compile(r"^[0-9]+$")
_ALLOWED_REQUEST_HEADERS = frozenset({"user-agent"})


class BinancePublicRequestError(ValueError):
    """Raised when a direct request falls outside the public REST allowlist."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent an allowed Binance URL from redirecting to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _header_items(headers: object) -> list[tuple[str, object]]:
    if headers is None:
        return []
    if isinstance(headers, Request):
        return list(headers.header_items())
    if not isinstance(headers, Mapping):
        raise BinancePublicRequestError("Binance request headers must be a mapping")
    return list(headers.items())


def _validate_headers(headers: object) -> None:
    for name, _value in _header_items(headers):
        if type(name) is not str or not name.strip():
            raise BinancePublicRequestError("Binance request header name is malformed")
        if name.casefold() not in _ALLOWED_REQUEST_HEADERS:
            raise BinancePublicRequestError(
                "credential or unapproved Binance request headers are prohibited"
            )


def _validate_query(path: str, query: str) -> None:
    expected_keys = ALLOWED_BINANCE_PUBLIC_PATH_QUERY_KEYS[path]
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(expected_keys) + 1,
        ) if query else []
    except ValueError as error:
        raise BinancePublicRequestError("Binance request query is malformed") from error
    names = [key for key, _value in pairs]
    if len(names) != len(set(names)):
        raise BinancePublicRequestError("duplicate Binance request query parameters are prohibited")
    if any(
        key.casefold().replace("-", "").replace("_", "")
        in {"apikey", "signature", "timestamp", "recvwindow", "secret", "token", "listenkey"}
        for key in names
    ):
        raise BinancePublicRequestError("credential or signed Binance query parameters are prohibited")
    if set(names) != set(expected_keys):
        raise BinancePublicRequestError("Binance request query parameters differ from the public allowlist")
    values = dict(pairs)
    symbol = values.get("symbol")
    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        raise BinancePublicRequestError("Binance public symbol parameter is malformed")
    if path == "/api/v3/klines":
        if (
            values.get("interval") != "1d"
            or values.get("limit") != "1"
            or type(values.get("startTime")) is not str
            or _UNSIGNED_INTEGER.fullmatch(values["startTime"]) is None
        ):
            raise BinancePublicRequestError("Binance kline query differs from the required public audit request")


def validate_binance_public_request(
    url: object,
    *,
    method: str | None = None,
    headers: object = None,
) -> None:
    """Validate exact HTTPS origin, REST path, query, method, and credential surface."""
    if isinstance(url, Request):
        request = url
        raw_url = request.full_url
        actual_method = request.get_method()
        actual_headers = request
    else:
        raw_url = url
        actual_method = "GET" if method is None else method
        actual_headers = headers
    if type(raw_url) is not str or not raw_url:
        raise BinancePublicRequestError("Binance public request URL is missing or malformed")
    if type(actual_method) is not str or actual_method != "GET":
        raise BinancePublicRequestError("only GET is permitted for direct Binance public requests")
    _validate_headers(actual_headers)
    try:
        parsed = urlsplit(raw_url)
        host = parsed.hostname
    except ValueError as error:
        raise BinancePublicRequestError("Binance public request URL is malformed") from error
    if (
        parsed.scheme != "https"
        or host is None
        or host.casefold() not in ALLOWED_BINANCE_PUBLIC_HOSTS
        or parsed.netloc.casefold() != BINANCE_PUBLIC_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise BinancePublicRequestError("Binance public request origin must be the exact HTTPS allowlist host")
    if parsed.path not in ALLOWED_BINANCE_PUBLIC_PATH_QUERY_KEYS:
        raise BinancePublicRequestError("Binance public request endpoint is not allowlisted")
    _validate_query(parsed.path, parsed.query)


def build_binance_public_url(path: str, params: Mapping[str, object]) -> str:
    """Build one exact allowlisted GET URL without accepting caller-selected hosts."""
    if type(path) is not str or path not in ALLOWED_BINANCE_PUBLIC_PATH_QUERY_KEYS:
        raise BinancePublicRequestError("Binance public endpoint path is not allowlisted")
    if not isinstance(params, Mapping) or set(params) != set(
        ALLOWED_BINANCE_PUBLIC_PATH_QUERY_KEYS[path]
    ):
        raise BinancePublicRequestError("Binance public request parameters differ from the endpoint allowlist")
    normalized: dict[str, str] = {}
    for key, value in params.items():
        if type(key) is not str or type(value) not in (str, int):
            raise BinancePublicRequestError("Binance public request parameter is malformed")
        if type(value) is int:
            value = str(value)
        if not value or any(ord(character) < 32 for character in value):
            raise BinancePublicRequestError("Binance public request parameter is malformed")
        normalized[key] = value
    url = f"{BINANCE_PUBLIC_BASE_URL}{path}?{urlencode(normalized)}"
    validate_binance_public_request(url)
    return url


def open_public_binance_url(
    url: str,
    *,
    timeout: float,
    headers: Mapping[str, str] | None = None,
):
    """Open only an allowlisted unauthenticated Binance request without redirects."""
    try:
        timeout_value = float(timeout)
    except (TypeError, OverflowError, ValueError) as error:
        raise BinancePublicRequestError(
            "Binance request timeout must be finite and positive"
        ) from error
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout_value)
        or not 0 < timeout_value <= 120
    ):
        raise BinancePublicRequestError("Binance request timeout must be finite and positive")
    validate_binance_public_request(url, headers=headers)
    request = Request(url, headers=dict(headers or {}), method="GET")
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)
