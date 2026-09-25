import pandas as pd
import pytest

from src.paper_broker import PaperConfig
from src.schedule import schedule_timestamps


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
