from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def write_quality_report(
    results: list[dict[str, Any]], reports_dir: Path, run_id: str
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    payload = {"run_id": run_id, "generated_at_utc": generated, "datasets": results}
    json_path = reports_dir / f"data_quality_{run_id}.json"
    markdown_path = reports_dir / f"data_quality_{run_id}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Data-quality report",
        "",
        f"- Run ID: `{run_id}`",
        f"- Generated (UTC): `{generated}`",
        "- Scope: finalized daily Binance spot candles from public endpoints",
        "",
        "| Asset | Raw | Clean | Start UTC | End UTC | Missing dates | Duplicate rows | Invalid OHLC | Non-positive prices |",
        "|---|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    for result in results:
        quality = result["quality"]
        lines.append(
            "| {symbol} | {raw_rows} | {clean_rows} | {start_utc} | {end_utc} | "
            "{missing_dates} | {duplicate_rows} | {invalid_ohlc_rows} | {non_positive_price_rows} |".format(
                **result, **quality
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Checks are performed on normalized raw CCXT rows before cleaning. The processed Parquet files are UTC-sorted, deduplicated by timestamp, and exclude rows with invalid OHLC relationships or non-positive prices. Missing dates are reported, not synthesized or forward-filled.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return markdown_path, json_path
