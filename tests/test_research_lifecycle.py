from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import run_experiments
from src.research_lifecycle import (
    assert_candidate_selection_permitted,
    load_research_lifecycle,
)


ROOT = Path(__file__).resolve().parents[1]


def _sealed_artifact_hashes() -> dict[str, str]:
    runs = ROOT / "experiments" / "runs"
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in runs.rglob("*")
        if path.is_file()
    }


def _write_lifecycle(root: Path, payload: dict) -> None:
    path = root / "forward_experiment" / "research_lifecycle.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_sealed_v1_rejects_before_any_data_or_candidate_access(monkeypatch):
    calls: list[str] = []
    before = _sealed_artifact_hashes()
    monkeypatch.setattr(
        run_experiments,
        "load_canonical_research_config",
        lambda *_args: calls.append("canonical_config"),
    )
    monkeypatch.setattr(
        run_experiments,
        "generate_candidate_grid",
        lambda *_args: calls.append("candidate_grid"),
    )
    monkeypatch.setattr(
        run_experiments,
        "load_close_prices_through",
        lambda *_args: calls.append("close_prices"),
    )

    with pytest.raises(PermissionError, match="V1 research is sealed"):
        run_experiments.run_controlled_experiments(ROOT)

    assert calls == []
    assert _sealed_artifact_hashes() == before


def test_current_v1_entrypoint_rejects_a_substituted_v2_lifecycle_before_access(
    monkeypatch, tmp_path
):
    _write_lifecycle(
        tmp_path,
        {
            "lifecycle_schema_version": 1,
            "research_generation": "V2",
            "selection_status": "OPEN",
            "candidate_reselection_allowed": True,
            "final_test_opened": False,
            "research_specification_id": "future-research-v2",
            "untouched_oos_required": True,
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(
        run_experiments,
        "load_canonical_research_config",
        lambda *_args: calls.append("canonical_config"),
    )

    with pytest.raises(PermissionError, match="current V1 entrypoint"):
        run_experiments.run_controlled_experiments(tmp_path)

    assert calls == []


def test_lifecycle_rejects_malformed_or_unknown_contracts(tmp_path):
    for payload in (
        {"lifecycle_schema_version": 1},
        {
            "lifecycle_schema_version": 2,
            "research_generation": "V1",
            "selection_status": "SEALED",
            "candidate_reselection_allowed": False,
            "final_test_opened": True,
        },
        {
            "lifecycle_schema_version": 1,
            "research_generation": "V1",
            "selection_status": "UNKNOWN",
            "candidate_reselection_allowed": False,
            "final_test_opened": True,
        },
    ):
        _write_lifecycle(tmp_path, payload)
        with pytest.raises(ValueError):
            load_research_lifecycle(tmp_path)


def test_separate_v2_contract_is_representable_without_reopening_v1(tmp_path):
    _write_lifecycle(
        tmp_path,
        {
            "lifecycle_schema_version": 1,
            "research_generation": "V2",
            "selection_status": "OPEN",
            "candidate_reselection_allowed": True,
            "final_test_opened": False,
            "research_specification_id": "future-research-v2",
            "untouched_oos_required": True,
        },
    )

    lifecycle = load_research_lifecycle(tmp_path)
    assert lifecycle.research_generation == "V2"
    assert_candidate_selection_permitted(lifecycle)

    with pytest.raises(PermissionError, match="V1 research is sealed"):
        assert_candidate_selection_permitted(load_research_lifecycle(ROOT))


def test_lifecycle_contract_is_hardening_critical():
    generator = (ROOT / "scripts" / "generate_hardening_manifest.py").read_text(
        encoding="utf-8"
    )

    assert '"forward_experiment/research_lifecycle.json"' in generator
