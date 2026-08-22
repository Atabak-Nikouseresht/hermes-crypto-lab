from src.experiment_report import write_experiment_reports


def test_experiment_reports_include_trial_count_walk_forward_and_no_live_promotion(tmp_path):
    summary = {
        "run_id": "run-1",
        "unique_candidate_count": 96,
        "candidate_trial_count": 117,
        "ledger_record_count": 120,
        "periods": {
            "training": {"start": "2020-01-01", "end": "2021-01-01"},
            "validation": {"start": "2021-01-01", "end": "2021-06-01"},
            "final_test": {"start": "2021-06-01", "end": "2022-01-01"},
        },
        "training_records": [],
        "validation_records": [],
        "walk_forward_records": [
            {"candidate_id": "a", "fold": 1, "penalized_score": 1.0}
        ],
        "finalists": [{"candidate_id": "a", "selection_score": 1.0}],
        "locked_candidate": {"candidate_id": "a", "lock_score": 0.8},
        "final_test": {
            "candidate_id": "a",
            "metrics": {"cagr": 0.1, "sharpe": 1.0, "max_drawdown": 0.2},
            "comparisons": {},
        },
        "live_promotion": False,
        "scoring_rule": "penalized",
    }

    paths = write_experiment_reports(tmp_path, summary)

    assert paths["walk_forward_report"].exists()
    assert paths["robustness_report"].exists()
    assert paths["summary_json"].exists()
    robustness = paths["robustness_report"].read_text(encoding="utf-8")
    assert "117" in robustness
    assert "Live promotion: **disabled**" in robustness
