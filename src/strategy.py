"""Look-ahead-safe momentum signal generation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

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


@dataclass(frozen=True)
class Signal:
    timestamp: pd.Timestamp
    ranked_assets: tuple[str, ...]
    target_weights: dict[str, float]
    momentum_30: dict[str, float]
    momentum_90_ex_7: dict[str, float]
    realized_volatility_30: dict[str, float]
    btc_above_ma200: bool


def generate_signal(
    close_prices: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    config: StrategyConfig,
) -> Signal:
    """Generate a signal using rows at or before ``as_of`` only."""
    timestamp = pd.Timestamp(as_of)
    if timestamp.tzinfo is None:
        raise ValueError("as_of must be timezone-aware UTC")
    timestamp = timestamp.tz_convert("UTC")
    history = close_prices.sort_index().loc[:timestamp].copy()
    required = max(
        config.momentum_long_days,
        config.btc_moving_average_days - 1,
        config.volatility_days,
    ) + 1
    if len(history) < required:
        raise ValueError(f"At least {required} observations are required")
    if "BTC/USDT" not in history:
        raise ValueError("BTC/USDT is required for the market regime filter")

    current = history.iloc[-1]
    short_base = history.iloc[-(config.momentum_short_days + 1)]
    long_base = history.iloc[-(config.momentum_long_days + 1)]
    skipped_endpoint = history.iloc[-(config.momentum_skip_days + 1)]
    momentum_30_series = current / short_base - 1.0
    momentum_90_ex_7_series = skipped_endpoint / long_base - 1.0
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
    momentum_30 = {asset: float(value) for asset, value in momentum_30_series.items()}
    momentum_90_ex_7 = {
        asset: float(value) for asset, value in momentum_90_ex_7_series.items()
    }
    realized_volatility = {asset: float(value) for asset, value in volatility.items()}

    if btc_above_ma:
        eligible = [
            asset
            for asset in history.columns
            if math.isfinite(momentum_30.get(asset, math.nan))
            and math.isfinite(momentum_90_ex_7.get(asset, math.nan))
            and momentum_30[asset] > 0
            and momentum_90_ex_7[asset] > 0
            and math.isfinite(realized_volatility.get(asset, math.nan))
            and realized_volatility[asset] > 0
        ]
        eligible.sort(key=lambda asset: (-momentum_90_ex_7[asset], asset))
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
        momentum_30=momentum_30,
        momentum_90_ex_7=momentum_90_ex_7,
        realized_volatility_30=realized_volatility,
        btc_above_ma200=btc_above_ma,
    )
