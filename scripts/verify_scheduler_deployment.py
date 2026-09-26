"""Operator gate for live Hermes and Windows scheduler deployments.

Run from the repository root after every scheduler deployment or update.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.scheduler_deployment import (
    SchedulerDeploymentError,
    parse_hermes_cron_list,
    verify_hermes_jobs,
    verify_windows_watchdog,
)
from scripts.verify_scheduler_manifest import verify as verify_static_scheduler_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "forward_experiment" / "scheduler_manifest.json"
WINDOWS_READBACK_SCRIPT = PROJECT_ROOT / "scripts" / "read_windows_task_scheduler.ps1"
HERMES_COMMAND = ("hermes", "cron", "list", "--all")
READBACK_SCHEMA_VERSION = 1


class DeploymentVerificationError(ValueError):
    """Raised when the host read-back cannot be safely exported or verified."""


def default_readback_path() -> Path:
    """Store ephemeral deployment observations outside the Git repository."""
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (
            base
            / "hermes"
            / "cache"
            / "scratch"
            / "hermes-crypto-lab-scheduler-readback.json"
        )
    return (
        Path.home()
        / ".hermes"
        / "cache"
        / "scratch"
        / "hermes-crypto-lab-scheduler-readback.json"
    )


def _external_readback_path(path: Path, project_root: Path) -> Path:
    target = Path(path).expanduser().resolve()
    root = Path(project_root).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return target
    raise DeploymentVerificationError(
        "scheduler read-back files must stay outside the Git repository"
    )


def load_static_contract(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Validate the static manifest and return its JSON payload."""
    verify_static_scheduler_manifest(project_root)
    manifest_path = project_root / "forward_experiment" / "scheduler_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(payload) is not dict:
        raise DeploymentVerificationError("scheduler manifest must be a JSON object")
    return payload


def export_hermes_readback(
    destination: Path,
    *,
    contract: dict[str, Any],
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """Run the installed Hermes CLI and save a minimal JSON read-back export."""
    target = _external_readback_path(destination, project_root)
    expected_names: set[str] = set()
    for key in ("weekly_job", "missed_audit_job", "monthly_job"):
        spec = contract.get(key)
        if type(spec) is not dict or type(spec.get("name")) is not str:
            raise DeploymentVerificationError(f"scheduler contract {key} is malformed")
        expected_names.add(spec["name"])

    try:
        result = subprocess.run(
            HERMES_COMMAND,
            cwd=project_root,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise DeploymentVerificationError(
            "could not execute the supported Hermes cron list command"
        ) from error
    if result.returncode != 0:
        raise DeploymentVerificationError(
            "Hermes cron list --all failed; raw command output was suppressed"
        )

    try:
        jobs = parse_hermes_cron_list(
            result.stdout,
            expected_names=expected_names,
            project_root=project_root,
        )
    except SchedulerDeploymentError as error:
        raise DeploymentVerificationError(
            "Hermes cron list output did not match the supported read-back format"
        ) from error

    payload = {
        "schema_version": READBACK_SCHEMA_VERSION,
        "source_command": "hermes cron list --all",
        "jobs": jobs,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return target


def load_hermes_readback(path: Path) -> list[dict[str, Any]]:
    """Load only the explicit JSON schema emitted by the Hermes export adapter."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentVerificationError(
            "Hermes read-back file is missing or malformed"
        ) from error
    if (
        type(payload) is not dict
        or set(payload) != {"schema_version", "source_command", "jobs"}
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != READBACK_SCHEMA_VERSION
        or payload.get("source_command") != "hermes cron list --all"
        or type(payload.get("jobs")) is not list
    ):
        raise DeploymentVerificationError("Hermes read-back JSON schema is invalid")
    return payload["jobs"]


def read_windows_task_scheduler(
    *, task_name: str, project_root: Path = PROJECT_ROOT
) -> dict[str, Any]:
    """Capture PowerShell read-back without echoing raw task arguments to logs."""
    if os.name != "nt":
        raise DeploymentVerificationError(
            "Windows Task Scheduler verification must run on the deployment host"
        )
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WINDOWS_READBACK_SCRIPT),
                "-TaskName",
                task_name,
            ],
            cwd=project_root,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise DeploymentVerificationError(
            "could not execute the Windows Task Scheduler read-back script"
        ) from error
    if result.returncode != 0:
        raise DeploymentVerificationError(
            "Windows Task Scheduler read-back failed; raw command output was suppressed"
        )
    try:
        readback = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DeploymentVerificationError(
            "Windows Task Scheduler returned malformed JSON"
        ) from error
    if type(readback) is not dict:
        raise DeploymentVerificationError(
            "Windows Task Scheduler read-back must be a JSON object"
        )
    return readback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-hermes-cli",
        action="store_true",
        help="run the installed hermes cron list --all CLI and export JSON",
    )
    parser.add_argument(
        "--hermes-readback-file",
        type=Path,
        help="explicit Hermes JSON read-back to verify; defaults to Hermes scratch on export",
    )
    parser.add_argument(
        "--verify-windows-task",
        action="store_true",
        help="read and verify Hermes_Crypto_Lab_Watchdog on this Windows host",
    )
    args = parser.parse_args()
    if not args.export_hermes_cli and args.hermes_readback_file is None and not args.verify_windows_task:
        parser.error("request Hermes export/read-back and/or --verify-windows-task")

    try:
        contract = load_static_contract()
        results: list[str] = ["Static scheduler manifest: PASS"]

        readback_file = args.hermes_readback_file
        if args.export_hermes_cli:
            readback_file = readback_file or default_readback_path()
            readback_file = export_hermes_readback(readback_file, contract=contract)
        if readback_file is not None:
            readback_file = _external_readback_path(readback_file, PROJECT_ROOT)
            jobs = load_hermes_readback(readback_file)
            verified = verify_hermes_jobs(
                jobs, contract=contract, project_root=PROJECT_ROOT
            )
            results.append(
                f"Hermes scheduler read-back: PASS ({verified['job_count']} governed jobs)"
            )

        if args.verify_windows_task:
            watchdog_contract = contract.get("windows_task_scheduler_watchdog")
            if type(watchdog_contract) is not dict:
                raise DeploymentVerificationError("Windows watchdog contract is malformed")
            task_name = watchdog_contract.get("task_name")
            if type(task_name) is not str or not task_name.strip():
                raise DeploymentVerificationError("Windows watchdog task name is malformed")
            readback = read_windows_task_scheduler(task_name=task_name)
            verified_task = verify_windows_watchdog(
                readback, contract=watchdog_contract, project_root=PROJECT_ROOT
            )
            results.append(
                "Windows watchdog read-back: PASS "
                f"({verified_task['action_count']} action, "
                f"{verified_task['trigger_count']} triggers)"
            )
    except (DeploymentVerificationError, SchedulerDeploymentError, OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("\n".join(results))
    if readback_file is not None:
        print("Hermes JSON read-back saved outside the repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
