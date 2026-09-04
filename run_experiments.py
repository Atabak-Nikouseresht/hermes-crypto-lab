from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from statistics import median, pstdev
from typing import Any

import yaml

from run_backtest import load_run_configuration
from src.config import load_canonical_research_config
from src.experiment_ledger import ExperimentLedger
from src.experiment_manager import (
    Candidate,
    ExperimentGate,
    Period,
    build_chronological_periods,
    generate_candidate_grid,
    select_stable_finalists,
)
from src.experiment_report import write_experiment_reports
from src.experiment_runner import (
    CandidateEvaluation,
    benchmark_metrics_for_period,
    evaluate_candidate_period,
    load_close_prices_through,
)
from src.logging_config import configure_logging
from src.research_data import load_canonical_timestamp_index
from src.research_lifecycle import (
    assert_current_v1_entrypoint_is_sealed,
    load_research_lifecycle,
)

LOGGER = logging.getLogger(__name__)
SCORING_RULE = (
    "stage score = Sharpe + 0.25*Calmar - 0.75*max_drawdown "
    "- 0.02*annualized_turnover; training selection = 0.25*candidate score "
    "+ 0.75*one-parameter-neighborhood median - 0.50*neighborhood dispersion; "
    "future lock score = 0.40*training region score + 0.60*validation score "
    "- 0.25*|training-validation|; non-overlapping walk-forward folds are diagnostics only"
)
METHODOLOGY_VERSION = "future-selection-v2-nonoverlap-oos"


def future_lock_score(*, selection_score: float, validation_score: float) -> float:
    return float(
        0.40 * selection_score
        + 0.60 * validation_score
        - 0.25 * abs(selection_score - validation_score)
    )


def _period_payload(period: Period) -> dict[str, str]:
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _evaluation_record(
    evaluation: CandidateEvaluation, *, stage: str, fold: int | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "event": "candidate_evaluation",
        "stage": stage,
        "candidate_id": evaluation.candidate.candidate_id,
        "parameters": evaluation.candidate.to_dict(),
        "period": _period_payload(evaluation.period),
        "penalized_score": evaluation.penalized_score,
        "metrics": evaluation.metrics,
        "benchmark_metrics": evaluation.benchmark_metrics,
        "comparisons": evaluation.comparisons,
    }
    if fold is not None:
        record["fold"] = fold
        record["scored_for_lock"] = False
    return record


def _evaluate_stage_candidate(
    *,
    prices,
    period: Period,
    candidate: Candidate,
    strategy_config,
    backtest_config,
    benchmark_metrics,
    stage: str,
    ledger: ExperimentLedger,
    fold: int | None = None,
) -> tuple[CandidateEvaluation, dict[str, Any]]:
    evaluation = evaluate_candidate_period(
        prices,
        period,
        candidate,
        strategy_config,
        backtest_config,
        benchmark_metrics,
    )
    record = _evaluation_record(evaluation, stage=stage, fold=fold)
    ledger.append(record)
    return evaluation, record


def run_controlled_experiments(project_root: Path | None = None) -> dict[str, Any]:
    root = (project_root or Path(__file__).resolve().parent).resolve()
    assert_current_v1_entrypoint_is_sealed(load_research_lifecycle(root))
    canonical = load_canonical_research_config(project_root)
    settings = canonical.settings
    configure_logging(settings.logs_dir, settings.log_level)
    assets = list(canonical.assets)
    strategy_config, backtest_config = load_run_configuration(settings.project_root)
    config_payload = yaml.safe_load(
        (settings.project_root / "config" / "strategy.yaml").read_text(encoding="utf-8")
    )
    experiment_config = config_payload["experiment"]
    if not config_payload.get("experiment_manager_enabled", False):
        raise PermissionError("Controlled experiment manager is not enabled")
    if config_payload.get("live_trading_enabled", False):
        raise PermissionError("Live trading must remain disabled")

    candidates = generate_candidate_grid(config_payload["parameter_grid"])
    common_index, dataset_provenance = load_canonical_timestamp_index(
        settings.processed_dir, assets, "1d"
    )
    periods = build_chronological_periods(
        common_index,
        minimum_lookback=max(candidate.trend_filter for candidate in candidates),
        train_fraction=float(experiment_config["train_fraction"]),
        validation_fraction=float(experiment_config["validation_fraction"]),
        walk_forward_folds=int(experiment_config["walk_forward_folds"]),
    )
    finalist_count = int(experiment_config["finalist_count"])
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = settings.project_root / "experiments" / "runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    ledger = ExperimentLedger(output_dir / "experiment_ledger.jsonl")
    gate = ExperimentGate(expected_training_trials=len(candidates))
    ledger.append(
        {
            "event": "run_started",
            "run_id": run_id,
            "methodology_version": METHODOLOGY_VERSION,
            "canonical_run": True,
            "dataset_provenance": dataset_provenance,
            "effective_configuration": {
                "strategy": config_payload["strategy"],
                "backtest": asdict(backtest_config),
                "assets": assets,
                "timeframe": "1d",
                "experiment": experiment_config,
            },
            "unique_candidate_count": len(candidates),
            "backtest": {
                **asdict(backtest_config),
                "accounting_currency": "USDT",
                "capital_reference": "EUR 2,000 equivalent",
            },
            "periods": {
                "training": _period_payload(periods.training),
                "validation": _period_payload(periods.validation),
                "final_test": _period_payload(periods.final_test),
                "walk_forward": [
                    _period_payload(period) for period in periods.walk_forward
                ],
            },
            "final_test_close_values_loaded": False,
            "live_promotion": False,
        }
    )

    # Stage 1: the Parquet reader is physically bounded at training_end.
    training_prices = load_close_prices_through(
        settings.processed_dir, assets, "1d", periods.training.end
    )
    ledger.append(
        {
            "event": "training_data_opened",
            "maximum_loaded_timestamp_utc": training_prices.index.max().isoformat(),
        }
    )
    training_benchmarks = benchmark_metrics_for_period(
        training_prices, periods.training, backtest_config
    )
    training_records: list[dict[str, Any]] = []
    training_scores: dict[str, float] = {}
    for number, candidate in enumerate(candidates, start=1):
        evaluation, record = _evaluate_stage_candidate(
            prices=training_prices,
            period=periods.training,
            candidate=candidate,
            strategy_config=strategy_config,
            backtest_config=backtest_config,
            benchmark_metrics=training_benchmarks,
            stage="training",
            ledger=ledger,
        )
        training_records.append(record)
        training_scores[candidate.candidate_id] = evaluation.penalized_score
        gate.record_training(candidate.candidate_id)
        if number % 12 == 0 or number == len(candidates):
            LOGGER.info("Training trials completed: %d/%d", number, len(candidates))

    finalists, stability = select_stable_finalists(
        candidates, training_scores, finalist_count=finalist_count
    )
    gate.set_finalists([candidate.candidate_id for candidate in finalists])
    ledger.append(
        {
            "event": "finalists_selected_from_training",
            "candidate_ids": [candidate.candidate_id for candidate in finalists],
            "stability_diagnostics": {
                candidate.candidate_id: stability[candidate.candidate_id]
                for candidate in finalists
            },
            "final_test_close_values_loaded": False,
        }
    )

    # Stage 2: validation values become available only after finalists are fixed.
    validation_prices = load_close_prices_through(
        settings.processed_dir, assets, "1d", periods.validation.end
    )
    ledger.append(
        {
            "event": "validation_data_opened",
            "maximum_loaded_timestamp_utc": validation_prices.index.max().isoformat(),
            "final_test_close_values_loaded": False,
        }
    )
    validation_benchmarks = benchmark_metrics_for_period(
        validation_prices, periods.validation, backtest_config
    )
    validation_records: list[dict[str, Any]] = []
    validation_scores: dict[str, float] = {}
    for candidate in finalists:
        evaluation, record = _evaluate_stage_candidate(
            prices=validation_prices,
            period=periods.validation,
            candidate=candidate,
            strategy_config=strategy_config,
            backtest_config=backtest_config,
            benchmark_metrics=validation_benchmarks,
            stage="validation",
            ledger=ledger,
        )
        validation_records.append(record)
        validation_scores[candidate.candidate_id] = evaluation.penalized_score
        gate.record_validation(candidate.candidate_id)

    walk_forward_records: list[dict[str, Any]] = []
    walk_forward_scores: dict[str, list[float]] = {
        candidate.candidate_id: [] for candidate in finalists
    }
    for fold_number, fold in enumerate(periods.walk_forward, start=1):
        fold_prices = validation_prices.loc[: fold.end]
        fold_benchmarks = benchmark_metrics_for_period(
            fold_prices, fold, backtest_config
        )
        for candidate in finalists:
            evaluation, record = _evaluate_stage_candidate(
                prices=fold_prices,
                period=fold,
                candidate=candidate,
                strategy_config=strategy_config,
                backtest_config=backtest_config,
                benchmark_metrics=fold_benchmarks,
                stage="rolling_pretest_evaluation",
                fold=fold_number,
                ledger=ledger,
            )
            walk_forward_records.append(record)
            walk_forward_scores[candidate.candidate_id].append(
                evaluation.penalized_score
            )

    finalist_summaries: list[dict[str, Any]] = []
    for candidate in finalists:
        candidate_id = candidate.candidate_id
        fold_scores = walk_forward_scores[candidate_id]
        walk_median = float(median(fold_scores))
        walk_dispersion = float(pstdev(fold_scores)) if len(fold_scores) > 1 else 0.0
        validation_score = validation_scores[candidate_id]
        selection_score = float(stability[candidate_id]["selection_score"])
        instability_penalty = 0.25 * abs(selection_score - validation_score)
        lock_score = future_lock_score(
            selection_score=selection_score,
            validation_score=validation_score,
        )
        finalist_summaries.append(
            {
                "candidate_id": candidate_id,
                "parameters": candidate.to_dict(),
                "selection_score": stability[candidate_id]["selection_score"],
                "region_median": stability[candidate_id]["region_median"],
                "region_dispersion": stability[candidate_id]["region_dispersion"],
                "validation_score": validation_score,
                "walk_forward_median": walk_median,
                "walk_forward_dispersion": walk_dispersion,
                "walk_forward_scored_for_lock": False,
                "rolling_pretest_median": walk_median,
                "rolling_pretest_dispersion": walk_dispersion,
                "rolling_pretest_scored_for_lock": False,
                "instability_penalty": instability_penalty,
                "lock_score": lock_score,
            }
        )
    finalist_summaries.sort(key=lambda item: (-item["lock_score"], item["candidate_id"]))
    locked_summary = finalist_summaries[0]
    locked_candidate = next(
        candidate
        for candidate in finalists
        if candidate.candidate_id == locked_summary["candidate_id"]
    )
    gate.lock_candidate(locked_candidate.candidate_id)
    ledger.append(
        {
            "event": "candidate_locked",
            **locked_summary,
            "locked_before_final_test_data_access": True,
            "live_promotion": False,
        }
    )

    # Stage 3: this is the first close-value read that includes the final test.
    gate.assert_test_access(locked_candidate.candidate_id)
    test_prices = load_close_prices_through(
        settings.processed_dir, assets, "1d", periods.final_test.end
    )
    ledger.append(
        {
            "event": "final_test_data_opened",
            "candidate_id": locked_candidate.candidate_id,
            "maximum_loaded_timestamp_utc": test_prices.index.max().isoformat(),
            "lock_confirmed": True,
        }
    )
    test_benchmarks = benchmark_metrics_for_period(
        test_prices, periods.final_test, backtest_config
    )
    final_evaluation, final_test_record = _evaluate_stage_candidate(
        prices=test_prices,
        period=periods.final_test,
        candidate=locked_candidate,
        strategy_config=strategy_config,
        backtest_config=backtest_config,
        benchmark_metrics=test_benchmarks,
        stage="final_test",
        ledger=ledger,
    )

    final_evaluation.result.equity_curve.to_parquet(output_dir / "final_test_equity.parquet")
    final_evaluation.result.orders.to_parquet(
        output_dir / "final_test_orders.parquet", index=False
    )
    final_evaluation.result.fills.to_parquet(
        output_dir / "final_test_fills.parquet", index=False
    )
    final_evaluation.result.positions.to_parquet(
        output_dir / "final_test_positions.parquet", index=False
    )
    final_evaluation.result.cash.to_frame().to_parquet(
        output_dir / "final_test_cash.parquet"
    )

    candidate_trial_count = (
        len(training_records)
        + len(validation_records)
        + len(walk_forward_records)
        + 1
    )
    ledger.append(
        {
            "event": "run_completed",
            "candidate_trial_count": candidate_trial_count,
            "unique_candidate_count": len(candidates),
            "locked_candidate_id": locked_candidate.candidate_id,
            "live_promotion": False,
        }
    )
    if not ledger.verify():
        raise RuntimeError("Experiment ledger failed final verification")
    ledger_record_count = ledger.record_count
    final_hash = ledger.final_hash
    ledger.finalize()

    summary = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "unique_candidate_count": len(candidates),
        "candidate_trial_count": candidate_trial_count,
        "ledger_record_count": ledger_record_count,
        "ledger_final_hash": final_hash,
        "periods": {
            "training": _period_payload(periods.training),
            "validation": _period_payload(periods.validation),
            "final_test": _period_payload(periods.final_test),
            "walk_forward": [
                _period_payload(period) for period in periods.walk_forward
            ],
            "rolling_pretest_evaluation": [
                _period_payload(period) for period in periods.walk_forward
            ],
        },
        "training_records": training_records,
        "validation_records": validation_records,
        "walk_forward_records": walk_forward_records,
        "rolling_pretest_evaluation_records": walk_forward_records,
        "walk_forward_fields_are_legacy_compatibility_labels": True,
        "finalists": finalist_summaries,
        "locked_candidate": locked_summary,
        "final_test": final_test_record,
        "live_promotion": False,
        "scoring_rule": SCORING_RULE,
        "methodology_version": METHODOLOGY_VERSION,
        "canonical_run": True,
        "dataset_provenance": dataset_provenance,
        "effective_configuration": {
            "strategy": config_payload["strategy"],
            "backtest": asdict(backtest_config),
            "assets": assets,
            "timeframe": "1d",
            "experiment": experiment_config,
        },
        "fees_and_slippage_included": True,
    }
    paths = write_experiment_reports(output_dir, summary)
    manifest_path = output_dir / "ledger_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "ledger": "experiment_ledger.jsonl",
                "record_count": ledger_record_count,
                "final_hash": final_hash,
                "verified_before_finalize": True,
                "read_only_after_finalize": True,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    paths["ledger_manifest"] = manifest_path
    paths["ledger"] = output_dir / "experiment_ledger.jsonl"
    LOGGER.info(
        "Controlled experiments complete: trials=%d locked=%s",
        candidate_trial_count,
        locked_candidate.candidate_id,
    )
    return {"summary": summary, "paths": paths}


def main() -> None:
    result = run_controlled_experiments()
    summary = result["summary"]
    print(f"Experiment run {summary['run_id']} completed")
    print(f"Candidate trials: {summary['candidate_trial_count']}")
    print(f"Locked candidate: {summary['locked_candidate']['candidate_id']}")
    print(f"Robustness report: {result['paths']['robustness_report']}")


if __name__ == "__main__":
    main()
