import math

import pytest

from src.paper_broker import PaperConfig


ASSETS = ("BTC/USDT",)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schedule_weekday": -1}, "schedule_weekday"),
        ({"schedule_weekday": 7}, "schedule_weekday"),
        ({"schedule_hour": -1}, "schedule_hour"),
        ({"schedule_hour": 24}, "schedule_hour"),
        ({"schedule_minute": -1}, "schedule_minute"),
        ({"schedule_minute": 60}, "schedule_minute"),
        ({"execution_target_minute": -1}, "execution_target_minute"),
        ({"execution_target_minute": 60}, "execution_target_minute"),
        ({"schedule_window_minutes": 0}, "schedule_window_minutes"),
        ({"schedule_minute": 5, "execution_target_minute": 4}, "execution_target_minute"),
        (
            {
                "schedule_minute": 5,
                "schedule_window_minutes": 10,
                "execution_target_minute": 16,
            },
            "execution_target_minute",
        ),
        ({"max_data_staleness_minutes": 0}, "max_data_staleness_minutes"),
        ({"max_quote_staleness_minutes": 0}, "max_quote_staleness_minutes"),
        ({"lookback_days": 0}, "lookback_days"),
        ({"quantity_tolerance": 0}, "quantity_tolerance"),
        ({"quantity_tolerance": math.nan}, "quantity_tolerance"),
        ({"rebalance_days": 0}, "rebalance_days"),
        ({"fee_rate": 1.0}, "cost rates"),
    ],
)
def test_paper_config_rejects_operationally_invalid_values(overrides, message):
    with pytest.raises(ValueError, match=message):
        PaperConfig(assets=ASSETS, **overrides)


def test_current_paper_config_defaults_remain_valid():
    PaperConfig(assets=ASSETS)


@pytest.mark.parametrize("value", [True, 1.5, math.inf, "30"])
def test_paper_config_rejects_non_integral_quote_timestamp_skew(value):
    with pytest.raises(ValueError, match="max_quote_timestamp_skew_seconds"):
        PaperConfig(assets=ASSETS, max_quote_timestamp_skew_seconds=value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_abs_daily_return", math.nan),
        ("max_abs_daily_return", math.inf),
        ("max_abs_daily_return", -math.inf),
        ("max_abs_daily_return", 0.0),
        ("max_abs_daily_return", -0.01),
        ("max_abs_daily_return", True),
        ("max_abs_daily_return", "0.75"),
        ("max_abs_daily_return", 1.000001),
        ("max_volume_ratio", math.nan),
        ("max_volume_ratio", math.inf),
        ("max_volume_ratio", -math.inf),
        ("max_volume_ratio", 0.0),
        ("max_volume_ratio", -0.01),
        ("max_volume_ratio", True),
        ("max_volume_ratio", "100"),
    ],
)
def test_paper_config_fails_closed_for_invalid_market_integrity_thresholds(field, value):
    with pytest.raises(ValueError, match=field):
        PaperConfig(assets=ASSETS, **{field: value})


@pytest.mark.parametrize("value", [1e-12, 0.75, 1.0])
def test_paper_config_accepts_finite_bounded_daily_return_threshold(value):
    assert PaperConfig(assets=ASSETS, max_abs_daily_return=value).max_abs_daily_return == value


@pytest.mark.parametrize("value", [1e-12, 100.0, 1_000_000.0])
def test_paper_config_accepts_any_positive_finite_volume_ratio(value):
    assert PaperConfig(assets=ASSETS, max_volume_ratio=value).max_volume_ratio == value
