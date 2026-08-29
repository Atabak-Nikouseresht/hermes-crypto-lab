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
