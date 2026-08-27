"""Verify the portable scheduler snapshot and tracked wrapper hashes."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PROTOCOL = "paper-exec-v3-ask-bid-minspread-utc0010"
JOBS = {
    "weekly_job": (
        "scripts/paper_forward_weekly.py",
        "10 0 * * 1",
        "crypto-paper-forward-weekly",
    ),
    "missed_audit_job": (
        "scripts/paper_forward_audit.py",
        "21 0 * * 1",
        "crypto-paper-forward-missed-audit",
    ),
    "monthly_job": (
        "scripts/paper_forward_monthly.py",
        "0 9 1 * *",
        "crypto-paper-forward-monthly",
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(project_root: Path = PROJECT_ROOT) -> dict:
    manifest_path = project_root / "forward_experiment" / "scheduler_manifest.json"
    sidecar_path = Path(str(manifest_path) + ".sha256")
    expected_manifest_hash = sidecar_path.read_text(encoding="ascii").split()[0]
    if _sha256(manifest_path) != expected_manifest_hash:
        raise ValueError("scheduler manifest sidecar hash mismatch")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("execution_protocol") != EXPECTED_PROTOCOL:
        raise ValueError("scheduler execution protocol mismatch")

    verified = {}
    for key, (relative_wrapper, expected_trigger, expected_name) in JOBS.items():
        job = payload.get(key) or {}
        wrapper = project_root / relative_wrapper
        if job.get("name") != expected_name:
            raise ValueError(f"{key} name mismatch")
        if job.get("hermes_trigger") != expected_trigger:
            raise ValueError(f"{key} trigger mismatch")
        if job.get("script") != wrapper.name:
            raise ValueError(f"{key} script mismatch")
        if job.get("enabled") is not True:
            raise ValueError(f"{key} is not enabled")
        if job.get("no_agent") is not True:
            raise ValueError(f"{key} no_agent mismatch")
        if job.get("workdir") != "[PROJECT_ROOT]":
            raise ValueError(f"{key} contains a non-portable workdir")
        actual_hash = _sha256(wrapper)
        if job.get("wrapper_sha256") != actual_hash:
            raise ValueError(f"{key} wrapper hash mismatch")
        verified[key] = actual_hash

    watchdog = payload.get("windows_task_scheduler_watchdog") or {}
    if watchdog.get("working_directory") != "[PROJECT_ROOT]":
        raise ValueError("watchdog contains a non-portable working directory")

    return {
        "valid": True,
        "manifest_sha256": expected_manifest_hash,
        "execution_protocol": EXPECTED_PROTOCOL,
        "wrapper_hashes": verified,
    }


def main() -> None:
    try:
        print(json.dumps(verify(), indent=2, sort_keys=True))
    except Exception as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
