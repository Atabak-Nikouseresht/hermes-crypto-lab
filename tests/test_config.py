from __future__ import annotations

import math

import pytest

from src.config import load_settings


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
