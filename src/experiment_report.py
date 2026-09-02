"""Controlled-experiment walk-forward and robustness reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _write_records(records: list[dict[str, Any]], path: Path) -> None:
    frame = pd.json_normalize(records, sep=".") if records else pd.DataFrame()
    frame.to_parquet(path, index=False)


def _metric(record: dict[str, Any], key: str) -> float:
    return float(record.get("metrics", {}).get(key, 0.0))


def write_experiment_reports(
    output_dir: Path, summary: dict[str, Any]
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_json = output_dir / "experiment_summary.json"
    summary_json.write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    training_path = output_dir / "training_trials.parquet"
    validation_path = output_dir / "validation_trials.parquet"
    walk_forward_path = output_dir / "walk_forward_trials.parquet"
    _write_records(summary.get("training_records", []), training_path)
    _write_records(summary.get("validation_records", []), validation_path)
    _write_records(summary.get("walk_forward_records", []), walk_forward_path)

    lock_path = output_dir / "candidate_lock.json"
    lock_path.write_text(
        json.dumps(summary["locked_candidate"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    final_test_path = output_dir / "final_test.json"
    final_test_path.write_text(
        json.dumps(summary["final_test"], indent=2, sort_keys=True), encoding="utf-8"
    )

    walk_forward_report = output_dir / "walk_forward_validation.md"
    wf_lines = [
        "# Rolling pre-test evaluation",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Finalists: `{len(summary.get('finalists', []))}`",
        "- Historical artifact fields and filenames retain the legacy `walk_forward` compatibility label.",
        "- Final test data was excluded from every rolling pre-test fold.",
        "- Fees and adverse slippage are included in every evaluation.",
        "",
        "| Candidate | Fold | Start | End | Penalized score | CAGR | Sharpe | Max drawdown | Turnover |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for record in summary.get("walk_forward_records", []):
        wf_lines.append(
            f"| {record['candidate_id']} | {record.get('fold', '')} | "
            f"{record.get('period', {}).get('start', '')} | "
            f"{record.get('period', {}).get('end', '')} | "
            f"{float(record.get('penalized_score', 0.0)):.4f} | "
            f"{_metric(record, 'cagr'):.2%} | {_metric(record, 'sharpe'):.3f} | "
            f"{_metric(record, 'max_drawdown'):.2%} | {_metric(record, 'turnover'):.3f} |"
        )
    walk_forward_report.write_text("\n".join(wf_lines) + "\n", encoding="utf-8")

    robustness_report = output_dir / "robustness_report.md"
    locked = summary["locked_candidate"]
    test = summary["final_test"]
    robust_lines = [
        "# Robustness and controlled-selection report",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Unique grid configurations: **{summary['unique_candidate_count']}**",
        f"- Candidate backtest trials: **{summary['candidate_trial_count']}**",
        f"- Immutable ledger records: **{summary['ledger_record_count']}**",
        f"- Live promotion: **{'enabled' if summary.get('live_promotion') else 'disabled'}**",
        "- Final test access: only after the candidate-lock ledger event.",
        "",
        "## Scoring rule",
        "",
        str(summary.get("scoring_rule", "")),
        "",
        "## Stable-region finalists",
        "",
        "| Candidate | Training region score | Validation score | Rolling pre-test median | Instability penalty | Lock score |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for finalist in summary.get("finalists", []):
        robust_lines.append(
            f"| {finalist['candidate_id']} | "
            f"{float(finalist.get('selection_score', 0.0)):.4f} | "
            f"{float(finalist.get('validation_score', 0.0)):.4f} | "
            f"{float(finalist.get('walk_forward_median', 0.0)):.4f} | "
            f"{float(finalist.get('instability_penalty', 0.0)):.4f} | "
            f"{float(finalist.get('lock_score', 0.0)):.4f} |"
        )
    robust_lines.extend(
        [
            "",
            "## Locked candidate",
            "",
            f"- Candidate: `{locked['candidate_id']}`",
            f"- Parameters: `{json.dumps(locked.get('parameters', {}), sort_keys=True)}`",
            f"- Lock score: `{float(locked.get('lock_score', 0.0)):.4f}`",
            "",
            "## Sealed final-test result",
            "",
            f"- CAGR: `{_metric(test, 'cagr'):.2%}`",
            f"- Sharpe: `{_metric(test, 'sharpe'):.3f}`",
            f"- Maximum drawdown: `{_metric(test, 'max_drawdown'):.2%}`",
            f"- Turnover: `{_metric(test, 'turnover'):.3f}`",
            "",
            "> The locked strategy remains research-only. No live-trading promotion is performed.",
            "",
        ]
    )
    robustness_report.write_text("\n".join(robust_lines), encoding="utf-8")
    return {
        "summary_json": summary_json,
        "training_trials": training_path,
        "validation_trials": validation_path,
        "walk_forward_trials": walk_forward_path,
        "candidate_lock": lock_path,
        "final_test": final_test_path,
        "walk_forward_report": walk_forward_report,
        "robustness_report": robustness_report,
    }
