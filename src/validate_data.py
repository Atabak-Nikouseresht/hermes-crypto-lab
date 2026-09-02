from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
PRICE_COLUMNS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class ValidationResult:
    summary: dict[str, int]
    missing_dates: list[str]

    @property
    def is_valid(self) -> bool:
        return not any(self.summary.values())


def rows_to_frame(rows: list[list[float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    for column in COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _masks(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    non_positive = frame[PRICE_COLUMNS].le(0).any(axis=1)
    invalid_ohlc = (
        frame["high"].lt(frame[["open", "close", "low"]].max(axis=1))
        | frame["low"].gt(frame[["open", "close", "high"]].min(axis=1))
        | frame[COLUMNS].isna().any(axis=1)
    )
    return non_positive, invalid_ohlc


def validate_ohlcv(frame: pd.DataFrame) -> ValidationResult:
    if frame.empty:
        return ValidationResult(
            summary={
                "missing_dates": 0,
                "duplicate_rows": 0,
                "invalid_ohlc_rows": 0,
                "non_positive_price_rows": 0,
                "empty_dataset": 1,
                "misaligned_timestamp_rows": 0,
                "invalid_volume_rows": 0,
                "non_finite_rows": 0,
            },
            missing_dates=[],
        )
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    unique_days = pd.DatetimeIndex(timestamps.dt.normalize().drop_duplicates().sort_values())
    expected = pd.date_range(unique_days.min(), unique_days.max(), freq="D", tz="UTC")
    missing = expected.difference(unique_days)
    non_positive, invalid_ohlc = _masks(frame.assign(timestamp=timestamps))
    numeric = frame[COLUMNS[1:]].apply(pd.to_numeric, errors="coerce")
    non_finite = ~np.isfinite(numeric).all(axis=1)
    invalid_volume = numeric["volume"].lt(0)
    misaligned = timestamps.ne(timestamps.dt.normalize())
    return ValidationResult(
        summary={
            "missing_dates": len(missing),
            "duplicate_rows": int(timestamps.duplicated(keep=False).sum()),
            "invalid_ohlc_rows": int(invalid_ohlc.sum()),
            "non_positive_price_rows": int(non_positive.sum()),
            "empty_dataset": 0,
            "misaligned_timestamp_rows": int(misaligned.sum()),
            "invalid_volume_rows": int(invalid_volume.sum()),
            "non_finite_rows": int(non_finite.sum()),
        },
        missing_dates=[value.isoformat() for value in missing],
    )


def clean_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    cleaned = frame.copy()
    cleaned["timestamp"] = pd.to_datetime(cleaned["timestamp"], utc=True)
    for column in COLUMNS[1:]:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
    cleaned = cleaned.sort_values("timestamp").drop_duplicates("timestamp", keep="first")
    non_positive, invalid_ohlc = _masks(cleaned)
    numeric = cleaned[COLUMNS[1:]]
    non_finite = ~np.isfinite(numeric).all(axis=1)
    invalid_volume = numeric["volume"].lt(0)
    misaligned = cleaned["timestamp"].ne(cleaned["timestamp"].dt.normalize())
    cleaned = cleaned.loc[
        ~(non_positive | invalid_ohlc | non_finite | invalid_volume | misaligned)
    ].reset_index(drop=True)
    return cleaned[COLUMNS]
