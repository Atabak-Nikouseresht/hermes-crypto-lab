"""Controlled candidate generation, access gating, and stability selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from statistics import median, pstdev


import numpy as np
import pandas as pd

ALLOWED_PARAMETER_GRID = {
    "momentum_window": [60, 90, 120],
    "skip_window": [0, 7],
    "trend_filter": [150, 200],
    "selected_assets": [1, 2],
    "rebalance_days": [7, 14],
    "volatility_window": [30, 60],
}


@dataclass(frozen=True, order=True)
class Candidate:
    momentum_window: int
    skip_window: int
    trend_filter: int
    selected_assets: int
    rebalance_days: int
    volatility_window: int

    @property
    def candidate_id(self) -> str:
        return (
            f"mw{self.momentum_window:03d}_sw{self.skip_window:02d}_"
            f"ma{self.trend_filter:03d}_n{self.selected_assets}_"
            f"r{self.rebalance_days:02d}_v{self.volatility_window:02d}"
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class Period:
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass(frozen=True)
class ExperimentPeriods:
    training: Period
    validation: Period
    final_test: Period
    walk_forward: tuple[Period, ...]


def generate_candidate_grid(grid: dict[str, list[int]]) -> list[Candidate]:
    required = [
        "momentum_window",
        "skip_window",
        "trend_filter",
        "selected_assets",
        "rebalance_days",
        "volatility_window",
    ]
    missing = [key for key in required if key not in grid]
    if missing:
        raise ValueError(f"Missing parameter-grid keys: {missing}")
    extra = sorted(set(grid) - set(required))
    if extra:
        raise ValueError(f"Unexpected parameter-grid keys: {extra}")
    for key in required:
        if sorted(grid[key]) != sorted(ALLOWED_PARAMETER_GRID[key]):
            raise ValueError(f"Grid values outside the controlled set for {key}")
    candidates = [Candidate(*values) for values in product(*(grid[key] for key in required))]
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("Parameter grid contains duplicate candidates")
    return candidates


def _week_key(timestamp: pd.Timestamp) -> tuple[int, int]:
    iso = timestamp.isocalendar()
    return int(iso.year), int(iso.week)


def build_chronological_periods(
    index: pd.DatetimeIndex,
    *,
    minimum_lookback: int,
    train_fraction: float,
    validation_fraction: float,
    walk_forward_folds: int,
) -> ExperimentPeriods:
    if index.tz is None:
        raise ValueError("Experiment index must be timezone-aware")
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("Split fractions must be between zero and one")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("Training plus validation must leave a final test period")
    if walk_forward_folds < 1:
        raise ValueError("walk_forward_folds must be positive")
    ordered = pd.DatetimeIndex(index).sort_values().tz_convert("UTC")
    week_ends = [
        location
        for location in range(minimum_lookback - 1, len(ordered) - 1)
        if _week_key(ordered[location]) != _week_key(ordered[location + 1])
    ]
    if len(week_ends) < 6:
        raise ValueError("Insufficient weekly boundaries for controlled experiments")
    start_position = week_ends[0]
    usable = len(ordered) - start_position

    def boundary_at(fraction: float) -> int:
        target = start_position + int(usable * fraction) - 1
        eligible = [position for position in week_ends if start_position < position <= target]
        if not eligible:
            raise ValueError("Experiment split is too short")
        return eligible[-1]

    training_end_position = boundary_at(train_fraction)
    validation_end_position = boundary_at(train_fraction + validation_fraction)
    training = Period(ordered[start_position], ordered[training_end_position])
    validation_start_position = training_end_position + 1
    final_test_start_position = validation_end_position + 1
    validation = Period(ordered[validation_start_position], ordered[validation_end_position])
    final_test = Period(ordered[final_test_start_position], ordered[-1])

    validation_positions = np.arange(validation_start_position, validation_end_position + 1)
    if len(validation_positions) < walk_forward_folds:
        raise ValueError("Validation period is too short for non-overlapping folds")
    chunks = np.array_split(validation_positions, walk_forward_folds)
    walk_forward = [
        Period(ordered[int(chunk[0])], ordered[int(chunk[-1])])
        for chunk in chunks
        if len(chunk)
    ]
    return ExperimentPeriods(training, validation, final_test, tuple(walk_forward))


def penalized_score(metrics: dict[str, float | int], *, duration_days: int) -> float:
    years = max(duration_days / 365.25, 1.0 / 365.25)
    annualized_turnover = float(metrics["turnover"]) / years
    return float(
        float(metrics["sharpe"])
        + 0.25 * float(metrics["calmar"])
        - 0.75 * float(metrics["max_drawdown"])
        - 0.02 * annualized_turnover
    )


def _hamming_distance(left: Candidate, right: Candidate) -> int:
    return sum(
        a != b
        for a, b in zip(
            left.to_dict().values(), right.to_dict().values(), strict=True
        )
    )


def select_stable_finalists(
    candidates: list[Candidate],
    base_scores: dict[str, float],
    *,
    finalist_count: int,
) -> tuple[list[Candidate], dict[str, dict[str, float | int]]]:
    diagnostics: dict[str, dict[str, float | int]] = {}
    for candidate in candidates:
        region = [
            float(base_scores[other.candidate_id])
            for other in candidates
            if _hamming_distance(candidate, other) <= 1
        ]
        region_median = float(median(region))
        region_dispersion = float(pstdev(region)) if len(region) > 1 else 0.0
        base = float(base_scores[candidate.candidate_id])
        selection_score = 0.25 * base + 0.75 * region_median - 0.50 * region_dispersion
        diagnostics[candidate.candidate_id] = {
            "base_score": base,
            "region_size": len(region),
            "region_median": region_median,
            "region_dispersion": region_dispersion,
            "selection_score": selection_score,
        }
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -float(diagnostics[candidate.candidate_id]["selection_score"]),
            candidate.candidate_id,
        ),
    )
    return ranked[:finalist_count], diagnostics


class ExperimentGate:
    """Enforce train -> validation -> lock -> final-test access ordering."""

    def __init__(self, expected_training_trials: int):
        self.expected_training_trials = expected_training_trials
        self.training_ids: set[str] = set()
        self.finalist_ids: set[str] | None = None
        self.validated_ids: set[str] = set()
        self.locked_candidate_id: str | None = None

    def record_training(self, candidate_id: str) -> None:
        if self.finalist_ids is not None:
            raise PermissionError("Training stage is already closed")
        self.training_ids.add(candidate_id)

    def set_finalists(self, candidate_ids: list[str]) -> None:
        if len(self.training_ids) != self.expected_training_trials:
            raise PermissionError("All training trials must finish before finalist selection")
        if not set(candidate_ids).issubset(self.training_ids):
            raise ValueError("Finalists must come from tested training candidates")
        self.finalist_ids = set(candidate_ids)

    def record_validation(self, candidate_id: str) -> None:
        if self.finalist_ids is None or candidate_id not in self.finalist_ids:
            raise PermissionError("Only declared finalists may access validation")
        self.validated_ids.add(candidate_id)

    def lock_candidate(self, candidate_id: str) -> None:
        if candidate_id not in self.validated_ids:
            raise PermissionError("Candidate must finish validation before it can be locked")
        if self.locked_candidate_id is not None:
            raise PermissionError("A candidate is already locked")
        self.locked_candidate_id = candidate_id

    def assert_test_access(self, candidate_id: str) -> None:
        if self.locked_candidate_id != candidate_id:
            raise PermissionError("Final test data is sealed until this candidate is locked")
