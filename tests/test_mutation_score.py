from __future__ import annotations

import json
import sys
from pathlib import Path

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


def test_mutation_workspace_uses_matching_non_src_module_names(tmp_path):
    from scripts.prepare_mutation_assurance import (
        MUTATION_TARGETS,
        prepare_mutation_workspace,
    )

    project_root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "mutation-workspace"
    prepare_mutation_workspace(project_root, workspace)

    target_root = workspace / "mutation_targets"
    assert (target_root / "__init__.py").is_file()
    assert tuple(
        sorted(
            path.name
            for path in target_root.glob("*.py")
            if path.name != "__init__.py"
        )
    ) == tuple(sorted(Path(relative).name for relative in MUTATION_TARGETS))
    for relative in MUTATION_TARGETS:
        assert (target_root / Path(relative).name).read_bytes() == (
            project_root / relative
        ).read_bytes()

    import tomllib

    config = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))
    mutation_config = config["tool"]["mutmut"]
    assert mutation_config["source_paths"] == ["mutation_targets/"]
    assert mutation_config["only_mutate"] == [
        f"mutation_targets/{Path(relative).name}" for relative in MUTATION_TARGETS
    ]
    assert mutation_config["pytest_add_cli_args_test_selection"] == [
        "tests/test_mutation_assurance.py"
    ]
    assert (workspace / "tests" / "test_mutation_assurance.py").read_bytes() == (
        project_root / "tests" / "test_mutation_assurance.py"
    ).read_bytes()


def test_mutation_workspace_uses_runner_or_hermes_scratch(monkeypatch, tmp_path):
    from scripts.prepare_mutation_assurance import mutation_temporary_root

    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "system-temp"))
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert mutation_temporary_root() == Path(
        hermes_home / "cache" / "scratch"
    )

    runner_temp = tmp_path / "actions" / "runner-temp"
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    assert mutation_temporary_root() == runner_temp.resolve()
