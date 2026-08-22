"""Deterministic constrained portfolio construction."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping


def build_target_weights(
    *,
    ranked_assets: Iterable[str],
    realized_volatility: Mapping[str, float],
    max_assets: int,
    asset_caps: Mapping[str, float],
    altcoins: set[str],
    max_altcoin_weight: float,
) -> dict[str, float]:
    """Return capped inverse-volatility weights plus an explicit cash weight.

    Caps are not redistributed. Any allocation removed by a per-asset or
    aggregate altcoin cap remains in cash.
    """
    if max_assets < 0:
        raise ValueError("max_assets must be non-negative")
    selected = []
    for asset in ranked_assets:
        volatility = float(realized_volatility.get(asset, math.nan))
        if math.isfinite(volatility) and volatility > 0:
            selected.append(asset)
        if len(selected) == max_assets:
            break
    if not selected:
        return {"CASH": 1.0}

    inverse = {asset: 1.0 / float(realized_volatility[asset]) for asset in selected}
    inverse_total = sum(inverse.values())
    weights = {
        asset: min(inverse[asset] / inverse_total, float(asset_caps.get(asset, 1.0)))
        for asset in selected
    }

    alt_total = sum(weight for asset, weight in weights.items() if asset in altcoins)
    if alt_total > max_altcoin_weight:
        scale = max_altcoin_weight / alt_total
        for asset in list(weights):
            if asset in altcoins:
                weights[asset] *= scale

    invested = sum(weights.values())
    if invested > 1.0 + 1e-12:
        raise ValueError("Portfolio constraints produced leverage")
    weights["CASH"] = max(0.0, 1.0 - invested)
    return weights
