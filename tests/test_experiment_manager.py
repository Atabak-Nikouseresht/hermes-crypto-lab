import pandas as pd
import pytest

from run_experiments import future_lock_score

from src.experiment_manager import (
    Candidate,
    ExperimentGate,
    build_chronological_periods,
    generate_candidate_grid,
    penalized_score,
    select_stable_finalists,
)


GRID = {
    "momentum_window": [60, 90, 120],
    "skip_window": [0, 7],
    "trend_filter": [150, 200],
    "selected_assets": [1, 2],
    "rebalance_days": [7, 14],
    "volatility_window": [30, 60],
}


def test_allowed_grid_generates_exactly_96_unique_candidates():
    candidates = generate_candidate_grid(GRID)

    assert len(candidates) == 96
    assert len({candidate.candidate_id for candidate in candidates}) == 96
    assert all(candidate.rebalance_days in {7, 14} for candidate in candidates)


def test_final_test_access_is_blocked_until_validated_candidate_is_locked():
    gate = ExperimentGate(expected_training_trials=2)
    gate.record_training("a")
    gate.record_training("b")
    gate.set_finalists(["a"])

    with pytest.raises(PermissionError):
        gate.assert_test_access("a")

    gate.record_validation("a")
    gate.lock_candidate("a")
    gate.assert_test_access("a")

    with pytest.raises(PermissionError):
        gate.assert_test_access("b")


def test_stable_region_beats_isolated_high_score():
    isolated = Candidate(60, 0, 150, 1, 7, 30)
    isolated_neighbor = Candidate(90, 0, 150, 1, 7, 30)
    stable_a = Candidate(60, 7, 200, 2, 14, 60)
    stable_b = Candidate(90, 7, 200, 2, 14, 60)
    stable_c = Candidate(120, 7, 200, 2, 14, 60)
    scores = {
        isolated.candidate_id: 10.0,
        isolated_neighbor.candidate_id: -2.0,
        stable_a.candidate_id: 5.0,
        stable_b.candidate_id: 5.1,
        stable_c.candidate_id: 4.9,
    }

    finalists, diagnostics = select_stable_finalists(
        [isolated, isolated_neighbor, stable_a, stable_b, stable_c],
        scores,
        finalist_count=1,
    )

    assert finalists[0] in {stable_a, stable_b, stable_c}
    assert diagnostics[finalists[0].candidate_id]["region_median"] > 4.0


def test_periods_are_chronological_and_walk_forward_never_enters_final_test():
    index = pd.date_range("2020-01-01", periods=1_000, freq="D", tz="UTC")

    periods = build_chronological_periods(
        index,
        minimum_lookback=200,
        train_fraction=0.60,
        validation_fraction=0.20,
        walk_forward_folds=3,
    )

    assert periods.training.start < periods.training.end
    assert periods.training.end < periods.validation.end < periods.final_test.end
    assert periods.training.end < periods.validation.start
    assert periods.validation.end < periods.final_test.start
    assert all(fold.end <= periods.validation.end for fold in periods.walk_forward)
    assert all(
        left.end < right.start
        for left, right in zip(periods.walk_forward, periods.walk_forward[1:], strict=False)
    )
    assert all(
        periods.validation.start <= fold.start <= fold.end
        for fold in periods.walk_forward
    )
    covered = pd.DatetimeIndex([])
    for fold in periods.walk_forward:
        covered = covered.union(index[(index >= fold.start) & (index <= fold.end)])
    expected = index[(index >= periods.validation.start) & (index <= periods.validation.end)]
    assert covered.equals(expected)


def test_future_lock_score_does_not_double_count_walk_forward_diagnostics():
    assert future_lock_score(selection_score=1.0, validation_score=2.0) == pytest.approx(
        0.4 * 1.0 + 0.6 * 2.0 - 0.25 * 1.0
    )


def test_penalized_score_reduces_turnover_and_drawdown_excesses():
    base = {
        "sharpe": 1.0,
        "calmar": 1.0,
        "max_drawdown": 0.20,
        "turnover": 2.0,
    }
    expensive = {**base, "max_drawdown": 0.50, "turnover": 20.0}

    assert penalized_score(base, duration_days=365) > penalized_score(
        expensive, duration_days=365
    )
