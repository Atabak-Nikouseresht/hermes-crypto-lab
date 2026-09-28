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
    JOB_KEYS,
    SchedulerDeploymentError,
    parse_hermes_cron_list,
    parse_hermes_config_path,
    parse_hermes_version_output,
    verify_hermes_timezone,
    verify_hermes_version,
    verify_hermes_jobs,
    verify_windows_watchdog,
)
from scripts.verify_scheduler_manifest import verify as verify_static_scheduler_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "forward_experiment" / "scheduler_manifest.json"
WINDOWS_READBACK_SCRIPT = PROJECT_ROOT / "scripts" / "read_windows_task_scheduler.ps1"
HERMES_COMMAND = ("hermes", "cron", "list", "--all")
HERMES_VERSION_COMMAND = ("hermes", "--version")
HERMES_CONFIG_PATH_COMMAND = ("hermes", "config", "path")
HERMES_TIMEZONE_COMMAND = ("hermes", "config", "get", "timezone")
READBACK_SCHEMA_VERSION = 4


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
    try:
        version_result = subprocess.run(
            HERMES_VERSION_COMMAND,
            cwd=project_root,
            capture_output=True,
            check=False,
            timeout=30,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DeploymentVerificationError(
            "could not execute the supported Hermes version command"
        ) from error
    if version_result.returncode != 0:
        raise DeploymentVerificationError(
            "Hermes version command failed; raw command output was suppressed"
        )
    try:
        installed_hermes_version = parse_hermes_version_output(version_result.stdout)
    except SchedulerDeploymentError as error:
        raise DeploymentVerificationError(
            "Hermes version output was missing or malformed"
        ) from error

    expected_names: set[str] = set()
    for key in JOB_KEYS:
        spec = contract.get(key)
        if type(spec) is not dict or type(spec.get("name")) is not str:
            raise DeploymentVerificationError(f"scheduler contract {key} is malformed")
        expected_names.add(spec["name"])

    try:
        config_path_result = subprocess.run(
            HERMES_CONFIG_PATH_COMMAND,
            cwd=project_root,
            capture_output=True,
            check=False,
            timeout=30,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DeploymentVerificationError(
            "could not execute the supported Hermes config path command"
        ) from error
    if config_path_result.returncode != 0:
        raise DeploymentVerificationError(
            "Hermes config path command failed; raw command output was suppressed"
        )
    try:
        config_path = parse_hermes_config_path(config_path_result.stdout)
    except SchedulerDeploymentError as error:
        raise DeploymentVerificationError(
            "Hermes config path output was missing or malformed"
        ) from error
    scripts_root = config_path.parent / "scripts"
    if not scripts_root.is_dir():
        raise DeploymentVerificationError(
            "installed Hermes scripts directory is missing"
        )

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
            scripts_root=scripts_root,
        )
    except SchedulerDeploymentError as error:
        raise DeploymentVerificationError(
            "Hermes cron list output did not match the supported read-back format"
        ) from error

    try:
        timezone_result = subprocess.run(
            HERMES_TIMEZONE_COMMAND,
            cwd=project_root,
            capture_output=True,
            check=False,
            timeout=30,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DeploymentVerificationError(
            "could not execute the supported Hermes timezone read-back command"
        ) from error
    if timezone_result.returncode != 0:
        raise DeploymentVerificationError(
            "Hermes timezone read-back failed; raw command output was suppressed"
        )
    if type(timezone_result.stdout) is not str:
        raise DeploymentVerificationError("Hermes timezone read-back was malformed")
    timezone_lines = timezone_result.stdout.splitlines()
    if (
        len(timezone_lines) != 1
        or not timezone_lines[0]
        or timezone_lines[0].strip() != timezone_lines[0]
    ):
        raise DeploymentVerificationError(
            "Hermes timezone read-back was missing or malformed"
        )
    effective_timezone = timezone_lines[0]

    payload = {
        "effective_timezone": effective_timezone,
        "schema_version": READBACK_SCHEMA_VERSION,
        "source_command": "hermes cron list --all",
        "timezone_source_command": "hermes config get timezone",
        "installed_hermes_version": installed_hermes_version,
        "version_source_command": "hermes --version",
        "scripts_root": str(scripts_root.resolve()),
        "jobs": jobs,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return target


def _load_hermes_readback_payload(path: Path) -> dict[str, Any]:
    """Load only the explicit JSON schema emitted by the Hermes export adapter."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentVerificationError(
            "Hermes read-back file is missing or malformed"
        ) from error
    if (
        type(payload) is not dict
        or set(payload)
        != {
            "schema_version",
            "source_command",
            "jobs",
            "effective_timezone",
            "timezone_source_command",
            "installed_hermes_version",
            "version_source_command",
            "scripts_root",
        }
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != READBACK_SCHEMA_VERSION
        or payload.get("source_command") != "hermes cron list --all"
        or payload.get("timezone_source_command") != "hermes config get timezone"
        or payload.get("version_source_command") != "hermes --version"
        or type(payload.get("installed_hermes_version")) is not str
        or type(payload.get("scripts_root")) is not str
        or not Path(payload["scripts_root"]).is_absolute()
        or type(payload.get("jobs")) is not list
        or type(payload.get("effective_timezone")) is not str
    ):
        raise DeploymentVerificationError("Hermes read-back JSON schema is invalid")
    return payload


def load_hermes_readback(path: Path) -> list[dict[str, Any]]:
    """Load governed Hermes jobs while preserving the established list API."""
    return _load_hermes_readback_payload(path)["jobs"]


def load_hermes_timezone_readback(path: Path) -> str:
    """Load the resolved timezone captured by the supported Hermes CLI."""
    return _load_hermes_readback_payload(path)["effective_timezone"]


def load_hermes_version_readback(path: Path) -> str:
    """Load the actual installed Hermes version captured by the CLI adapter."""
    return _load_hermes_readback_payload(path)["installed_hermes_version"]


def load_hermes_scripts_root_readback(path: Path) -> Path:
    """Load the installed Hermes script directory captured by the CLI adapter."""
    return Path(_load_hermes_readback_payload(path)["scripts_root"])


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
            gateway_contract = contract.get("hermes_gateway")
            if type(gateway_contract) is not dict:
                raise DeploymentVerificationError("Hermes gateway contract is malformed")
            try:
                version_result = verify_hermes_version(
                    load_hermes_version_readback(readback_file),
                    required_version=gateway_contract.get("required_version"),
                )
            except SchedulerDeploymentError as error:
                raise DeploymentVerificationError(str(error)) from error
            try:
                timezone_result = verify_hermes_timezone(
                    load_hermes_timezone_readback(readback_file),
                    required_timezone=gateway_contract.get("timezone_config"),
                )
            except SchedulerDeploymentError as error:
                raise DeploymentVerificationError(str(error)) from error
            verified = verify_hermes_jobs(
                jobs,
                contract=contract,
                project_root=PROJECT_ROOT,
                scripts_root=load_hermes_scripts_root_readback(readback_file),
            )
            results.append(
                "Hermes runtime version: PASS "
                f"({version_result['installed_version']})"
            )
            results.append(
                "Hermes effective timezone: PASS "
                f"({timezone_result['effective_timezone']})"
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
