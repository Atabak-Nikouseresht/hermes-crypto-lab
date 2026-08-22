"""Performance and risk metrics for daily crypto equity curves."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 and math.isfinite(denominator) else 0.0


def _maximum_recovery_duration(equity: pd.Series) -> int:
    peak_value = float(equity.iloc[0])
    peak_timestamp = equity.index[0]
    underwater_peak_timestamp: pd.Timestamp | None = None
    maximum_days = 0
    for timestamp, value in equity.iloc[1:].items():
        value = float(value)
        if value >= peak_value:
            if underwater_peak_timestamp is not None:
                maximum_days = max(
                    maximum_days, (timestamp - underwater_peak_timestamp).days
                )
                underwater_peak_timestamp = None
            peak_value = value
            peak_timestamp = timestamp
        elif underwater_peak_timestamp is None:
            underwater_peak_timestamp = peak_timestamp
    if underwater_peak_timestamp is not None:
        maximum_days = max(
            maximum_days, (equity.index[-1] - underwater_peak_timestamp).days
        )
    return int(maximum_days)


def calculate_performance_metrics(
    equity_curve: pd.DataFrame,
    fills: pd.DataFrame,
    *,
    annualization_days: int = 365,
    cvar_confidence: float = 0.95,
) -> dict[str, float | int]:
    if "equity" not in equity_curve or equity_curve.empty:
        raise ValueError("equity_curve must contain a non-empty equity column")
    equity = equity_curve["equity"].astype(float).sort_index()
    returns = equity.pct_change().dropna()
    elapsed_days = max((equity.index[-1] - equity.index[0]).total_seconds() / 86_400, 0.0)
    if elapsed_days > 0 and equity.iloc[0] > 0 and equity.iloc[-1] > 0:
        cagr = (equity.iloc[-1] / equity.iloc[0]) ** (365.25 / elapsed_days) - 1.0
    else:
        cagr = 0.0

    daily_mean = float(returns.mean()) if not returns.empty else 0.0
    daily_volatility = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    volatility = daily_volatility * math.sqrt(annualization_days)
    sharpe = _safe_ratio(daily_mean * annualization_days, volatility)
    downside = returns.clip(upper=0.0)
    downside_deviation = (
        float(np.sqrt(np.mean(np.square(downside)))) * math.sqrt(annualization_days)
        if not downside.empty
        else 0.0
    )
    sortino = _safe_ratio(daily_mean * annualization_days, downside_deviation)

    running_peak = equity.cummax()
    drawdown = equity / running_peak - 1.0
    max_drawdown = abs(float(drawdown.min()))
    calmar = _safe_ratio(cagr, max_drawdown)
    if returns.empty:
        cvar_95 = 0.0
    else:
        threshold = float(returns.quantile(1.0 - cvar_confidence))
        tail = returns[returns <= threshold]
        cvar_95 = max(0.0, -float(tail.mean())) if not tail.empty else 0.0

    if fills.empty:
        traded_notional = 0.0
        total_fees = 0.0
    else:
        traded_notional = float(
            (fills["filled_quantity"].abs() * fills["execution_price"].abs()).sum()
        )
        total_fees = float(fills["fee"].sum())
    mean_equity = float(equity.mean())
    turnover = traded_notional / mean_equity if mean_equity > 0 else 0.0

    return {
        "cagr": float(cagr),
        "volatility": float(volatility),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": float(max_drawdown),
        "calmar": float(calmar),
        "cvar_95": float(cvar_95),
        "turnover": float(turnover),
        "recovery_duration_days": _maximum_recovery_duration(equity),
        "total_fees": total_fees,
        "ending_equity": float(equity.iloc[-1]),
    }
