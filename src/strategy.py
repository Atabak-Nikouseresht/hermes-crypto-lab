"""Look-ahead-safe momentum signal generation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from numbers import Real

import numpy as np
import pandas as pd

from src.portfolio import build_target_weights


@dataclass(frozen=True)
class StrategyConfig:
    momentum_short_days: int = 30
    momentum_long_days: int = 90
    momentum_skip_days: int = 7
    btc_moving_average_days: int = 200
    volatility_days: int = 30
    annualization_days: int = 365
    max_assets: int = 2
    asset_caps: dict[str, float] = field(
        default_factory=lambda: {
            "BTC/USDT": 0.70,
            "ETH/USDT": 0.60,
            "BNB/USDT": 0.40,
            "XRP/USDT": 0.40,
            "TRX/USDT": 0.40,
        }
    )
    altcoins: set[str] = field(
        default_factory=lambda: {"BNB/USDT", "XRP/USDT", "TRX/USDT"}
    )
    max_altcoin_weight: float = 0.60

    def __post_init__(self) -> None:
        positive_integer_fields = (
            "momentum_short_days",
            "momentum_long_days",
            "btc_moving_average_days",
            "volatility_days",
            "max_assets",
        )
        for field_name in positive_integer_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.volatility_days < 2:
            raise ValueError("volatility_days must be at least 2 for ddof=1")
        if (
            isinstance(self.momentum_skip_days, bool)
            or not isinstance(self.momentum_skip_days, int)
            or self.momentum_skip_days < 0
        ):
            raise ValueError("momentum_skip_days must be a nonnegative integer")
        if (
            isinstance(self.annualization_days, bool)
            or not isinstance(self.annualization_days, Real)
            or not math.isfinite(float(self.annualization_days))
            or self.annualization_days <= 0
        ):
            raise ValueError("annualization_days must be a positive finite number")
        if not isinstance(self.asset_caps, dict):
            raise ValueError("asset_caps must be a dictionary")
        for symbol, cap in self.asset_caps.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("asset_caps must contain nonempty symbol strings")
            if (
                isinstance(cap, bool)
                or not isinstance(cap, Real)
                or not math.isfinite(float(cap))
                or not 0.0 <= float(cap) <= 1.0
            ):
                raise ValueError("asset_caps values must be finite values within [0, 1]")
        if not isinstance(self.altcoins, set):
            raise ValueError("altcoins must be a set")
        if any(not isinstance(symbol, str) or not symbol.strip() for symbol in self.altcoins):
            raise ValueError("altcoins must contain nonempty symbol strings")
        if (
            isinstance(self.max_altcoin_weight, bool)
            or not isinstance(self.max_altcoin_weight, Real)
            or not math.isfinite(float(self.max_altcoin_weight))
            or not 0.0 <= float(self.max_altcoin_weight) <= 1.0
        ):
            raise ValueError("max_altcoin_weight must be a finite value within [0, 1]")

    @property
    def required_observations(self) -> int:
        """History for each indexed endpoint, MA levels and volatility returns."""
        return max(
            self.momentum_short_days,
            self.momentum_long_days,
            self.momentum_skip_days,
            self.btc_moving_average_days - 1,
            self.volatility_days,
        ) + 1


@dataclass(frozen=True)
class Signal:
    timestamp: pd.Timestamp
    ranked_assets: tuple[str, ...]
    target_weights: dict[str, float]
    momentum_short: dict[str, float]
    momentum_long_ex_skip: dict[str, float]
    realized_volatility: dict[str, float]
    btc_above_trend_ma: bool


def generate_signal(
    close_prices: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    config: StrategyConfig,
) -> Signal:
    """Generate a signal using rows at or before ``as_of`` only.

    With t the last available row, long momentum excluding skip is exactly
    P[t - skip] / P[t - long] - 1, NOT P[t - skip] / P[t - long - skip] - 1.
    Windows count observations (daily bars), not elapsed calendar time.
    In zero-based iloc from the end, P[t - k] is history.iloc[-(k + 1)].
    Thus skip=0 uses the current close and long requires long+1 observations.
    """
    timestamp = pd.Timestamp(as_of)
    if timestamp.tzinfo is None:
        raise ValueError("as_of must be timezone-aware UTC")
    timestamp = timestamp.tz_convert("UTC")
    history = close_prices.sort_index().loc[:timestamp].copy()
    required = config.required_observations
    if len(history) < required:
        raise ValueError(f"At least {required} observations are required")
    if "BTC/USDT" not in history:
        raise ValueError("BTC/USDT is required for the market regime filter")

    current = history.iloc[-1]
    short_base = history.iloc[-(config.momentum_short_days + 1)]
    long_base = history.iloc[-(config.momentum_long_days + 1)]
    skipped_endpoint = history.iloc[-(config.momentum_skip_days + 1)]
    momentum_short_series = current / short_base - 1.0
    momentum_long_ex_skip_series = skipped_endpoint / long_base - 1.0
    log_returns = np.log(history / history.shift(1))
    volatility = (
        log_returns.iloc[-config.volatility_days :].std(ddof=1)
        * math.sqrt(config.annualization_days)
    ).clip(lower=1e-12)

    btc_ma = history["BTC/USDT"].iloc[-config.btc_moving_average_days :].mean()
    btc_above_ma = bool(
        pd.notna(current["BTC/USDT"])
        and pd.notna(btc_ma)
        and current["BTC/USDT"] > btc_ma
    )
    momentum_short = {asset: float(value) for asset, value in momentum_short_series.items()}
    momentum_long_ex_skip = {
        asset: float(value) for asset, value in momentum_long_ex_skip_series.items()
    }
    realized_volatility = {asset: float(value) for asset, value in volatility.items()}

    if btc_above_ma:
        eligible = [
            asset
            for asset in history.columns
            if math.isfinite(momentum_short.get(asset, math.nan))
            and math.isfinite(momentum_long_ex_skip.get(asset, math.nan))
            and momentum_short[asset] > 0
            and momentum_long_ex_skip[asset] > 0
            and math.isfinite(realized_volatility.get(asset, math.nan))
            and realized_volatility[asset] > 0
        ]
        eligible.sort(key=lambda asset: (-momentum_long_ex_skip[asset], asset))
        ranked = tuple(eligible[: config.max_assets])
    else:
        ranked = ()

    target_weights = build_target_weights(
        ranked_assets=ranked,
        realized_volatility=realized_volatility,
        max_assets=config.max_assets,
        asset_caps=config.asset_caps,
        altcoins=config.altcoins,
        max_altcoin_weight=config.max_altcoin_weight,
    )
    return Signal(
        timestamp=timestamp,
        ranked_assets=ranked,
        target_weights=target_weights,
        momentum_short=momentum_short,
        momentum_long_ex_skip=momentum_long_ex_skip,
        realized_volatility=realized_volatility,
        btc_above_trend_ma=btc_above_ma,
    )
