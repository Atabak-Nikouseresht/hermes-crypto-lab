"""Forward-run diagnostics and outcome persistence after paper execution."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from src.paper_broker import MarketSnapshot, PaperRunResult, PaperTradingSystem
from src.strategy import generate_signal

ERROR_OUTCOMES = {
    "KILL_SWITCH_ACTIVATED",
    "DATA_QUALITY_FAILURE",
    "RECONCILIATION_FAILURE",
    "EXECUTION_ERROR",
}


def commit_operational_failure(
    system: PaperTradingSystem,
    *,
    outcome: str,
    message: str,
    now: datetime | pd.Timestamp,
) -> PaperRunResult:
    """Commit an operational failure as a terminal run without trading."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    run_id = "paper_failure_" + now_ts.strftime("%Y%m%dT%H%M%S%fZ")
    schedule_key = system._scheduled_key(now_ts)
    if schedule_key and system.store.schedule_exists(schedule_key):
        return PaperRunResult(
            run_id,
            "DUPLICATE_SCHEDULE",
            "Schedule already has a committed run",
            outcome="NO_REBALANCE",
        )
    reconciliation = system.store.reconcile()
    system.store.insert_run(
        run_id=run_id,
        started_at=now_ts.to_pydatetime(),
        mode="PAPER",
        schedule_key=schedule_key,
        signal_timestamp=None,
        data_timestamp=None,
    )
    if outcome in {
        "DATA_QUALITY_FAILURE",
        "RECONCILIATION_FAILURE",
        "KILL_SWITCH_ACTIVATED",
    } and system.store.account()["status"] == "ACTIVE":
        system.store.activate_kill_switch(
            message, run_id=run_id, now=now_ts.to_pydatetime()
        )
    system.store.finish_run(
        run_id=run_id,
        status=outcome,
        completed_at=now_ts.to_pydatetime(),
        message=message,
        reconciliation=reconciliation,
    )
    if schedule_key:
        target = now_ts.normalize() + pd.Timedelta(
            hours=system.config.schedule_hour,
            minutes=system.config.execution_target_minute,
        )
        system.store.record_forward_window(
            schedule_key=schedule_key,
            scheduled_for=target.to_pydatetime(),
            run_id=run_id,
            outcome=outcome,
            now=now_ts.to_pydatetime(),
        )
        scheduled_for = target.to_pydatetime()
    else:
        scheduled_for = None
    system.store.record_forward_incident(
        incident_type=outcome,
        reason=message,
        now=now_ts.to_pydatetime(),
        run_id=run_id,
        scheduled_for=scheduled_for,
    )
    return PaperRunResult(run_id, outcome, message, outcome=outcome)


def build_forward_diagnostics(
    system: PaperTradingSystem, snapshot: MarketSnapshot
) -> dict[str, Any]:
    config = system.config
    signal_timestamp = snapshot.closes.index[-1].tz_convert("UTC")
    signal = generate_signal(
        snapshot.closes, as_of=signal_timestamp, config=config.strategy_config
    )
    _signal_timestamp, proposals = system._proposals(snapshot)
    account = system.store.account()
    positions = system.store.positions()
    mids = {asset: snapshot.quotes[asset].mid for asset in config.assets}
    market_values = {
        asset: positions.get(asset, {}).get("quantity", 0.0) * mids[asset]
        for asset in config.assets
    }
    equity = account["cash"] + sum(market_values.values())
    current_weights = {
        asset: market_values[asset] / equity if equity else 0.0 for asset in config.assets
    }
    current_weights["CASH"] = account["cash"] / equity if equity else 1.0
    target_weights = {
        asset: float(signal.target_weights.get(asset, 0.0)) for asset in config.assets
    }
    target_weights["CASH"] = float(signal.target_weights.get("CASH", 0.0))
    trend_window = config.strategy_config.btc_moving_average_days
    btc = snapshot.closes["BTC/USDT"]
    btc_ma = float(btc.iloc[-trend_window:].mean())
    btc_vs_trend = float(btc.iloc[-1] / btc_ma - 1.0)
    eligibility = {}
    for asset in config.assets:
        eligibility[asset] = bool(
            signal.btc_above_ma200
            and signal.momentum_30.get(asset, math.nan) > 0
            and signal.momentum_90_ex_7.get(asset, math.nan) > 0
            and signal.realized_volatility_30.get(asset, math.nan) > 0
        )
    estimated_notional = sum(
        float(order["requested_quantity"]) * mids[order["symbol"]]
        for order in proposals
    )
    return {
        "signal_timestamp_utc": signal_timestamp.isoformat(),
        "regime": "RISK_ON" if signal.btc_above_ma200 else "RISK_OFF",
        "btc_vs_trend": btc_vs_trend,
        "trend_window": trend_window,
        "momentum": {
            asset: float(signal.momentum_90_ex_7[asset]) for asset in config.assets
        },
        "eligibility": eligibility,
        "selected_assets": list(signal.ranked_assets),
        "current_weights": current_weights,
        "target_weights": target_weights,
        "proposed_orders": proposals,
        "rejected_orders": list(getattr(system, "_last_rejections", [])),
        "turnover": estimated_notional / equity if equity else 0.0,
        "pre_execution_equity": equity,
    }


def classify_outcome(
    system: PaperTradingSystem,
    result: PaperRunResult,
    diagnostics: dict[str, Any],
) -> str:
    if result.status == "KILL_SWITCH":
        message = result.message.lower()
        if "mismatch" in message or "reconcil" in message or "negative persistent" in message:
            return "RECONCILIATION_FAILURE"
        if "data" in message or "quote" in message or "bar" in message:
            return "DATA_QUALITY_FAILURE"
        return "KILL_SWITCH_ACTIVATED"
    if result.status == "DUPLICATE_SCHEDULE":
        return "NO_REBALANCE"
    if result.status == "NO_REBALANCE":
        return "NO_REBALANCE"
    if result.status == "DRY_RUN":
        if not diagnostics.get("selected_assets"):
            return (
                "NO_ELIGIBLE_ASSET"
                if diagnostics.get("proposed_orders")
                else "CASH_ONLY"
            )
        return "NO_REBALANCE"
    if result.status == "EXECUTED":
        with system.store.connect(read_only=True) as connection:
            fill_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_fills WHERE run_id=?", [result.run_id]
                ).fetchone()[0]
            )
        if not diagnostics.get("selected_assets"):
            return (
                "NO_ELIGIBLE_ASSET"
                if diagnostics.get("proposed_orders")
                else "CASH_ONLY"
            )
        if fill_count:
            return "PAPER_TRADE_COMPLETED"
        return "NO_REBALANCE"
    return "EXECUTION_ERROR"


def finalize_forward_run(
    system: PaperTradingSystem,
    result: PaperRunResult,
    snapshot: MarketSnapshot,
    *,
    now: datetime | pd.Timestamp,
    diagnostics: dict[str, Any] | None = None,
    outcome_override: str | None = None,
) -> PaperRunResult:
    """Persist forward diagnostics only after the paper run is terminal."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    with system.store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT status, completed_at_utc, reconciliation FROM paper_runs WHERE run_id=?",
            [result.run_id],
        ).fetchone()
    if row is None or row[0] == "RUNNING" or row[1] is None:
        raise RuntimeError("Cannot finalize forward diagnostics before paper transaction finalization")
    diagnostics = diagnostics or {}
    outcome = outcome_override or classify_outcome(system, result, diagnostics)
    reconciliation = system.store.reconcile()
    account = system.store.account()
    system.store.record_forward_details(
        run_id=result.run_id,
        outcome=outcome,
        diagnostics=diagnostics,
        observed_prices={asset: snapshot.quotes[asset].mid for asset in system.config.assets},
        observed_at=now_ts.to_pydatetime(),
        kill_switch_active=account["status"] != "ACTIVE",
        reconciliation_valid=reconciliation.valid,
    )
    system.store.ensure_forward_baseline(run_id=result.run_id)
    schedule_key = system._scheduled_key(now_ts)
    if schedule_key is not None and result.status != "DRY_RUN":
        target = now_ts.normalize() + pd.Timedelta(
            hours=system.config.schedule_hour,
            minutes=system.config.execution_target_minute,
        )
        system.store.record_forward_window(
            schedule_key=schedule_key,
            scheduled_for=target.to_pydatetime(),
            run_id=result.run_id,
            outcome=outcome,
            now=now_ts.to_pydatetime(),
        )
    if outcome in ERROR_OUTCOMES:
        system.store.record_forward_incident(
            incident_type=outcome,
            reason=result.message,
            now=now_ts.to_pydatetime(),
            run_id=result.run_id,
            scheduled_for=(
                now_ts.normalize()
                + pd.Timedelta(
                    hours=system.config.schedule_hour,
                    minutes=system.config.execution_target_minute,
                )
            ).to_pydatetime()
            if schedule_key is not None
            else None,
        )
    return replace(result, outcome=outcome, diagnostics=diagnostics)
