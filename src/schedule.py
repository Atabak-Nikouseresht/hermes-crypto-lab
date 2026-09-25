"""Central schedule window and execution-target calculations."""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class ScheduleConfig(Protocol):
    schedule_hour: int
    schedule_minute: int
    execution_target_minute: int
    schedule_window_minutes: int


def target_offset_minutes(schedule_minute: int, execution_target_minute: int) -> int:
    """Resolve the target minute within one unambiguous hour-based schedule."""
    for name, value in (
        ("schedule_minute", schedule_minute),
        ("execution_target_minute", execution_target_minute),
    ):
        if type(value) is not int or not 0 <= value <= 59:
            raise ValueError(f"{name} must be an exact integer in [0, 59]")
    return (execution_target_minute - schedule_minute) % 60


def schedule_timestamps(
    config: ScheduleConfig, schedule_date: pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """Return UTC window start, target and end for the schedule's calendar date."""
    day = pd.Timestamp(schedule_date)
    if day.tzinfo is None:
        raise ValueError("Schedule date must be timezone-aware")
    day = day.tz_convert("UTC").normalize()
    start = day + pd.Timedelta(
        hours=config.schedule_hour, minutes=config.schedule_minute
    )
    target = start + pd.Timedelta(
        minutes=target_offset_minutes(
            config.schedule_minute, config.execution_target_minute
        )
    )
    end = start + pd.Timedelta(minutes=config.schedule_window_minutes)
    return start, target, end


def schedule_target_from_start(
    config: ScheduleConfig, schedule_start: pd.Timestamp
) -> pd.Timestamp:
    """Resolve target time from a persisted, timezone-aware schedule identity."""
    start = pd.Timestamp(schedule_start)
    if start.tzinfo is None:
        raise ValueError("Schedule start must be timezone-aware")
    start = start.tz_convert("UTC")
    expected_start, target, _end = schedule_timestamps(config, start)
    if start != expected_start:
        raise ValueError("Schedule start is inconsistent with configured schedule")
    return target


def schedule_window_end(
    config: ScheduleConfig, schedule_start: pd.Timestamp
) -> pd.Timestamp:
    """Resolve the deadline from a persisted, timezone-aware schedule identity."""
    start = pd.Timestamp(schedule_start)
    if start.tzinfo is None:
        raise ValueError("Schedule start must be timezone-aware")
    start = start.tz_convert("UTC")
    expected_start, _target, end = schedule_timestamps(config, start)
    if start != expected_start:
        raise ValueError("Schedule start is inconsistent with configured schedule")
    return end
