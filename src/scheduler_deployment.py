"""Normalize and verify live Hermes and Windows scheduler read-backs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any

from src.scheduler_contract import SchedulerContractError, verify_scheduler_job

JOB_KEYS = ("weekly_job", "missed_audit_job", "monthly_job", "canary_job")
_FIELD_PATTERN = re.compile(
    r"^ {4}(Name|Schedule|Next run|Script|Mode|Workdir):\s*(.*?)\s*$"
)
_HEADER_PATTERN = re.compile(r"^ {2}([^\s\[]+)\s+\[([^\]]+)\]$")
_HERMES_VERSION_LINE = re.compile(
    r"^Hermes Agent v(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))(?:\s.*)?$"
)
_SEMVER = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_NO_AGENT_MODE = "no-agent (script stdout delivered directly)"


class SchedulerDeploymentError(ValueError):
    """Raised when an installed scheduler differs from its portable contract."""


def verify_hermes_timezone(
    effective_timezone: object, *, required_timezone: object
) -> dict[str, object]:
    """Require an explicit Hermes timezone read-back matching the UTC contract."""
    if type(required_timezone) is not str or required_timezone != "UTC":
        raise SchedulerDeploymentError("required Hermes timezone contract must be UTC")
    if type(effective_timezone) is not str or not effective_timezone.strip():
        raise SchedulerDeploymentError("effective Hermes timezone is missing or malformed")
    if effective_timezone != required_timezone:
        raise SchedulerDeploymentError(
            "effective Hermes timezone differs from required UTC"
        )
    return {
        "verified": True,
        "required_timezone": required_timezone,
        "effective_timezone": effective_timezone,
    }


def parse_hermes_version_output(output: object) -> str:
    """Parse the installed Hermes CLI's stable first-line version marker."""
    if type(output) is not str or not output.strip():
        raise SchedulerDeploymentError("Hermes version output is missing or malformed")
    first_line = output.splitlines()[0]
    match = _HERMES_VERSION_LINE.fullmatch(first_line)
    if match is None:
        raise SchedulerDeploymentError("Hermes version output is missing or malformed")
    return match.group("version")


def verify_hermes_version(
    installed_version: object, *, required_version: object
) -> dict[str, object]:
    """Require the exact, explicitly governed Hermes runtime version."""
    if type(required_version) is not str or _SEMVER.fullmatch(required_version) is None:
        raise SchedulerDeploymentError("required Hermes version is missing or malformed")
    if type(installed_version) is not str or _SEMVER.fullmatch(installed_version) is None:
        raise SchedulerDeploymentError("installed Hermes version is missing or malformed")
    if installed_version != required_version:
        raise SchedulerDeploymentError(
            "installed Hermes version differs from the required runtime version"
        )
    return {
        "verified": True,
        "required_version": required_version,
        "installed_version": installed_version,
    }


def parse_hermes_config_path(output: object) -> Path:
    """Resolve the active Hermes config location without opening its contents."""
    if type(output) is not str or not output:
        raise SchedulerDeploymentError("Hermes config path output is missing or malformed")
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0].strip() != lines[0]:
        raise SchedulerDeploymentError("Hermes config path output is missing or malformed")
    path = Path(lines[0]).expanduser()
    if not path.is_absolute() or path.name.casefold() != "config.yaml":
        raise SchedulerDeploymentError("Hermes config path is not an absolute config.yaml path")
    try:
        return path.resolve()
    except (OSError, ValueError) as error:
        raise SchedulerDeploymentError("Hermes config path is malformed") from error


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise SchedulerDeploymentError(f"{label} must be a JSON object")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise SchedulerDeploymentError(f"{label} must be a non-empty string")
    return value


def _contract_jobs(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    expected: dict[str, Mapping[str, Any]] = {}
    for key in JOB_KEYS:
        spec = _mapping(contract.get(key), f"scheduler contract {key}")
        name = _string(spec.get("name"), f"{key}.name")
        _string(spec.get("hermes_trigger"), f"{key}.hermes_trigger")
        _string(spec.get("script"), f"{key}.script")
        digest = _string(spec.get("wrapper_sha256"), f"{key}.wrapper_sha256")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SchedulerDeploymentError(f"{key}.wrapper_sha256 is malformed")
        if spec.get("workdir") != "[PROJECT_ROOT]":
            raise SchedulerDeploymentError(f"{key}.workdir is not portable")
        if name in expected:
            raise SchedulerDeploymentError("scheduler contract has duplicate job names")
        expected[name] = spec
    return expected


def parse_hermes_cron_list(
    output: str,
    *,
    expected_names: set[str],
    project_root: Path,
    scripts_root: Path | None = None,
) -> list[dict[str, object]]:
    """Normalize the text emitted by the installed `hermes cron list --all` CLI."""
    if type(output) is not str or not output.strip():
        raise SchedulerDeploymentError("Hermes cron list returned empty output")
    if type(expected_names) is not set or any(
        type(name) is not str or not name.strip() for name in expected_names
    ):
        raise SchedulerDeploymentError("expected Hermes job names are malformed")

    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in output.splitlines():
        header = _HEADER_PATTERN.fullmatch(line.rstrip("\r"))
        if header is not None:
            if current is not None:
                blocks.append(current)
            current = {"id": header.group(1), "state": header.group(2), "fields": {}}
            continue
        if current is None:
            continue
        field = _FIELD_PATTERN.fullmatch(line.rstrip("\r"))
        if field is not None:
            label, value = field.groups()
            fields = current["fields"]
            if label in fields:
                raise SchedulerDeploymentError(
                    f"Hermes cron list repeated the {label} field"
                )
            fields[label] = value
    if current is not None:
        blocks.append(current)

    jobs: list[dict[str, object]] = []
    project = Path(project_root).resolve()
    runtime_scripts_root = Path(scripts_root or project / "scripts").resolve()
    for block in blocks:
        fields = block["fields"]
        name = fields.get("Name")
        if type(name) is not str:
            raise SchedulerDeploymentError("Hermes cron list omitted a job name")
        if name not in expected_names:
            continue
        required = ("Schedule", "Next run", "Script", "Mode", "Workdir")
        if any(type(fields.get(key)) is not str or not fields[key] for key in required):
            raise SchedulerDeploymentError(f"Hermes job {name} has incomplete read-back")
        try:
            next_run = datetime.fromisoformat(fields["Next run"].replace("Z", "+00:00"))
        except ValueError as error:
            raise SchedulerDeploymentError(
                f"Hermes job {name} has a malformed next-run timestamp"
            ) from error
        if next_run.tzinfo is None or next_run.utcoffset() != timedelta(0):
            raise SchedulerDeploymentError(
                f"Hermes job {name} next-run timestamp is not UTC"
            )
        script_name = fields["Script"]
        if (
            Path(script_name).name != script_name
            or "/" in script_name
            or "\\" in script_name
            or script_name in {".", ".."}
        ):
            raise SchedulerDeploymentError(f"Hermes job {name} has a non-basename script")
        script_path = (runtime_scripts_root / script_name).resolve()
        try:
            script_path.relative_to(runtime_scripts_root)
        except ValueError as error:
            raise SchedulerDeploymentError(
                f"Hermes job {name} script escapes the installed Hermes scripts directory"
            ) from error
        if not script_path.is_file():
            raise SchedulerDeploymentError(f"Hermes job {name} wrapper is missing")
        state = block["state"]
        if state not in {"active", "paused", "completed"}:
            raise SchedulerDeploymentError(f"Hermes job {name} has an unknown state")
        jobs.append(
            {
                "id": block["id"],
                "name": name,
                "schedule": {"kind": "cron", "expr": fields["Schedule"]},
                "next_run_timezone": "UTC",
                "script": script_name,
                "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
                "no_agent": fields["Mode"] == _NO_AGENT_MODE,
                "workdir": fields["Workdir"],
                "enabled": state == "active",
            }
        )
    return jobs


def verify_hermes_jobs(
    jobs: object,
    *,
    contract: Mapping[str, Any],
    project_root: Path,
    scripts_root: Path | None = None,
) -> dict[str, object]:
    """Verify exactly one strictly typed read-back for each governed Hermes job."""
    if type(jobs) is not list:
        raise SchedulerDeploymentError("Hermes read-back jobs must be a JSON array")
    expected = _contract_jobs(contract)
    root = Path(project_root).resolve()
    runtime_scripts_root = Path(scripts_root or root / "scripts").resolve()
    names: set[str] = set()
    identifiers: set[str] = set()
    verified: list[dict[str, Any]] = []

    for index, raw_job in enumerate(jobs):
        job = _mapping(raw_job, f"Hermes read-back job {index}")
        expected_fields = {
            "enabled",
            "id",
            "name",
            "next_run_timezone",
            "no_agent",
            "schedule",
            "script",
            "script_sha256",
            "workdir",
        }
        if set(job) != expected_fields:
            raise SchedulerDeploymentError(
                f"Hermes read-back job {index} has an unexpected schema"
            )
        name = job.get("name")
        if type(name) is not str or name not in expected:
            raise SchedulerDeploymentError(
                f"Hermes read-back contains an unknown or malformed job at index {index}"
            )
        job_id = job.get("id")
        if type(job_id) is not str or not job_id.strip():
            raise SchedulerDeploymentError(f"Hermes job {name} has an invalid identity")
        if name in names or job_id in identifiers:
            raise SchedulerDeploymentError("Hermes read-back has duplicate job names or IDs")
        names.add(name)
        identifiers.add(job_id)
        spec = expected[name]
        gateway_contract = contract.get("hermes_gateway")
        if type(gateway_contract) is not dict:
            raise SchedulerDeploymentError("Hermes gateway timezone contract is malformed")
        verify_hermes_timezone(
            job.get("next_run_timezone"),
            required_timezone=gateway_contract.get("timezone_config"),
        )
        script = _string(spec.get("script"), f"{name}.script")
        try:
            result = verify_scheduler_job(
                dict(job),
                expected_name=name,
                expected_expression=_string(
                    spec.get("hermes_trigger"), f"{name}.hermes_trigger"
                ),
                expected_script=runtime_scripts_root / script,
                expected_workdir=root,
                scripts_root=runtime_scripts_root,
                expected_script_sha256=_string(
                    spec.get("wrapper_sha256"), f"{name}.wrapper_sha256"
                ),
            )
        except SchedulerContractError as error:
            raise SchedulerDeploymentError(str(error)) from error
        verified.append(result)

    if names != set(expected):
        missing = sorted(set(expected) - names)
        raise SchedulerDeploymentError(
            "Hermes read-back is missing governed jobs: " + ", ".join(missing)
        )
    return {"verified": True, "job_count": len(verified), "jobs": verified}


def _same_path(actual: str, expected: Path) -> bool:
    actual_windows = PureWindowsPath(actual)
    if actual_windows.is_absolute():
        return str(actual_windows).casefold() == str(PureWindowsPath(expected)).casefold()
    return Path(actual).resolve() == Path(expected).resolve()


def verify_windows_watchdog(
    readback: object,
    *,
    contract: Mapping[str, Any],
    project_root: Path,
) -> dict[str, object]:
    """Compare the live Task Scheduler action, flags, restart, and trigger state."""
    task = _mapping(readback, "Windows Task Scheduler read-back")
    failures: list[str] = []
    if set(task) != {"task_name", "enabled", "actions", "settings", "triggers"}:
        failures.append("Windows Task Scheduler read-back has an unexpected schema")

    task_name = _string(task.get("task_name"), "Windows task_name")
    expected_name = _string(contract.get("task_name"), "watchdog.task_name")
    if task_name != expected_name:
        failures.append("task name differs from the static contract")
    if type(task.get("enabled")) is not bool or task.get("enabled") is not True:
        failures.append("task enabled state is not the exact boolean true")
    if contract.get("enabled") is not True:
        failures.append("static watchdog enabled contract is invalid")

    action_contract = _mapping(contract.get("action"), "watchdog.action")
    actions = task.get("actions")
    if type(actions) is not list or len(actions) != 1:
        failures.append("task must have exactly one execution action")
    else:
        action = _mapping(actions[0], "watchdog action")
        if set(action) != {"execute", "arguments", "working_directory"}:
            failures.append("task action has an unexpected schema")
        executable = _string(action.get("execute"), "watchdog action.execute")
        executable_path = PureWindowsPath(executable)
        expected_executable = _string(
            action_contract.get("executable"), "watchdog.action.executable"
        )
        suffix = tuple(part.casefold() for part in executable_path.parts[-4:])
        expected_suffix = (
            "system32",
            "windowspowershell",
            "v1.0",
            expected_executable.casefold(),
        )
        if (
            not executable_path.is_absolute()
            or executable_path.name.casefold() != expected_executable.casefold()
            or suffix != expected_suffix
        ):
            failures.append("task executable is not the governed Windows PowerShell path")

        arguments = _string(action.get("arguments"), "watchdog action.arguments")
        prefix = _string(
            action_contract.get("arguments_prefix"),
            "watchdog.action.arguments_prefix",
        )
        marker = prefix + ' "'
        if not arguments.startswith(marker) or not arguments.endswith('"'):
            failures.append("task arguments differ from the governed invocation")
        else:
            target_script = arguments[len(marker) : -1]
            expected_script = Path(project_root) / _string(
                action_contract.get("script"), "watchdog.action.script"
            )
            if not _same_path(target_script, expected_script):
                failures.append("task action script path differs from the governed wrapper")

        workdir = _string(
            action.get("working_directory"), "watchdog action.working_directory"
        )
        if contract.get("working_directory") != "[PROJECT_ROOT]" or not _same_path(
            workdir, Path(project_root)
        ):
            failures.append("task action working directory differs from the project root")

    settings = _mapping(task.get("settings"), "watchdog settings")
    if set(settings) != {
        "multiple_instances",
        "restart_count",
        "restart_interval",
        "run_only_if_network_available",
        "start_when_available",
    }:
        failures.append("task settings have an unexpected schema")
    expected_restart = _mapping(
        contract.get("restart_on_failure"), "watchdog.restart_on_failure"
    )
    expected_settings: tuple[tuple[str, object, type], ...] = (
        ("multiple_instances", contract.get("multiple_instances"), str),
        (
            "run_only_if_network_available",
            contract.get("run_only_if_network_available"),
            bool,
        ),
        ("start_when_available", contract.get("start_when_available"), bool),
        ("restart_count", expected_restart.get("count"), int),
        ("restart_interval", expected_restart.get("interval"), str),
    )
    for key, expected_value, expected_type in expected_settings:
        actual_value = settings.get(key)
        if type(actual_value) is not expected_type or actual_value != expected_value:
            failures.append(f"task setting {key} differs from the static contract")

    expected_triggers = contract.get("triggers")
    actual_triggers = task.get("triggers")
    trigger_count = 0
    if type(expected_triggers) is not list or type(actual_triggers) is not list:
        failures.append("task triggers must be JSON arrays")
    elif len(actual_triggers) != len(expected_triggers):
        failures.append("task trigger count differs from the static contract")
    else:
        trigger_count = len(expected_triggers)
        for index, (actual_item, expected_item) in enumerate(
            zip(actual_triggers, expected_triggers, strict=True)
        ):
            actual_trigger = _mapping(actual_item, f"watchdog trigger {index}")
            expected_trigger = _mapping(
                expected_item, f"watchdog contract trigger {index}"
            )
            if set(actual_trigger) != set(expected_trigger):
                failures.append(f"task trigger {index} has an unexpected schema")
            for key in (
                "type",
                "enabled",
                "repetition_interval",
                "repetition_duration",
            ):
                expected_value = expected_trigger.get(key)
                actual_value = actual_trigger.get(key)
                if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                    failures.append(f"task trigger {index} {key} differs from contract")

    if failures:
        raise SchedulerDeploymentError("Windows watchdog mismatch: " + "; ".join(failures))
    return {
        "verified": True,
        "task_name": expected_name,
        "enabled": True,
        "action_count": 1,
        "trigger_count": trigger_count,
    }
