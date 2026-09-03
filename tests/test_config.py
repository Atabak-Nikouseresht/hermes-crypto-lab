from __future__ import annotations

import math

import pytest

from src.config import load_canonical_research_config, load_settings


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("HCL_FETCH_LIMIT", "1", 1),
        ("HCL_MAX_RETRIES", "0", 0),
        ("HCL_BACKOFF_BASE_SECONDS", "0.001", 0.001),
        ("HCL_REQUEST_TIMEOUT_MS", "1", 1),
    ],
)
def test_runtime_setting_valid_boundaries(monkeypatch, tmp_path, name, value, expected):
    monkeypatch.setenv(name, value)

    settings = load_settings(tmp_path)

    field = {
        "HCL_FETCH_LIMIT": "fetch_limit",
        "HCL_MAX_RETRIES": "max_retries",
        "HCL_BACKOFF_BASE_SECONDS": "backoff_base_seconds",
        "HCL_REQUEST_TIMEOUT_MS": "request_timeout_ms",
    }[name]
    assert getattr(settings, field) == expected


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("HCL_FETCH_LIMIT", "0", "HCL_FETCH_LIMIT must be a positive integer"),
        ("HCL_FETCH_LIMIT", "abc", "HCL_FETCH_LIMIT must be a positive integer"),
        ("HCL_MAX_RETRIES", "-1", "HCL_MAX_RETRIES must be a non-negative integer"),
        ("HCL_MAX_RETRIES", "1.5", "HCL_MAX_RETRIES must be a non-negative integer"),
        (
            "HCL_BACKOFF_BASE_SECONDS",
            "0",
            "HCL_BACKOFF_BASE_SECONDS must be a positive finite number",
        ),
        (
            "HCL_BACKOFF_BASE_SECONDS",
            str(math.inf),
            "HCL_BACKOFF_BASE_SECONDS must be a positive finite number",
        ),
        (
            "HCL_BACKOFF_BASE_SECONDS",
            "nan",
            "HCL_BACKOFF_BASE_SECONDS must be a positive finite number",
        ),
        (
            "HCL_REQUEST_TIMEOUT_MS",
            "0",
            "HCL_REQUEST_TIMEOUT_MS must be a positive integer",
        ),
        (
            "HCL_REQUEST_TIMEOUT_MS",
            "bad",
            "HCL_REQUEST_TIMEOUT_MS must be a positive integer",
        ),
    ],
)
def test_invalid_runtime_setting_fails_before_network(
    monkeypatch, tmp_path, name, value, message
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"^{message}$"):
        load_settings(tmp_path)


def test_canonical_research_config_rejects_exploratory_source_drift(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "assets.yaml").write_text("assets: [BTC/USDT]\n", encoding="utf-8")
    monkeypatch.setenv("HCL_EXCHANGE", "other-public-exchange")

    with pytest.raises(ValueError, match="canonical research exchange"):
        load_canonical_research_config(tmp_path)

    monkeypatch.setenv("HCL_EXCHANGE", "binance")
    monkeypatch.setenv("HCL_TIMEFRAME", "4h")
    with pytest.raises(ValueError, match="canonical research timeframe"):
        load_canonical_research_config(tmp_path)


def test_canonical_research_config_uses_governed_daily_assets(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "assets.yaml").write_text("assets: [BTC/USDT]\n", encoding="utf-8")
    monkeypatch.delenv("HCL_EXCHANGE", raising=False)
    monkeypatch.delenv("HCL_TIMEFRAME", raising=False)

    canonical = load_canonical_research_config(tmp_path)

    assert canonical.settings.timeframe == "1d"
    assert canonical.assets == ("BTC/USDT",)
