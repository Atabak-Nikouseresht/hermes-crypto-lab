"""Concise persistent-paper weekly report."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.paper_broker import MarketSnapshot, PaperRunResult
from src.paper_store import PaperStore


def write_weekly_paper_report(
    store: PaperStore,
    result: PaperRunResult,
    snapshot: MarketSnapshot,
    reports_dir: Path,
    *,
    now: pd.Timestamp,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    account = store.account()
    positions = store.positions()
    position_value = sum(
        state["quantity"] * snapshot.quotes[symbol].mid
        for symbol, state in positions.items()
        if symbol in snapshot.quotes
    )
    equity = account["cash"] + position_value
    with store.connect(read_only=True) as connection:
        order_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE run_id=?", [result.run_id]
            ).fetchone()[0]
        )
        fill_summary = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(fee), 0),
                   COALESCE(SUM(spread_cost), 0), COALESCE(SUM(slippage_cost), 0)
            FROM paper_fills WHERE run_id=?
            """,
            [result.run_id],
        ).fetchone()
        open_incidents = int(
            connection.execute(
                "SELECT COUNT(*) FROM paper_incidents WHERE cleared_at_utc IS NULL"
            ).fetchone()[0]
        )
    path = reports_dir / f"paper_weekly_{now.strftime('%Y-%m-%d')}_{result.run_id}.md"
    lines = [
        "# Weekly paper-trading report",
        "",
        f"- Run: `{result.run_id}`",
        f"- Timestamp UTC: `{now.tz_convert('UTC').isoformat()}`",
        f"- Status: **{result.status}**",
        f"- Message: {result.message}",
        "- Execution mode: virtual simulation only — No real exchange orders",
        f"- Account status: **{account['status']}**",
        f"- Open kill-switch incidents: **{open_incidents}**",
        "",
        "## Account",
        "",
        f"- Initial capital: **{account['initial_cash']:,.2f} USDT**",
        f"- Cash: **{account['cash']:,.2f} USDT**",
        f"- Positions value: **{position_value:,.2f} USDT**",
        f"- Equity: **{equity:,.2f} USDT**",
        "",
        "## This run",
        "",
        f"- Proposed orders: **{len(result.proposed_orders)}**",
        f"- Persisted orders: **{order_count}**",
        f"- Fills: **{int(fill_summary[0])}**",
        f"- Fees: **{float(fill_summary[1]):,.4f} USDT**",
        f"- Spread cost: **{float(fill_summary[2]):,.4f} USDT**",
        f"- Slippage cost: **{float(fill_summary[3]):,.4f} USDT**",
        "",
        "## Positions",
        "",
        "| Asset | Quantity | Average cost | Mid price | Market value |",
        "|---|---:|---:|---:|---:|",
    ]
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
            "> Research-only paper trading. A kill switch blocks simulated execution after missing, stale, invalid, or unreconciled state.",
            "",
        ]
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return path
