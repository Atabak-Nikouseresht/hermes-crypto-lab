"""Currency measurement utilities; never used by strategy or allocation logic."""

from __future__ import annotations

import pandas as pd


def _validate_utc_series(series: pd.Series, name: str) -> pd.Series:
    if not isinstance(series.index, pd.DatetimeIndex) or series.index.tz is None:
        raise ValueError(f"{name} must use a timezone-aware UTC index")
    if str(series.index.tz) != "UTC":
        raise ValueError(f"{name} must use a timezone-aware UTC index")
    values = series.astype(float)
    if values.empty or values.isna().any() or (values <= 0).any():
        raise ValueError(f"{name} must contain finite positive values")
    if not values.index.is_monotonic_increasing or values.index.has_duplicates:
        raise ValueError(f"{name} timestamps must be unique and increasing")
    return values


def convert_usdt_equity_to_eur(
    equity_usdt: pd.Series,
    eur_per_usdt: pd.Series,
    *,
    source: str,
) -> pd.DataFrame:
    """Convert on identical UTC timestamps without imputation or forward filling."""
    usdt = _validate_utc_series(equity_usdt, "USDT equity")
    fx = _validate_utc_series(eur_per_usdt, "EUR/USDT conversion series")
    if not usdt.index.equals(fx.index):
        raise ValueError("USDT equity and EUR/USDT conversion must have identical timestamps")
    result = pd.DataFrame(
        {
            "equity_usdt": usdt,
            "eur_per_usdt": fx,
            "equity_eur": usdt * fx,
            "usdt_return": usdt.pct_change(),
            "fx_return": fx.pct_change(),
            "eur_return": (usdt * fx).pct_change(),
        }
    )
    result.attrs.update(
        {
            "source": source,
            "forward_fill_used": False,
            "usdt_label": "USDT-denominated portfolio return",
            "eur_label": "EUR-converted portfolio return",
            "defensive_label": "USDT defensive allocation",
            "usdt_depeg_risk_assumed_zero": False,
        }
    )
    return result
