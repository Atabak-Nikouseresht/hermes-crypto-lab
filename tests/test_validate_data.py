from datetime import datetime, timezone

import pandas as pd

from src.validate_data import clean_ohlcv, validate_ohlcv


def _frame(rows):
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def test_validation_detects_missing_duplicate_invalid_ohlc_and_non_positive_price():
    frame = _frame(
        [
            [datetime(2024, 1, 1, tzinfo=timezone.utc), 10, 12, 9, 11, 100],
            [datetime(2024, 1, 1, tzinfo=timezone.utc), 10, 12, 9, 11, 100],
            [datetime(2024, 1, 3, tzinfo=timezone.utc), 11, 10, 12, 0, 100],
        ]
    )

    result = validate_ohlcv(frame)

    assert result.summary["missing_dates"] == 1
    assert result.summary["duplicate_rows"] == 2
    assert result.summary["invalid_ohlc_rows"] == 1
    assert result.summary["non_positive_price_rows"] == 1
    assert result.is_valid is False


def test_cleaning_sorts_deduplicates_and_removes_invalid_rows():
    frame = _frame(
        [
            [datetime(2024, 1, 2, tzinfo=timezone.utc), 11, 13, 10, 12, 100],
            [datetime(2024, 1, 1, tzinfo=timezone.utc), 10, 12, 9, 11, 100],
            [datetime(2024, 1, 1, tzinfo=timezone.utc), 10, 12, 9, 11, 100],
            [datetime(2024, 1, 3, tzinfo=timezone.utc), -1, 2, -2, 1, 100],
        ]
    )

    cleaned = clean_ohlcv(frame)

    assert list(cleaned["timestamp"]) == [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
    ]
    assert str(cleaned["timestamp"].dtype) == "datetime64[ns, UTC]"
