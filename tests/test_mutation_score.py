from __future__ import annotations

import json
import sys

import pytest

import scripts.verify_mutation_score as mutation_score_module
from scripts.verify_mutation_score import (
    summarize_mutation_score,
    verify_mutation_score,
)


@pytest.fixture
def passing_stats():
    return {
        "killed": 8,
        "survived": 2,
        "total": 10,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }


def test_mutation_score_accepts_exact_floor_and_counts_timeout_as_killed(passing_stats):
    passing_stats["killed"] = 7
    passing_stats["survived"] = 1
    passing_stats["timeout"] = 1

    assert summarize_mutation_score(passing_stats, minimum_score=80) == (8, 10, 80.0)


def test_mutation_score_fails_below_floor(passing_stats):
    passing_stats["killed"] = 7
    passing_stats["survived"] = 3

    with pytest.raises(ValueError, match="below the required 80%"):
        summarize_mutation_score(passing_stats, minimum_score=80)


@pytest.mark.parametrize(
    "changes",
    [
        {"killed": True},
        {"total": -1},
        {"total": 0},
        {"skipped": 10},
        {"check_was_interrupted_by_user": 1},
        {"segfault": 1},
    ],
)
def test_mutation_score_rejects_invalid_or_incomplete_runs(passing_stats, changes):
    passing_stats.update(changes)

    with pytest.raises(ValueError):
        summarize_mutation_score(passing_stats)


def test_mutation_score_reader_requires_valid_json_statistics(tmp_path, passing_stats):
    stats_path = tmp_path / "mutmut-cicd-stats.json"
    stats_path.write_text(json.dumps(passing_stats), encoding="utf-8")

    assert verify_mutation_score(stats_path) == "Mutation score 80.00% (8/10); required 80%"

    stats_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="statistic"):
        verify_mutation_score(stats_path)


def test_mutation_score_rejects_non_integer_threshold(passing_stats):
    with pytest.raises(ValueError, match="exact integer"):
        summarize_mutation_score(passing_stats, minimum_score=True)


def test_mutation_score_cli_rejects_custom_stats_path(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_mutation_score.py", "--stats-path", "stale-stats.json"],
    )

    with pytest.raises(SystemExit) as error:
        mutation_score_module.main()

    assert error.value.code == 2
