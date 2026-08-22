"""Complete weekly reporting for the locked forward paper experiment."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.paper_broker import MarketSnapshot, PaperRunResult
from src.paper_store import PaperStore


def _weights(values: dict[str, float]) -> str:
    return ", ".join(f"{key} {value:.1%}" for key, value in values.items()) or "—"


def write_weekly_paper_report(
    store: PaperStore,
    result: PaperRunResult,
    snapshot: MarketSnapshot,
    reports_dir: Path,
    *,
    now: pd.Timestamp,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    utc_now = pd.Timestamp(now).tz_convert("UTC")
    rome_now = utc_now.tz_convert(ZoneInfo("Europe/Rome"))
    account = store.account()
    positions = store.positions()
    position_value = sum(
        state["quantity"] * snapshot.quotes[symbol].mid
        for symbol, state in positions.items()
        if symbol in snapshot.quotes
    )
    equity = account["cash"] + position_value
    reconciliation = store.reconcile()
    diagnostics = result.diagnostics or {}
    with store.connect(read_only=True) as connection:
        orders = connection.execute(
            """
            SELECT symbol, side, requested_quantity, target_weight, status
            FROM paper_orders WHERE run_id=? ORDER BY symbol
            """,
            [result.run_id],
        ).fetchall()
        fills = connection.execute(
            """
            SELECT symbol, side, filled_quantity, mid_price, execution_price,
                   fee, spread_cost, slippage_cost
            FROM paper_fills WHERE run_id=? ORDER BY symbol
            """,
            [result.run_id],
        ).fetchall()
        fill_summary = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(fee), 0),
                   COALESCE(SUM(spread_cost), 0), COALESCE(SUM(slippage_cost), 0),
                   COALESCE(SUM(ABS(filled_quantity * mid_price)), 0)
            FROM paper_fills WHERE run_id=?
            """,
            [result.run_id],
        ).fetchone()
        open_incidents = int(
            connection.execute(
                """
                SELECT (SELECT COUNT(*) FROM paper_incidents WHERE cleared_at_utc IS NULL)
                     + (SELECT COUNT(*) FROM forward_incidents WHERE resolved_at_utc IS NULL)
                """
            ).fetchone()[0]
        )
        experiment = connection.execute(
            "SELECT MIN(started_at_utc) FROM forward_experiments WHERE status='ACTIVE'"
        ).fetchone()[0]
        if experiment is None:
            equity_rows = connection.execute(
                "SELECT equity FROM equity_snapshots ORDER BY snapshot_at_utc"
            ).fetchall()
        else:
            equity_rows = connection.execute(
                "SELECT equity FROM equity_snapshots WHERE snapshot_at_utc >= ? ORDER BY snapshot_at_utc",
                [experiment],
            ).fetchall()
    equity_history = pd.Series([float(row[0]) for row in equity_rows], dtype=float)
    if equity_history.empty:
        equity_history = pd.Series([account["initial_cash"], equity], dtype=float)
    elif equity_history.iloc[-1] != equity:
        equity_history = pd.concat([equity_history, pd.Series([equity])], ignore_index=True)
    peaks = equity_history.cummax()
    drawdowns = equity_history / peaks - 1.0
    maximum_drawdown = float(drawdowns.min())
    current_drawdown = float(drawdowns.iloc[-1])
    cumulative_return = equity / account["initial_cash"] - 1.0
    actual_turnover = (
        float(fill_summary[4]) / float(diagnostics.get("pre_execution_equity", equity))
        if equity
        else 0.0
    )

    path = reports_dir / f"paper_weekly_{utc_now.strftime('%Y-%m-%d')}_{result.run_id}.md"
    lines = [
        "# Weekly Quant Forward Paper Report",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Outcome: **{result.outcome or result.status}**",
        f"- Internal status: **{result.status}**",
        f"- Timestamp UTC: `{utc_now.isoformat()}`",
        f"- Timestamp Europe/Rome: `{rome_now.isoformat()}`",
        "- Locked candidate: `mw120_sw00_ma150_n2_r07_v30`",
        f"- Account status: **{account['status']}**",
        f"- Market regime: **{diagnostics.get('regime', 'UNAVAILABLE')}**",
        f"- BTC vs MA{diagnostics.get('trend_window', 150)}: **{float(diagnostics.get('btc_vs_trend', 0.0)):+.2%}**",
        f"- Selected assets: **{', '.join(diagnostics.get('selected_assets', [])) or 'none'}**",
        f"- Current weights: **{_weights(diagnostics.get('current_weights', {}))}**",
        f"- Target weights: **{_weights(diagnostics.get('target_weights', {}))}**",
        "",
        "## Deterministic signal diagnostics",
        "",
        "| Asset | 120-day momentum | Eligible |",
        "|---|---:|---|",
    ]
    momentum = diagnostics.get("momentum", {})
    eligibility = diagnostics.get("eligibility", {})
    for asset in snapshot.closes.columns:
        value = momentum.get(asset)
        value_text = f"{float(value):+.2%}" if value is not None else "—"
        lines.append(f"| {asset} | {value_text} | {'yes' if eligibility.get(asset) else 'no'} |")

    lines.extend(
        [
            "",
            "## Proposed orders",
            "",
            "| Asset | Side | Requested quantity | Target weight |",
            "|---|---|---:|---:|",
        ]
    )
    proposed = diagnostics.get("proposed_orders", [])
    for order in proposed:
        lines.append(
            f"| {order['symbol']} | {order['side']} | {float(order['requested_quantity']):.8f} | {float(order['target_weight']):.2%} |"
        )
    if not proposed:
        lines.append("| — | — | 0 | — |")

    lines.extend(
        [
            "",
            "## Simulated fills",
            "",
            "| Asset | Side | Quantity | Mid | Execution | Fee | Spread | Slippage |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for symbol, side, quantity, mid, execution, fee, spread, slippage in fills:
        lines.append(
            f"| {symbol} | {side} | {float(quantity):.8f} | {float(mid):.6f} | "
            f"{float(execution):.6f} | {float(fee):.4f} | {float(spread):.4f} | {float(slippage):.4f} |"
        )
    if not fills:
        lines.append("| — | — | 0 | — | — | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Portfolio and controls",
            "",
            f"- Proposed turnover: **{float(diagnostics.get('turnover', 0.0)):.2%}**",
            f"- Executed turnover: **{actual_turnover:.2%}**",
            f"- Fee / spread / slippage: **{float(fill_summary[1]):.4f} / {float(fill_summary[2]):.4f} / {float(fill_summary[3]):.4f} USDT**",
            f"- Cash: **{account['cash']:,.2f} USDT**",
            f"- Ending equity: **{equity:,.2f} USDT**",
            f"- Cumulative return: **{cumulative_return:+.2%}**",
            f"- Maximum drawdown: **{maximum_drawdown:.2%}**",
            f"- Current drawdown: **{current_drawdown:.2%}**",
            f"- Kill switch: **{'ACTIVE' if account['status'] != 'ACTIVE' else 'inactive'}**",
            f"- Reconciliation: **{'valid' if reconciliation.valid else 'FAILED'} — {reconciliation.message}**",
            f"- Open operational incidents: **{open_incidents}**",
            "",
            "## Positions",
            "",
            "| Asset | Quantity | Average cost | Mid price | Market value |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for symbol in sorted(positions):
        state = positions[symbol]
        mid = snapshot.quotes[symbol].mid
        lines.append(
            f"| {symbol} | {state['quantity']:.8f} | {state['average_cost']:.6f} | "
            f"{mid:.6f} | {state['quantity'] * mid:.2f} |"
        )
    if not positions:
        lines.append("| — | 0 | — | — | 0.00 |")
    lines.extend(
        [
            "",
            f"- Message: {result.message}",
            "",
            "> All transactions are virtual. No real exchange orders, private endpoints, leverage, margin, derivatives, short selling, withdrawals, or LLM-generated signals are used.",
            "",
        ]
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return path


def write_operational_failure_report(
    store: PaperStore,
    result: PaperRunResult,
    reports_dir: Path,
    *,
    now: pd.Timestamp,
    locked_candidate_id: str,
) -> Path:
    """Write a complete no-market-state report for a committed failure run."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    utc_now = pd.Timestamp(now).tz_convert("UTC")
    rome_now = utc_now.tz_convert(ZoneInfo("Europe/Rome"))
    account = store.account()
    reconciliation = store.reconcile()
    path = reports_dir / f"paper_weekly_{utc_now.strftime('%Y-%m-%d')}_{result.run_id}.md"
    lines = [
        "# Weekly Quant Forward Paper Report",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Outcome: **{result.outcome or result.status}**",
        f"- Timestamp UTC: `{utc_now.isoformat()}`",
        f"- Timestamp Europe/Rome: `{rome_now.isoformat()}`",
        f"- Locked candidate: `{locked_candidate_id}`",
        f"- Account status: **{account['status']}**",
        "- Market regime: **UNAVAILABLE**",
        "- BTC vs MA150: **UNAVAILABLE**",
        "- 120-day momentum: **UNAVAILABLE — no valid market snapshot**",
        "- Eligibility: **UNAVAILABLE — no valid market snapshot**",
        "- Selected assets: **none**",
        "- Current weights: **UNAVAILABLE**",
        "- Target weights: **UNAVAILABLE**",
        "- Proposed orders: **0**",
        "- Simulated fills: **0**",
        "- Fee / spread / slippage: **0 / 0 / 0 USDT**",
        "- Turnover: **0.00%**",
        f"- Cash: **{account['cash']:,.2f} USDT**",
        f"- Ending equity: **UNAVAILABLE without a valid quote snapshot**",
        "- Cumulative return / maximum drawdown / current drawdown: **UNAVAILABLE for this failed run**",
        f"- Kill switch: **{'ACTIVE' if account['status'] != 'ACTIVE' else 'inactive'}**",
        f"- Reconciliation: **{'valid' if reconciliation.valid else 'FAILED'} — {reconciliation.message}**",
        f"- Message: {result.message}",
        "",
        "> All transactions are virtual. The failure produced no trade, no backdated execution, and no LLM-generated signal.",
        "",
    ]
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return path
