import json
from pathlib import Path

import numpy as np
import pytest

from src.statistical_diagnostics import (
    _expected_max_sharpe,
    block_bootstrap_ci,
    generate_statistical_diagnostic,
    probabilistic_sharpe_ratio,
)


def test_probabilistic_sharpe_and_bootstrap_are_deterministic():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    psr = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0, periods_per_year=365)
    first = block_bootstrap_ci(returns, statistic="mean", block_size=5, replications=500, seed=7)
    second = block_bootstrap_ci(returns, statistic="mean", block_size=5, replications=500, seed=7)

    assert 0 <= psr <= 1
    assert first == second
    assert first["status"] == "OK"


def test_probabilistic_sharpe_matches_reference_with_annualized_benchmark():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    result = probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=0.25,
        periods_per_year=365,
    )

    assert result == pytest.approx(0.9998590203062097, abs=1e-12)


def test_probabilistic_sharpe_is_frequency_invariant_for_equivalent_benchmarks():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    daily = probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=0.25,
        periods_per_year=365,
    )
    weekly = probabilistic_sharpe_ratio(
        returns,
        benchmark_sharpe=0.25 * np.sqrt(52 / 365),
        periods_per_year=52,
    )

    assert weekly == pytest.approx(daily, abs=1e-12)


def test_tracked_post_selection_diagnostics_use_corrected_frequency_scaling(tmp_path):
    run = Path("experiments/runs/20260822T000641481839Z")
    generated_result = generate_statistical_diagnostic(
        equity_path=run / "final_test_equity.parquet",
        fills_path=run / "final_test_fills.parquet",
        training_trials_path=run / "training_trials.parquet",
        experiment_summary_path=run / "experiment_summary.json",
        output_dir=tmp_path,
    )
    artifact = json.loads(
        Path("audits/statistical/statistical_diagnostics.json").read_text(encoding="utf-8")
    )
    generated = json.loads(
        Path(generated_result["json_path"]).read_text(encoding="utf-8")
    )

    assert artifact["status"] == "POST_SELECTION_DIAGNOSTIC_NOT_SEALED_OOS"
    assert artifact == generated
    assert artifact["probabilistic_sharpe_ratio_vs_zero"] == pytest.approx(
        0.5603306177454201, abs=1e-12
    )
    assert artifact["deflated_sharpe_ratio"] == pytest.approx(
        0.012314525539555088, abs=1e-12
    )
    assert artifact["training_configuration_trials"] == 96
    assert artifact["total_candidate_backtests_disclosed"] == 117


def test_candidate_count_is_derived_from_authoritative_summary(tmp_path):
    source = Path("experiments/runs/20260822T000641481839Z")
    run = tmp_path / source.name
    run.mkdir()
    for name in (
        "experiment_summary.json",
        "final_test_equity.parquet",
        "final_test_fills.parquet",
        "training_trials.parquet",
    ):
        (run / name).write_bytes((source / name).read_bytes())
    summary_path = run / "experiment_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["candidate_trial_count"] = 120
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    result = generate_statistical_diagnostic(
        equity_path=run / "final_test_equity.parquet",
        fills_path=run / "final_test_fills.parquet",
        training_trials_path=run / "training_trials.parquet",
        experiment_summary_path=summary_path,
        output_dir=tmp_path / "diagnostics",
    )

    markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
    assert result["training_configuration_trials"] == 96
    assert result["total_candidate_backtests_disclosed"] == 120
    assert "96 training configurations / 120 candidate backtests" in markdown


def test_statistical_diagnostic_rejects_mismatched_run_provenance(tmp_path):
    summary_path = tmp_path / "experiment_summary.json"
    summary_path.write_text(
        json.dumps({"run_id": "different-run", "candidate_trial_count": 117}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="provenance"):
        generate_statistical_diagnostic(
            equity_path=tmp_path / "missing-equity.parquet",
            fills_path=tmp_path / "missing-fills.parquet",
            training_trials_path=tmp_path / "missing-training.parquet",
            experiment_summary_path=summary_path,
            output_dir=tmp_path / "output",
        )


@pytest.mark.parametrize("candidate_trial_count", [0, -1, True, 1.5, "117"])
def test_statistical_diagnostic_rejects_malformed_candidate_count(
    tmp_path, candidate_trial_count
):
    summary_path = tmp_path / "experiment_summary.json"
    summary_path.write_text(
        json.dumps({"run_id": tmp_path.name, "candidate_trial_count": candidate_trial_count}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="candidate_trial_count"):
        generate_statistical_diagnostic(
            equity_path=tmp_path / "missing-equity.parquet",
            fills_path=tmp_path / "missing-fills.parquet",
            training_trials_path=tmp_path / "missing-training.parquet",
            experiment_summary_path=summary_path,
            output_dir=tmp_path / "output",
        )


def test_small_sample_is_explicitly_insufficient():
    result = block_bootstrap_ci(np.array([0.01, -0.01]), statistic="sharpe", block_size=2)
    assert result["status"] == "INSUFFICIENT_SAMPLE"


def test_sharpe_bootstrap_defaults_to_daily_annualization():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    default = block_bootstrap_ci(
        returns, statistic="sharpe", block_size=5, replications=500, seed=7
    )
    explicit = block_bootstrap_ci(
        returns,
        statistic="sharpe",
        block_size=5,
        periods_per_year=365,
        replications=500,
        seed=7,
    )

    assert default == explicit


def test_sharpe_bootstrap_supports_alternative_frequency_reference():
    returns = np.array([0.01, -0.005, 0.004, 0.002, -0.003] * 30)

    daily = block_bootstrap_ci(
        returns,
        statistic="sharpe",
        block_size=5,
        periods_per_year=365,
        replications=500,
        seed=7,
    )
    weekly = block_bootstrap_ci(
        returns,
        statistic="sharpe",
        block_size=5,
        periods_per_year=52,
        replications=500,
        seed=7,
    )

    scale = np.sqrt(52 / 365)
    assert weekly["lower"] == pytest.approx(daily["lower"] * scale, abs=1e-12)
    assert weekly["upper"] == pytest.approx(daily["upper"] * scale, abs=1e-12)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"block_size": 0}, "block_size"),
        ({"block_size": -1}, "block_size"),
        ({"block_size": True}, "block_size"),
        ({"replications": 0}, "replications"),
        ({"replications": -1}, "replications"),
        ({"replications": True}, "replications"),
        ({"periods_per_year": 0}, "periods_per_year"),
        ({"periods_per_year": True}, "periods_per_year"),
        ({"seed": -1}, "seed"),
        ({"seed": True}, "seed"),
        ({"seed": 1.5}, "seed"),
        ({"statistic": "median"}, "statistic"),
    ],
)
def test_bootstrap_rejects_invalid_controls_before_sampling(kwargs, message):
    arguments = {
        "statistic": "mean",
        "block_size": 5,
        "periods_per_year": 365,
        "replications": 20,
        "seed": 7,
    }
    arguments.update(kwargs)

    with pytest.raises(ValueError, match=message):
        block_bootstrap_ci(np.arange(30, dtype=float), **arguments)


@pytest.mark.parametrize("periods_per_year", [0, -1, True, 365.0])
def test_probabilistic_sharpe_requires_exact_positive_period_count(periods_per_year):
    with pytest.raises(ValueError, match="periods_per_year"):
        probabilistic_sharpe_ratio(
            np.arange(10, dtype=float),
            benchmark_sharpe=0.0,
            periods_per_year=periods_per_year,
        )


@pytest.mark.parametrize("benchmark", [True, "0.0", np.nan, np.inf])
def test_probabilistic_sharpe_rejects_malformed_benchmark(benchmark):
    with pytest.raises(ValueError, match="benchmark_sharpe"):
        probabilistic_sharpe_ratio(
            np.arange(10, dtype=float),
            benchmark_sharpe=benchmark,
            periods_per_year=365,
        )


@pytest.mark.parametrize("trials", [0, -1, True, 2.0])
def test_expected_max_sharpe_requires_exact_positive_trial_count(trials):
    with pytest.raises(ValueError, match="trials"):
        _expected_max_sharpe(np.array([0.1, 0.2]), trials)


def test_expected_max_sharpe_rejects_nonfinite_sharpes():
    with pytest.raises(ValueError, match="sharpes"):
        _expected_max_sharpe(np.array([0.1, np.nan]), 2)
