import pandas as pd
import pytest

from src.currency_reporting import convert_usdt_equity_to_eur


def test_eur_conversion_requires_identical_utc_timestamps_and_decomposes_fx():
    index = pd.DatetimeIndex(["2026-01-01", "2026-01-02"], tz="UTC")
    usdt = pd.Series([2000.0, 2200.0], index=index)
    eur_per_usdt = pd.Series([0.90, 0.80], index=index)

    report = convert_usdt_equity_to_eur(usdt, eur_per_usdt, source="declared-test-source")

    assert report["equity_eur"].tolist() == [1800.0, 1760.0]
    assert report.attrs["source"] == "declared-test-source"
    assert report.attrs["forward_fill_used"] is False
    assert report["usdt_return"].iloc[1] == pytest.approx(0.10)
    assert report["fx_return"].iloc[1] == pytest.approx(0.80 / 0.90 - 1)
    assert report["eur_return"].iloc[1] == pytest.approx(1760 / 1800 - 1)


def test_eur_conversion_rejects_missing_or_non_utc_fx_points():
    index = pd.DatetimeIndex(["2026-01-01", "2026-01-02"], tz="UTC")
    usdt = pd.Series([2000.0, 2100.0], index=index)
    missing = pd.Series([0.9], index=index[:1])
    with pytest.raises(ValueError, match="identical timestamps"):
        convert_usdt_equity_to_eur(usdt, missing, source="test")

    naive = pd.Series([0.9, 0.91], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        convert_usdt_equity_to_eur(usdt, naive, source="test")
