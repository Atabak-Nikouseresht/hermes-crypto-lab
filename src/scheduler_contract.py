"""Read-back verification for Hermes scheduler jobs and approved wrappers."""

from __future__ import annotations

import hashlib
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
    schedule = job.get("schedule") or {}
    if job.get("name") != expected_name:
        failures.append(f"name={job.get('name')!r}")
    if schedule.get("kind") != "cron" or schedule.get("expr") != expected_expression:
        failures.append(f"schedule={schedule!r}")
    actual_script_value = Path(str(job.get("script", "")))
    actual_script = (
        actual_script_value.resolve()
        if actual_script_value.is_absolute()
        else (Path(scripts_root) / actual_script_value).resolve()
    )
    expected_script = Path(expected_script).resolve()
    if actual_script != expected_script:
        failures.append(f"script={actual_script}")
    elif not actual_script.is_file():
        failures.append(f"script missing={actual_script}")
    else:
        actual_digest = hashlib.sha256(actual_script.read_bytes()).hexdigest()
        if actual_digest != expected_script_sha256:
            failures.append(
                f"script hash={actual_digest}, expected={expected_script_sha256}"
            )
    if not bool(job.get("no_agent")):
        failures.append("no_agent is not true")
    actual_workdir = Path(str(job.get("workdir", ""))).resolve()
    if actual_workdir != Path(expected_workdir).resolve():
        failures.append(f"workdir={actual_workdir}")
    enabled = job.get("enabled", job.get("state") not in {"paused", "completed"})
    if not enabled:
        failures.append("job is disabled")
    if failures:
        raise SchedulerContractError("Scheduler read-back mismatch: " + "; ".join(failures))
    return {
        "verified": True,
        "job_id": str(job.get("id", "")),
        "name": expected_name,
        "schedule": expected_expression,
        "script": str(expected_script),
        "script_sha256": expected_script_sha256,
        "workdir": str(Path(expected_workdir).resolve()),
        "no_agent": True,
    }
