import pandas as pd
import pytest

from src.paper_broker import PaperConfig
from src.schedule import (
    schedule_target_from_start,
    schedule_timestamps,
    schedule_window_end,
    target_offset_minutes,
)


def test_ordinary_schedule_preserves_current_utc_target_and_window():
    config = PaperConfig(assets=("BTC/USDT",))

    start, target, end = schedule_timestamps(config, pd.Timestamp("2026-01-05", tz="UTC"))

    assert start == pd.Timestamp("2026-01-05T00:05:00Z")
    assert target == pd.Timestamp("2026-01-05T00:10:00Z")
    assert end == pd.Timestamp("2026-01-05T00:20:00Z")


def test_target_rolls_into_next_hour():
    config = PaperConfig(
        assets=("BTC/USDT",),
        schedule_minute=55,
        execution_target_minute=5,
        schedule_window_minutes=20,
    )

    start, target, end = schedule_timestamps(config, pd.Timestamp("2026-01-05", tz="UTC"))

    assert start == pd.Timestamp("2026-01-05T00:55:00Z")
    assert target == pd.Timestamp("2026-01-05T01:05:00Z")
    assert end == pd.Timestamp("2026-01-05T01:15:00Z")


def test_target_rolls_into_next_day():
    config = PaperConfig(
        assets=("BTC/USDT",),
        schedule_hour=23,
        schedule_minute=55,
        execution_target_minute=5,
        schedule_window_minutes=20,
    )

    start, target, end = schedule_timestamps(config, pd.Timestamp("2026-01-05", tz="UTC"))

    assert start == pd.Timestamp("2026-01-05T23:55:00Z")
    assert target == pd.Timestamp("2026-01-06T00:05:00Z")
    assert end == pd.Timestamp("2026-01-06T00:15:00Z")


def test_schedule_timestamps_reject_naive_date():
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_timestamps(PaperConfig(assets=("BTC/USDT",)), pd.Timestamp("2026-01-05"))


@pytest.mark.parametrize(
    ("schedule_minute", "target_minute", "expected"),
    [(5, 10, 5), (10, 10, 0), (55, 5, 10), (0, 59, 59)],
)
def test_target_offset_is_minute_of_hour_relative(
    schedule_minute, target_minute, expected
):
    assert target_offset_minutes(schedule_minute, target_minute) == expected


@pytest.mark.parametrize("value", [True, False, -1, 60, 1.0, "5", None])
def test_target_offset_rejects_non_exact_or_out_of_range_minutes(value):
    with pytest.raises(ValueError, match="exact integer"):
        target_offset_minutes(value, 10)
    with pytest.raises(ValueError, match="exact integer"):
        target_offset_minutes(5, value)


def test_schedule_helpers_validate_identity_and_return_utc_deadlines():
    config = PaperConfig(assets=("BTC/USDT",))
    start = pd.Timestamp("2026-01-05T00:05:00+00:00")

    assert schedule_target_from_start(config, start) == pd.Timestamp(
        "2026-01-05T00:10:00Z"
    )
    assert schedule_window_end(config, start) == pd.Timestamp(
        "2026-01-05T00:20:00Z"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_target_from_start(config, pd.Timestamp("2026-01-05T00:05:00"))
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_window_end(config, pd.Timestamp("2026-01-05T00:05:00"))
    with pytest.raises(ValueError, match="inconsistent"):
        schedule_target_from_start(config, pd.Timestamp("2026-01-05T00:06:00Z"))
    with pytest.raises(ValueError, match="inconsistent"):
        schedule_window_end(config, pd.Timestamp("2026-01-05T00:06:00Z"))
