"""Forward-run diagnostics and outcome persistence after paper execution."""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.paper_broker import MarketSnapshot, PaperRunResult, PaperTradingSystem
from src.paper_notifications import NotificationService
from src.paper_report import write_recovered_committed_report
from src.strategy import generate_signal

ERROR_OUTCOMES = {
    "KILL_SWITCH_ACTIVATED",
    "DATA_QUALITY_FAILURE",
    "RECONCILIATION_FAILURE",
    "EXECUTION_ERROR",
}

RECOVERED_INCOMPLETE_OUTCOME = "RECOVERED_COMMITTED_INCOMPLETE_EVIDENCE"
RECOVERED_INCOMPLETE_DELIVERY = "RECOVERED_COMMITTED_INCOMPLETE_DELIVERY"


def recover_committed_forward_evidence(
    system: PaperTradingSystem,
    *,
    now: datetime | pd.Timestamp,
    reports_dir: Path | None = None,
    notification_target: str | None = None,
) -> int:
    """Finalize schedule evidence without replaying committed paper state."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    with system.store.connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT r.run_id, r.schedule_key, d.outcome,
                   (SELECT COUNT(*) FROM forward_market_observations o
                    WHERE o.run_id=r.run_id) AS observation_count,
                   (SELECT COUNT(*) FROM paper_notifications n
                    WHERE n.run_id=r.run_id) AS notification_count
            FROM paper_runs r
            LEFT JOIN paper_run_diagnostics d ON d.run_id=r.run_id
            WHERE r.mode='PAPER' AND r.schedule_key IS NOT NULL
              AND r.status NOT IN ('RUNNING', 'RECOVERED_ABORTED')
              AND EXISTS (SELECT 1 FROM equity_snapshots e WHERE e.run_id=r.run_id)
              AND (
                    d.run_id IS NULL
                 OR NOT EXISTS (
                        SELECT 1 FROM forward_schedule_windows w
                        WHERE w.schedule_key=r.schedule_key)
                 OR (
                        NOT EXISTS (SELECT 1 FROM paper_notifications n
                                    WHERE n.run_id=r.run_id)
                    AND d.outcome <> ?
                    AND EXISTS (SELECT 1 FROM forward_market_observations o
                                WHERE o.run_id=r.run_id)
                    AND NOT EXISTS (SELECT 1 FROM forward_incidents i
                                    WHERE i.run_id=r.run_id
                                      AND i.incident_type=?))
                 OR (
                        ?
                    AND NOT EXISTS (SELECT 1 FROM paper_notifications n
                                    WHERE n.run_id=r.run_id)
                    AND EXISTS (SELECT 1 FROM forward_incidents i
                                WHERE i.run_id=r.run_id
                                  AND i.incident_type IN (?, ?)))
              )
            ORDER BY r.started_at_utc
            """
            ,
            [
                RECOVERED_INCOMPLETE_OUTCOME,
                RECOVERED_INCOMPLETE_DELIVERY,
                bool(reports_dir and notification_target),
                RECOVERED_INCOMPLETE_DELIVERY,
                RECOVERED_INCOMPLETE_OUTCOME,
            ],
        ).fetchall()
    recovered = 0
    for run_id, schedule_key, persisted_outcome, observation_count, notification_count in rows:
        committed_evidence = system.store.committed_forward_evidence(run_id)
        staged_complete = bool(
            committed_evidence is not None
            and set(committed_evidence[1]) == set(system.config.assets)
        )
        canonical_complete = bool(
            persisted_outcome is not None
            and persisted_outcome != RECOVERED_INCOMPLETE_OUTCOME
            and int(observation_count) == len(system.config.assets)
        )
        evidence_complete = canonical_complete or staged_complete
        delivery_incomplete = evidence_complete and int(notification_count) == 0
        outcome = persisted_outcome
        if persisted_outcome is None and staged_complete:
            assert committed_evidence is not None
            diagnostics, observed_prices, observed_at = committed_evidence
            reconciliation = system.store.reconcile()
            account = system.store.account()
            recovered_result = PaperRunResult(
                run_id,
                "EXECUTED" if reconciliation.valid else "KILL_SWITCH",
                reconciliation.message,
            )
            outcome = classify_outcome(system, recovered_result, diagnostics)
            system.store.record_forward_details(
                run_id=run_id,
                outcome=outcome,
                diagnostics=diagnostics,
                observed_prices=observed_prices,
                observed_at=observed_at,
                kill_switch_active=account["status"] != "ACTIVE",
                reconciliation_valid=reconciliation.valid,
            )
        elif persisted_outcome is None:
            outcome = RECOVERED_INCOMPLETE_OUTCOME
            reconciliation = system.store.reconcile()
            account = system.store.account()
            system.store.record_forward_details(
                run_id=run_id,
                outcome=outcome,
                diagnostics={"rejected_orders": system.store.order_rejections(run_id)},
                observed_prices={},
                observed_at=now_ts.to_pydatetime(),
                kill_switch_active=account["status"] != "ACTIVE",
                reconciliation_valid=reconciliation.valid,
            )
        assert outcome is not None
        if evidence_complete:
            system.store.ensure_forward_baseline(run_id=run_id)
        else:
            system.store.ensure_recovered_forward_baseline(run_id=run_id)
        schedule_start = pd.Timestamp(schedule_key).tz_convert("UTC")
        scheduled_for = schedule_start.normalize() + pd.Timedelta(
            hours=system.config.schedule_hour,
            minutes=system.config.execution_target_minute,
        )
        system.store.record_forward_window(
            schedule_key=schedule_key,
            scheduled_for=scheduled_for.to_pydatetime(),
            run_id=run_id,
            outcome=outcome,
            now=now_ts.to_pydatetime(),
        )
        if not evidence_complete:
            system.store.record_forward_incident(
                incident_type=RECOVERED_INCOMPLETE_OUTCOME,
                reason=(
                    "Committed paper state recovered after interruption; exact market "
                    "evidence cannot be reconstructed; a recovery report and notification "
                    "eligibility record are created without fabricated observations"
                ),
                now=now_ts.to_pydatetime(),
                run_id=run_id,
                scheduled_for=scheduled_for.to_pydatetime(),
            )
        elif delivery_incomplete:
            system.store.record_forward_incident(
                incident_type=RECOVERED_INCOMPLETE_DELIVERY,
                reason=(
                    "Forward evidence committed before interruption; report or "
                    "notification completion was not persisted and cannot be assumed"
                ),
                now=now_ts.to_pydatetime(),
                run_id=run_id,
                scheduled_for=scheduled_for.to_pydatetime(),
            )
        if int(notification_count) == 0 and reports_dir is not None:
            report_path = write_recovered_committed_report(
                system.store,
                run_id=run_id,
                outcome=outcome,
                reports_dir=reports_dir,
                now=now_ts,
                locked_candidate_id=system.config.locked_candidate_id,
            )
            if notification_target is not None:
                NotificationService(
                    system.store,
                    target=notification_target,
                    sender=lambda _target, _path: None,
                ).register_pending(run_id, report_path)
        recovered += 1
    return recovered


def commit_operational_failure(
    system: PaperTradingSystem,
    *,
    outcome: str,
    message: str,
    now: datetime | pd.Timestamp,
) -> PaperRunResult:
    """Commit an operational failure as a terminal run without trading."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    run_id = "paper_failure_" + now_ts.strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8]
    schedule_key = system._scheduled_key(now_ts)
    transient_data_halt = outcome == "DATA_QUALITY_FAILURE"
    if transient_data_halt:
        # Invalid public evidence is not a committed schedule outcome: a valid
        # retry remains permissible until the governed window closes.
        schedule_key = None
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
    if not transient_data_halt:
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
    if result.status == "DATA_HALT":
        return "DATA_QUALITY_FAILURE"
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
    if result.status == "DATA_HALT":
        outcome = "DATA_QUALITY_FAILURE"
        return replace(result, outcome=outcome, diagnostics={})
    committed_evidence = system.store.committed_forward_evidence(result.run_id)
    if committed_evidence is not None:
        diagnostics, observed_prices, observed_at = committed_evidence
    else:
        diagnostics = diagnostics or {}
        observed_prices = {
            asset: snapshot.quotes[asset].mid for asset in system.config.assets
        }
        observed_at = now_ts.to_pydatetime()
    persisted_rejections = system.store.order_rejections(result.run_id)
    if persisted_rejections:
        diagnostics["rejected_orders"] = persisted_rejections
    outcome = outcome_override or classify_outcome(system, result, diagnostics)
    reconciliation = system.store.reconcile()
    account = system.store.account()
    system.store.record_forward_details(
        run_id=result.run_id,
        outcome=outcome,
        diagnostics=diagnostics,
        observed_prices=observed_prices,
        observed_at=observed_at,
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
