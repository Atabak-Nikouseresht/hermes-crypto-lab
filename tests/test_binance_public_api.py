from io import BytesIO
from urllib.request import Request

import pytest

from src.binance_public_api import (
    BinancePublicRequestError,
    _NoRedirectHandler,
    build_binance_public_url,
    open_public_binance_url,
    validate_binance_public_request,
)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/v3/referencePrice", {"symbol": "BTCUSDT"}),
        ("/api/v3/executionRules", {"symbol": "BTCUSDT"}),
        (
            "/api/v3/klines",
            {"symbol": "BTCUSDT", "interval": "1d", "startTime": "1", "limit": "1"},
        ),
    ],
)
def test_only_exact_public_binance_rest_endpoints_build_urls(path, params):
    url = build_binance_public_url(path, params)

    assert url.startswith(f"https://api.binance.com{path}?")
    assert validate_binance_public_request(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.binance.com/api/v3/account",
        "https://api.binance.com/api/v3/order?symbol=BTCUSDT",
        "https://api.binance.com/api/v3/referencePrice/evil?symbol=BTCUSDT",
        "http://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT",
        "https://api.binance.com.evil.example/api/v3/referencePrice?symbol=BTCUSDT",
        "https://api.binance.com@evil.example/api/v3/referencePrice?symbol=BTCUSDT",
        "https://user:pass@api.binance.com/api/v3/referencePrice?symbol=BTCUSDT",
        "https://api.binance.com:8443/api/v3/referencePrice?symbol=BTCUSDT",
        "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT&signature=abc",
        "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT&timestamp=1",
        "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT&symbol=ETHUSDT",
        "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT#fragment",
    ],
)
def test_binance_request_validator_rejects_hosts_paths_and_signed_parameters(url):
    with pytest.raises(BinancePublicRequestError):
        validate_binance_public_request(url)


def test_binance_request_validator_rejects_auth_headers_and_non_get_methods():
    url = "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT"
    for headers in (
        {"X-MBX-APIKEY": "not-a-real-key"},
        {"Authorization": "Bearer not-a-real-token"},
        {"X-API-Key": "not-a-real-key"},
        {"X-Key": "not-a-real-key"},
        {"Cookie": "session=not-a-real-cookie"},
        {"X-Password": "not-a-real-password"},
        {"X-Credential": "not-a-real-credential"},
        {"X-Session-ID": "not-a-real-session"},
    ):
        with pytest.raises(BinancePublicRequestError, match="credential"):
            validate_binance_public_request(url, headers=headers)
    with pytest.raises(BinancePublicRequestError, match="GET"):
        validate_binance_public_request(url, method="POST")


def test_public_request_allows_only_the_required_user_agent_header():
    url = "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT"

    validate_binance_public_request(
        url, headers={"User-Agent": "hermes-crypto-lab-audit/1"}
    )


def test_request_object_auth_headers_are_rejected():
    request = Request(
        "https://api.binance.com/api/v3/executionRules?symbol=BTCUSDT",
        headers={"X-MBX-APIKEY": "not-a-real-key"},
    )

    with pytest.raises(BinancePublicRequestError, match="credential"):
        validate_binance_public_request(request)


def test_endpoint_builder_rejects_unknown_path_or_unexpected_parameters():
    with pytest.raises(BinancePublicRequestError, match="allowlist"):
        build_binance_public_url("/api/v3/order", {"symbol": "BTCUSDT"})
    with pytest.raises(BinancePublicRequestError, match="parameters"):
        build_binance_public_url(
            "/api/v3/referencePrice", {"symbol": "BTCUSDT", "timestamp": "1"}
        )


def test_binance_opener_disables_redirects(monkeypatch):
    import src.binance_public_api as api

    captured = {}

    class FakeOpener:
        def open(self, request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return "response"

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(api, "build_opener", fake_build_opener)
    result = open_public_binance_url(
        "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT",
        timeout=7,
    )

    assert result == "response"
    assert captured["timeout"] == 7
    assert len(captured["handlers"]) == 1
    assert isinstance(captured["handlers"][0], _NoRedirectHandler)
    assert captured["request"].get_method() == "GET"


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), True, 10**400])
def test_binance_public_request_timeout_is_bounded(timeout):
    with pytest.raises(BinancePublicRequestError, match="timeout"):
        open_public_binance_url(
            "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT",
            timeout=timeout,
        )


def test_redirect_handler_refuses_external_redirect_target():
    request = Request("https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT")
    handler = _NoRedirectHandler()

    assert handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://evil.example/private",
    ) is None


def test_data_cross_check_binance_rest_uses_the_same_endpoint_allowlist(monkeypatch):
    import scripts.data_cross_check as cross_check
    from src.binance_public_api import validate_binance_public_request

    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"[]"

    def open_public_url(url, *, timeout, headers=None):
        validate_binance_public_request(url, headers=headers)
        calls.append((url, timeout, headers))
        return Response()

    monkeypatch.setattr(cross_check, "open_public_binance_url", open_public_url)
    url = (
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d"
        "&startTime=1&limit=1"
    )
    assert cross_check._get(url) == b"[]"
    assert calls and calls[0][0] == url

    with pytest.raises(BinancePublicRequestError):
        cross_check._get("https://api.binance.com/api/v3/account")
    assert len(calls) == 1


def test_production_public_market_calls_use_exact_allowlisted_paths(monkeypatch):
    import json

    import src.paper_market as paper_market

    calls = []
    responses = [
        {"symbol": "BTCUSDT", "referencePrice": "60000", "timestamp": 1790553600000},
        {"symbolRules": []},
    ]

    def request(url, *, timeout):
        calls.append((url, timeout))
        return BytesIO(json.dumps(responses.pop(0)).encode("utf-8"))

    class Market:
        timeout = 15_000

        @staticmethod
        def market(symbol):
            return {"id": symbol.replace("/", "")}

    monkeypatch.setattr(paper_market, "urlopen", request)
    client = paper_market.PublicMarketClient(Market())

    client.fetch_reference_price("BTC/USDT")
    client.fetch_execution_rules("BTC/USDT")

    assert calls == [
        (
            "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT",
            15.0,
        ),
        (
            "https://api.binance.com/api/v3/executionRules?symbol=BTCUSDT",
            15.0,
        ),
    ]
