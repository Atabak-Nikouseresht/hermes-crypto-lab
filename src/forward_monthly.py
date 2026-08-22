"""Forward-only monthly paper-performance reporting."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.paper_store import PaperStore


def _iso(value) -> str:
    return pd.Timestamp(value).tz_convert("UTC").isoformat()


def generate_monthly_forward_report(
    store: PaperStore,
    *,
    experiment_id: str,
    report_date: datetime,
    output_dir: Path,
    assets: tuple[str, ...],
    slippage_rate: float,
) -> dict[str, Any]:
    report_ts = pd.Timestamp(report_date).tz_convert("UTC")
    month_end = report_ts.normalize().replace(day=1)
    month_start = month_end - pd.offsets.MonthBegin(1)
    with store.connect(read_only=True) as connection:
        experiment = connection.execute(
            "SELECT started_at_utc FROM forward_experiments WHERE experiment_id=?",
            [experiment_id],
        ).fetchone()
        if experiment is None:
            raise ValueError(f"Unknown forward experiment: {experiment_id}")
        experiment_start = pd.Timestamp(experiment[0]).tz_convert("UTC")
        effective_start = max(month_start, experiment_start)
        anchor = connection.execute(
            """
            SELECT w.run_id, e.snapshot_at_utc, e.cash, e.positions_value, e.equity
            FROM forward_schedule_windows w
            JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
            JOIN equity_snapshots e ON e.run_id=w.run_id
            WHERE x.experiment_id=? AND w.scheduled_for_utc < ?
              AND w.outcome <> 'MISSED_SCHEDULE'
            ORDER BY w.scheduled_for_utc DESC LIMIT 1
            """,
            [experiment_id, effective_start],
        ).fetchone()
        if anchor is None:
            anchor = connection.execute(
                """
                SELECT b.run_id, b.observed_at_utc, e.cash, e.positions_value, b.equity
                FROM forward_baselines b
                JOIN equity_snapshots e ON e.run_id=b.run_id
                WHERE b.experiment_id=?
                """,
                [experiment_id],
            ).fetchone()
        windows = connection.execute(
            """
            SELECT w.schedule_key, w.scheduled_for_utc, w.run_id, w.outcome
            FROM forward_schedule_windows w
            JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
            WHERE x.experiment_id=? AND w.scheduled_for_utc >= ? AND w.scheduled_for_utc < ?
            ORDER BY w.scheduled_for_utc
            """,
            [experiment_id, effective_start, month_end],
        ).fetchall()
        equities = connection.execute(
            """
            SELECT w.run_id, e.snapshot_at_utc, e.cash, e.positions_value, e.equity
            FROM forward_schedule_windows w
            JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
            JOIN equity_snapshots e ON e.run_id=w.run_id
            WHERE x.experiment_id=? AND w.scheduled_for_utc >= ? AND w.scheduled_for_utc < ?
              AND w.outcome <> 'MISSED_SCHEDULE'
            ORDER BY e.snapshot_at_utc
            """,
            [experiment_id, effective_start, month_end],
        ).fetchall()
        fills = connection.execute(
            """
            SELECT f.run_id, f.side, f.filled_quantity, f.mid_price,
                   f.execution_price, f.fee, f.spread_cost, f.slippage_cost
            FROM paper_fills f
            JOIN forward_schedule_windows w ON w.run_id=f.run_id
            JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
            WHERE x.experiment_id=? AND w.scheduled_for_utc >= ? AND w.scheduled_for_utc < ?
            """,
            [experiment_id, effective_start, month_end],
        ).fetchall()
        observations = connection.execute(
            """
            SELECT o.run_id, o.observed_at_utc, o.symbol, o.price
            FROM forward_market_observations o
            JOIN forward_schedule_windows w ON w.run_id=o.run_id
            JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
            WHERE x.experiment_id=? AND w.scheduled_for_utc >= ? AND w.scheduled_for_utc < ?
            ORDER BY o.observed_at_utc, o.symbol
            """,
            [experiment_id, effective_start, month_end],
        ).fetchall()
        if anchor is not None:
            anchor_observations = connection.execute(
                """
                SELECT run_id, observed_at_utc, symbol, price
                FROM forward_market_observations WHERE run_id=? ORDER BY symbol
                """,
                [anchor[0]],
            ).fetchall()
            observed_run_ids = {row[0] for row in observations}
            if anchor[0] not in observed_run_ids:
                observations = anchor_observations + observations
        forward_incidents = connection.execute(
            """
            SELECT i.incident_type, COUNT(*) FROM forward_incidents i
            JOIN forward_experiment_incidents x ON x.incident_id=i.incident_id
            WHERE x.experiment_id=? AND i.created_at_utc >= ? AND i.created_at_utc < ?
            GROUP BY i.incident_type
            """,
            [experiment_id, effective_start, month_end],
        ).fetchall()
        kill_incidents = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM paper_incidents p
                JOIN forward_schedule_windows w ON w.run_id=p.run_id
                JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
                WHERE x.experiment_id=? AND p.created_at_utc >= ? AND p.created_at_utc < ?
                """,
                [experiment_id, effective_start, month_end],
            ).fetchone()[0]
        )
        reconciliation_failures = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM paper_runs r
                JOIN forward_schedule_windows w ON w.run_id=r.run_id
                JOIN forward_experiment_windows x ON x.schedule_key=w.schedule_key
                WHERE x.experiment_id=? AND r.started_at_utc >= ? AND r.started_at_utc < ?
                  AND (r.status='RECONCILIATION_FAILURE' OR CAST(r.reconciliation AS VARCHAR) LIKE '%\"valid\": false%')
                """,
                [experiment_id, effective_start, month_end],
            ).fetchone()[0]
        )

    scheduled_windows = len(windows)
    missed_windows = sum(1 for row in windows if row[3] == "MISSED_SCHEDULE")
    completed_windows = scheduled_windows - missed_windows
    weeks_in_cash = sum(1 for row in equities if abs(float(row[3])) <= 1e-9)
    equity_run_ids = {row[0] for row in equities}
    equity_sequence = (
        [anchor] if anchor is not None and anchor[0] not in equity_run_ids else []
    ) + list(equities)
    equity_values = pd.Series(
        [float(row[4]) for row in equity_sequence],
        index=pd.DatetimeIndex(
            [pd.Timestamp(row[1]).tz_convert("UTC") for row in equity_sequence]
        ),
        dtype=float,
    )
    if len(equity_values) >= 2:
        net_return = float(equity_values.iloc[-1] / equity_values.iloc[0] - 1.0)
        returns = equity_values.pct_change().dropna()
        volatility = float(returns.std(ddof=1) * math.sqrt(52)) if len(returns) > 1 else 0.0
        sharpe: float | str = (
            float(returns.mean() / returns.std(ddof=1) * math.sqrt(52))
            if len(returns) >= 12 and returns.std(ddof=1) > 0
            else "insufficient sample"
        )
        drawdowns = equity_values / equity_values.cummax() - 1.0
        maximum_drawdown = abs(float(drawdowns.min()))
    else:
        net_return = 0.0
        volatility = 0.0
        sharpe = "insufficient sample"
        maximum_drawdown = 0.0

    total_fees = sum(float(row[5]) for row in fills)
    spread_cost = sum(float(row[6]) for row in fills)
    slippage_cost = sum(float(row[7]) for row in fills)
    total_cost = total_fees + spread_cost + slippage_cost
    starting_equity = float(equity_values.iloc[0]) if not equity_values.empty else store.account()["initial_cash"]
    ending_equity = float(equity_values.iloc[-1]) if not equity_values.empty else starting_equity
    gross_return = (ending_equity + total_cost) / starting_equity - 1.0 if starting_equity else 0.0
    traded_notional = sum(abs(float(row[2]) * float(row[3])) for row in fills)
    mean_equity = float(equity_values.mean()) if not equity_values.empty else starting_equity
    turnover = traded_notional / mean_equity if mean_equity else 0.0

    expected_gap_numerator = 0.0
    expected_gap_denominator = 0.0
    for _run, side, quantity, mid, execution, *_rest in fills:
        expected = float(mid) * (1.0 + slippage_rate if side == "BUY" else 1.0 - slippage_rate)
        weight = abs(float(quantity) * float(mid))
        expected_gap_numerator += abs(float(execution) - expected) / expected * weight
        expected_gap_denominator += weight
    execution_gap = (
        expected_gap_numerator / expected_gap_denominator if expected_gap_denominator else 0.0
    )

    obs_frame = pd.DataFrame(
        observations, columns=["run_id", "timestamp", "symbol", "price"]
    )
    if not obs_frame.empty:
        obs_frame["timestamp"] = pd.to_datetime(obs_frame["timestamp"], utc=True)
        obs_frame = obs_frame.drop_duplicates(
            subset=["timestamp", "symbol"], keep="first"
        )
        pivot = obs_frame.pivot(index="timestamp", columns="symbol", values="price").sort_index()
        pivot = pivot.loc[:, list(assets)].dropna()
    else:
        pivot = pd.DataFrame(columns=list(assets), dtype=float)
    strategy_timestamps = [_iso(value) for value in equity_values.index]
    benchmark_pivot = pivot.reindex(equity_values.index).dropna()
    benchmark_timestamps = [_iso(value) for value in benchmark_pivot.index]
    if len(benchmark_pivot) >= 2:
        btc_return = float(
            benchmark_pivot["BTC/USDT"].iloc[-1] / benchmark_pivot["BTC/USDT"].iloc[0] - 1.0
        )
        equal_weight_return = float(
            (benchmark_pivot.iloc[-1] / benchmark_pivot.iloc[0]).mean() - 1.0
        )
    else:
        btc_return = 0.0
        equal_weight_return = 0.0

    incidents = {key: int(value) for key, value in forward_incidents}
    result: dict[str, Any] = {
        "data_scope": "forward_only",
        "backtest_rows_used": 0,
        "experiment_id": experiment_id,
        "forward_experiment_start_utc": experiment_start.isoformat(),
        "period_start_utc": effective_start.isoformat(),
        "period_end_utc": month_end.isoformat(),
        "scheduled_windows": scheduled_windows,
        "completed_windows": completed_windows,
        "missed_windows": missed_windows,
        "paper_trades": len({row[0] for row in fills}),
        "fills": len(fills),
        "weeks_in_cash": weeks_in_cash,
        "gross_return": round(float(gross_return), 12),
        "net_return": round(float(net_return), 12),
        "fees": total_fees,
        "spread_cost": spread_cost,
        "slippage_cost": slippage_cost,
        "turnover": turnover,
        "volatility": volatility,
        "sharpe": sharpe,
        "maximum_drawdown": maximum_drawdown,
        "btc_buy_hold_return": btc_return,
        "equal_weight_return": equal_weight_return,
        "operational_incidents": incidents,
        "kill_switch_incidents": kill_incidents,
        "reconciliation_failures": reconciliation_failures,
        "expected_vs_observed_execution_gap": execution_gap,
        "strategy_timestamps": strategy_timestamps,
        "btc_benchmark_timestamps": benchmark_timestamps,
        "equal_weight_benchmark_timestamps": benchmark_timestamps,
        "sample_warning": "Insufficient forward observations for profitability claims",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"forward_monthly_{month_start.strftime('%Y-%m')}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError(f"Monthly report already exists for {month_start.strftime('%Y-%m')}")
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    markdown = [
        "# Monthly forward paper-performance report",
        "",
        f"- Experiment: `{experiment_id}`",
        f"- Forward start: `{experiment_start.isoformat()}`",
        f"- Period: `{effective_start.isoformat()}` to `{month_end.isoformat()}`",
        "- Scope: **forward observations only; historical backtest rows used: 0**",
        f"- Scheduled/completed/missed windows: **{scheduled_windows}/{completed_windows}/{missed_windows}**",
        f"- Trades/fills/weeks in cash: **{result['paper_trades']}/{len(fills)}/{weeks_in_cash}**",
        f"- Gross return: **{gross_return:.2%}**",
        f"- Net return: **{net_return:.2%}**",
        f"- Fees/spread/slippage: **{total_fees:.4f}/{spread_cost:.4f}/{slippage_cost:.4f} USDT**",
        f"- Turnover: **{turnover:.3f}**",
        f"- Volatility: **{volatility:.2%}**",
        f"- Sharpe: **{sharpe if isinstance(sharpe, str) else f'{sharpe:.3f}'}**",
        f"- Maximum drawdown: **{maximum_drawdown:.2%}**",
        f"- BTC buy-and-hold return (identical timestamps): **{btc_return:.2%}**",
        f"- Equal-weight return (identical timestamps): **{equal_weight_return:.2%}**",
        f"- Execution gap vs locked backtest convention: **{execution_gap:.4%}**",
        f"- Operational incidents: `{json.dumps(incidents, sort_keys=True)}`",
        f"- Kill-switch incidents/reconciliation failures: **{kill_incidents}/{reconciliation_failures}**",
        "",
        "> Insufficient forward observations for profitability claims. Human review is required.",
        "",
    ]
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")
    result["report_path"] = markdown_path
    result["json_path"] = json_path
    return result
