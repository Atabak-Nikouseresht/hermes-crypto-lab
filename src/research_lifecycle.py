"""Fail-closed lifecycle contract for canonical research generation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


LIFECYCLE_PATH = Path("forward_experiment/research_lifecycle.json")


@dataclass(frozen=True)
class ResearchLifecycle:
    research_generation: str
    selection_status: str
    candidate_reselection_allowed: bool
    final_test_opened: bool
    research_specification_id: str | None = None
    untouched_oos_required: bool | None = None


def _require_exact(payload: dict, key: str, expected_type: type) -> object:
    value = payload.get(key)
    if type(value) is not expected_type:
        raise ValueError(f"Research lifecycle field {key!r} has an invalid type")
    return value


def load_research_lifecycle(project_root: Path) -> ResearchLifecycle:
    """Load only recognized, internally consistent lifecycle generations."""
    path = project_root / LIFECYCLE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Research lifecycle contract is unavailable or malformed") from error
    if type(payload) is not dict:
        raise ValueError("Research lifecycle contract must be an object")
    if type(payload.get("lifecycle_schema_version")) is not int or payload[
        "lifecycle_schema_version"
    ] != 1:
        raise ValueError("Unknown research lifecycle schema version")

    generation = _require_exact(payload, "research_generation", str)
    status = _require_exact(payload, "selection_status", str)
    reselection_allowed = _require_exact(payload, "candidate_reselection_allowed", bool)
    final_test_opened = _require_exact(payload, "final_test_opened", bool)
    lifecycle = ResearchLifecycle(
        research_generation=generation,
        selection_status=status,
        candidate_reselection_allowed=reselection_allowed,
        final_test_opened=final_test_opened,
        research_specification_id=payload.get("research_specification_id"),
        untouched_oos_required=payload.get("untouched_oos_required"),
    )
    if generation == "V1":
        if (
            status != "SEALED"
            or reselection_allowed is not False
            or final_test_opened is not True
        ):
            raise ValueError("V1 research lifecycle must remain sealed")
        return lifecycle
    if generation == "V2":
        if (
            status != "OPEN"
            or reselection_allowed is not True
            or final_test_opened is not False
            or type(lifecycle.research_specification_id) is not str
            or not lifecycle.research_specification_id
            or lifecycle.untouched_oos_required is not True
        ):
            raise ValueError("V2 research lifecycle requires a new specification and untouched OOS")
        return lifecycle
    raise ValueError(f"Unknown research generation: {generation!r}")


def assert_candidate_selection_permitted(lifecycle: ResearchLifecycle) -> None:
    if lifecycle.research_generation == "V1":
        raise PermissionError(
            "V1 research is sealed. Candidate reselection is forbidden. Start a separately "
            "governed V2 research generation with a new specification and genuinely untouched "
            "OOS period."
        )
    if lifecycle.research_generation != "V2" or not lifecycle.candidate_reselection_allowed:
        raise PermissionError("Research generation is not permitted to select candidates")


def assert_current_v1_entrypoint_is_sealed(lifecycle: ResearchLifecycle) -> None:
    """Keep the historical V1 entrypoint closed even if its local contract is replaced."""
    if lifecycle.research_generation != "V1":
        raise PermissionError(
            "The current V1 entrypoint cannot execute a future research generation. "
            "V2 requires a separately governed entrypoint, specification, and untouched OOS."
        )
    assert_candidate_selection_permitted(lifecycle)
