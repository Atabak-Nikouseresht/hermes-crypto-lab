"""Read-back verification for Hermes scheduler jobs and approved wrappers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


class SchedulerContractError(ValueError):
    pass


def verify_scheduler_job(
    job: dict[str, Any],
    *,
    expected_name: str,
    expected_expression: str,
    expected_script: Path,
    expected_workdir: Path,
    scripts_root: Path,
    expected_script_sha256: str,
) -> dict[str, Any]:
    failures = []
    if type(job) is not dict:
        raise SchedulerContractError("Scheduler read-back job must be a mapping")

    job_id = job.get("id")
    if type(job_id) is not str or not job_id.strip():
        failures.append("id must be a non-empty string")

    name = job.get("name")
    if type(name) is not str or not name.strip() or name != expected_name:
        failures.append("name must match the expected non-empty string")

    schedule = job.get("schedule")
    if type(schedule) is not dict:
        failures.append("schedule must be a mapping")
    else:
        if set(schedule) != {"kind", "expr"}:
            failures.append("schedule must contain only kind and expr")
        kind = schedule.get("kind")
        expression = schedule.get("expr")
        if type(kind) is not str or kind != "cron":
            failures.append('schedule.kind must be the exact string "cron"')
        if (
            type(expression) is not str
            or not expression.strip()
            or expression != expected_expression
        ):
            failures.append("schedule.expr must match the expected non-empty string")

    actual_script_value = job.get("script")
    script_path: Path | None = None
    root = Path(scripts_root).resolve()
    expected_script_path = Path(expected_script).resolve()
    if type(actual_script_value) is not str or not actual_script_value.strip():
        failures.append("script must be a non-empty string")
    else:
        raw_parts = Path(actual_script_value.replace("\\", "/")).parts
        if ".." in raw_parts:
            failures.append("script path must not traverse directories")
        else:
            try:
                actual_value = Path(actual_script_value)
                script_path = (
                    actual_value.resolve()
                    if actual_value.is_absolute()
                    else (root / actual_value).resolve()
                )
                script_path.relative_to(root)
                if script_path != expected_script_path:
                    failures.append("script path does not match the approved wrapper")
                elif not script_path.is_file():
                    failures.append("approved script is missing")
            except (OSError, ValueError):
                failures.append("script path is malformed or outside the scripts root")

    readback_digest = job.get("script_sha256")
    if (
        type(readback_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", readback_digest) is None
    ):
        failures.append("script_sha256 must be a lowercase SHA-256 string")
    elif readback_digest != expected_script_sha256:
        failures.append("read-back script hash does not match the expected hash")

    no_agent = job.get("no_agent")
    if type(no_agent) is not bool or no_agent is not True:
        failures.append("no_agent must be the exact boolean true")

    workdir = job.get("workdir")
    if type(workdir) is not str or not workdir.strip():
        failures.append("workdir must be a non-empty string")
    else:
        try:
            if Path(workdir).resolve() != Path(expected_workdir).resolve():
                failures.append("workdir does not match the expected project directory")
        except (OSError, ValueError):
            failures.append("workdir is malformed")

    enabled = job.get("enabled")
    if type(enabled) is not bool or enabled is not True:
        failures.append("enabled must be the exact boolean true")

    if script_path is not None and script_path.is_file():
        actual_digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
        if actual_digest != expected_script_sha256:
            failures.append("approved wrapper bytes do not match the expected hash")
    if failures:
        raise SchedulerContractError("Scheduler read-back mismatch: " + "; ".join(failures))
    return {
        "verified": True,
        "job_id": job_id,
        "name": expected_name,
        "schedule": expected_expression,
        "script": str(expected_script_path),
        "script_sha256": expected_script_sha256,
        "workdir": str(Path(expected_workdir).resolve()),
        "no_agent": True,
        "enabled": True,
    }
