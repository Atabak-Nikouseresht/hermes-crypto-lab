from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from scripts.check_binance_public_api import (
    BinanceAPIIncompatibleError,
    BinanceAPIUnavailableError,
    _request_json,
    run_canary,
)


SYMBOL = "BTCUSDT"
NOW = pd.Timestamp("2026-09-28T00:00:00Z")
NOW_MS = int(NOW.timestamp() * 1000)


def _reference_payload(**overrides):
    return {
        "symbol": SYMBOL,
        "referencePrice": "67500.125",
        "timestamp": NOW_MS,
        **overrides,
    }


def _execution_rules_payload(**overrides):
    return {
        "symbolRules": [
            {
                "symbol": SYMBOL,
                "rules": [
                    {
                        "ruleType": "PRICE_RANGE",
                        "bidLimitMultUp": "1.2",
                        "bidLimitMultDown": "0.8",
                        "askLimitMultUp": "1.2",
                        "askLimitMultDown": "0.8",
                    }
                ],
            }
        ],
        "timestamp": NOW_MS,
        **overrides,
    }


def test_canary_validates_both_runtime_schemas_and_only_two_public_endpoints():
    calls = []

    def get_json(path, params, *, timeout):
        calls.append((path, params, timeout))
        if path == "/api/v3/referencePrice":
            return _reference_payload()
        if path == "/api/v3/executionRules":
            return _execution_rules_payload()
        raise AssertionError(f"unexpected canary endpoint: {path}")

    result = run_canary(get_json=get_json, acquired_at=NOW)

    assert [call[0] for call in calls] == [
        "/api/v3/referencePrice",
        "/api/v3/executionRules",
    ]
    assert all(call[1] == {"symbol": SYMBOL} for call in calls)
    assert result["symbol"] == SYMBOL
    assert result["reference_price_status"] == "REFERENCE_PRICE"
    assert result["execution_rules_status"] == "PRICE_RANGE_PRESENT"
    assert result["status"] == "PASS"


def test_canary_accepts_optional_reference_price_and_absent_price_range():
    def get_json(path, _params, *, timeout):
        if path == "/api/v3/referencePrice":
            return {"symbol": SYMBOL, "referencePrice": None}
        return {
            "symbolRules": [{"symbol": SYMBOL, "rules": []}],
        }

    result = run_canary(get_json=get_json, acquired_at=NOW)

    assert result["reference_price_status"] == "LAST_FALLBACK"
    assert result["execution_rules_status"] == "PRICE_RANGE_ABSENT"
    assert result["status"] == "PASS"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"symbol": SYMBOL},
        {"symbol": "ETHUSDT", "referencePrice": "1", "timestamp": NOW_MS},
        _reference_payload(referencePrice=0),
        _reference_payload(referencePrice="NaN"),
        _reference_payload(referencePrice="Infinity"),
        _reference_payload(referencePrice="not-a-number"),
        _reference_payload(timestamp="1759017600000"),
        _reference_payload(timestamp=True),
        _reference_payload(timestamp=-1),
    ],
)
def test_canary_rejects_incompatible_reference_price_schema(payload):
    def get_json(_path, _params, *, timeout):
        return payload

    with pytest.raises(BinanceAPIIncompatibleError):
        run_canary(get_json=get_json, acquired_at=NOW)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"symbolRules": []},
        {"symbolRules": [{"symbol": "ETHUSDT", "rules": []}]},
        {"symbolRules": [{"symbol": SYMBOL, "rules": "not-a-list"}]},
        {"symbolRules": [{"symbol": SYMBOL, "rules": [{"ruleType": 1}]}]},
        {"symbolRules": [{"symbol": SYMBOL, "rules": []}], "timestamp": "1"},
        {
            "symbolRules": [
                {
                    "symbol": SYMBOL,
                    "rules": [
                        {"ruleType": "PRICE_RANGE", "bidLimitMultUp": "NaN"}
                    ],
                }
            ]
        },
    ],
)
def test_canary_rejects_incompatible_execution_rules_schema(payload):
    def get_json(path, _params, *, timeout):
        if path == "/api/v3/referencePrice":
            return _reference_payload()
        return payload

    with pytest.raises(BinanceAPIIncompatibleError):
        run_canary(get_json=get_json, acquired_at=NOW)


def test_canary_network_unavailable_and_incompatible_errors_are_distinct(monkeypatch):
    import scripts.check_binance_public_api as canary

    def timeout(*_args, **_kwargs):
        raise TimeoutError("network timeout")

    monkeypatch.setattr(canary, "open_public_binance_url", timeout)
    with pytest.raises(BinanceAPIUnavailableError, match="API_UNAVAILABLE"):
        _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=1)

    def missing_endpoint(*_args, **_kwargs):
        raise HTTPError("https://api.binance.com", 404, "Not Found", {}, BytesIO(b""))

    monkeypatch.setattr(canary, "open_public_binance_url", missing_endpoint)
    with pytest.raises(BinanceAPIIncompatibleError, match="API_INCOMPATIBLE"):
        _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=1)

    def rate_limited(*_args, **_kwargs):
        raise HTTPError("https://api.binance.com", 429, "Too Many Requests", {}, BytesIO(b""))

    monkeypatch.setattr(canary, "open_public_binance_url", rate_limited)
    with pytest.raises(BinanceAPIUnavailableError, match="API_UNAVAILABLE"):
        _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=1)


def test_canary_classifies_http_451_as_access_unavailable(monkeypatch):
    import scripts.check_binance_public_api as canary

    def access_denied(*_args, **_kwargs):
        raise HTTPError(
            "https://api.binance.com", 451, "Unavailable For Legal Reasons", {}, BytesIO(b"")
        )

    monkeypatch.setattr(canary, "open_public_binance_url", access_denied)
    with pytest.raises(BinanceAPIUnavailableError, match="API_UNAVAILABLE.*451"):
        _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=1)


def test_canary_classifies_malformed_json_as_api_incompatible(monkeypatch):
    import scripts.check_binance_public_api as canary

    monkeypatch.setattr(
        canary,
        "open_public_binance_url",
        lambda *_args, **_kwargs: BytesIO(b"not-json"),
    )
    with pytest.raises(BinanceAPIIncompatibleError, match="API_INCOMPATIBLE"):
        _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=1)


def test_canary_classifies_transient_url_errors_as_api_unavailable(monkeypatch):
    import scripts.check_binance_public_api as canary

    monkeypatch.setattr(
        canary,
        "open_public_binance_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(BinanceAPIUnavailableError, match="API_UNAVAILABLE"):
        _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=1)


def test_canary_accepts_the_production_documented_no_reference_http_error(monkeypatch):
    import scripts.check_binance_public_api as canary

    def open_url(url, *, timeout):
        if "/api/v3/referencePrice?" in url:
            raise HTTPError(
                url,
                400,
                "No reference price",
                {},
                BytesIO(b'{"code":-2043,"msg":"No reference price"}'),
            )
        return BytesIO(json.dumps(_execution_rules_payload()).encode("utf-8"))

    monkeypatch.setattr(canary, "open_public_binance_url", open_url)
    result = run_canary(acquired_at=NOW)

    assert result["reference_price_status"] == "LAST_FALLBACK"
    assert result["execution_rules_status"] == "PRICE_RANGE_PRESENT"
    assert result["status"] == "PASS"


def test_canary_does_not_treat_execution_rules_http_2043_as_optional(monkeypatch):
    import scripts.check_binance_public_api as canary

    monkeypatch.setattr(
        canary,
        "open_public_binance_url",
        lambda url, **_kwargs: (_ for _ in ()).throw(
            HTTPError(
                url,
                400,
                "No reference price",
                {},
                BytesIO(b'{"code":-2043,"msg":"No reference price"}'),
            )
        ),
    )
    with pytest.raises(BinanceAPIIncompatibleError, match="HTTP 400"):
        _request_json("/api/v3/executionRules", {"symbol": SYMBOL}, timeout=1)


def test_canary_network_request_is_bounded_and_uses_exact_public_path(monkeypatch):
    import scripts.check_binance_public_api as canary

    captured = {}

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def open_url(url, *, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return Response(json.dumps(_reference_payload()).encode("utf-8"))

    monkeypatch.setattr(canary, "open_public_binance_url", open_url)
    assert _request_json("/api/v3/referencePrice", {"symbol": SYMBOL}, timeout=9) == _reference_payload()
    assert captured == {
        "url": "https://api.binance.com/api/v3/referencePrice?symbol=BTCUSDT",
        "timeout": 9,
    }
