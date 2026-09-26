from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from src.scheduler_deployment import (
    SchedulerDeploymentError,
    parse_hermes_cron_list,
    verify_hermes_jobs,
    verify_windows_watchdog,
)
from scripts.verify_scheduler_deployment import (
    DeploymentVerificationError,
    export_hermes_readback,
    load_hermes_readback,
)


WATCHDOG_CONTRACT = {
    "action": {
        "arguments_prefix": (
            "-NoProfile -NonInteractive -WindowStyle Hidden "
            "-ExecutionPolicy Bypass -File"
        ),
        "executable": "powershell.exe",
        "script": "scripts/hermes_gateway_watchdog.ps1",
    },
    "enabled": True,
    "multiple_instances": "IgnoreNew",
    "restart_on_failure": {"count": 3, "interval": "PT5M"},
    "run_only_if_network_available": True,
    "start_when_available": True,
    "task_name": "Hermes_Crypto_Lab_Watchdog",
    "triggers": [
        {"type": "logon", "enabled": True},
        {
            "type": "daily",
            "enabled": True,
            "repetition_interval": "PT15M",
            "repetition_duration": "P1D",
        },
    ],
    "working_directory": "[PROJECT_ROOT]",
}


JOB_SPECS = {
    "weekly_job": (
        "crypto-paper-forward-weekly",
        "10 0 * * 1",
        "paper_forward_weekly.py",
    ),
    "missed_audit_job": (
        "crypto-paper-forward-missed-audit",
        "21 0 * * 1",
        "paper_forward_audit.py",
    ),
    "monthly_job": (
        "crypto-paper-forward-monthly",
        "0 9 1 * *",
        "paper_forward_monthly.py",
    ),
}


def _write_jobs(project_root: Path) -> dict[str, dict[str, str]]:
    scripts_root = project_root / "scripts"
    scripts_root.mkdir(parents=True)
    contract = {}
    for index, (key, (name, expression, filename)) in enumerate(JOB_SPECS.items()):
        wrapper = scripts_root / filename
        wrapper.write_text(f"approved wrapper {index}\n", encoding="utf-8")
        contract[key] = {
            "name": name,
            "hermes_trigger": expression,
            "script": filename,
            "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
            "workdir": "[PROJECT_ROOT]",
        }
    return contract


def _jobs_readback(project_root: Path) -> list[dict[str, object]]:
    jobs = []
    for index, (name, expression, filename) in enumerate(JOB_SPECS.values()):
        script = project_root / "scripts" / filename
        jobs.append(
            {
                "id": f"job-{index}",
                "name": name,
                "schedule": {"kind": "cron", "expr": expression},
                "script": filename,
                "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "no_agent": True,
                "workdir": str(project_root),
                "enabled": True,
            }
        )
    return jobs


def _windows_readback(project_root: Path) -> dict[str, object]:
    script = project_root / WATCHDOG_CONTRACT["action"]["script"]
    arguments = (
        f'{WATCHDOG_CONTRACT["action"]["arguments_prefix"]} "{script}"'
    )
    return {
        "task_name": WATCHDOG_CONTRACT["task_name"],
        "enabled": True,
        "actions": [
            {
                "execute": (
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
                ),
                "arguments": arguments,
                "working_directory": str(project_root),
            }
        ],
        "settings": {
            "multiple_instances": "IgnoreNew",
            "run_only_if_network_available": True,
            "start_when_available": True,
            "restart_count": 3,
            "restart_interval": "PT5M",
        },
        "triggers": [
            {"type": "logon", "enabled": True},
            {
                "type": "daily",
                "enabled": True,
                "repetition_interval": "PT15M",
                "repetition_duration": "P1D",
            },
        ],
    }


def test_hermes_cli_text_export_maps_only_observed_scheduler_fields(tmp_path):
    _write_jobs(tmp_path)
    output = (
        "Scheduled Jobs\n"
        "\n"
        "  abc123def456 [active]\n"
        "    Name:      crypto-paper-forward-weekly\n"
        "    Schedule:  10 0 * * 1\n"
        "    Script:    paper_forward_weekly.py\n"
        "    Mode:      no-agent (script stdout delivered directly)\n"
        f"    Workdir:   {tmp_path}\n"
    )

    jobs = parse_hermes_cron_list(
        output,
        expected_names={name for name, _, _ in JOB_SPECS.values()},
        project_root=tmp_path,
    )

    assert jobs == [
        {
            "id": "abc123def456",
            "name": "crypto-paper-forward-weekly",
            "schedule": {"kind": "cron", "expr": "10 0 * * 1"},
            "script": "paper_forward_weekly.py",
            "script_sha256": hashlib.sha256(
                (tmp_path / "scripts" / "paper_forward_weekly.py").read_bytes()
            ).hexdigest(),
            "no_agent": True,
            "workdir": str(tmp_path),
            "enabled": True,
        }
    ]


@pytest.mark.parametrize(
    "output",
    [
        "  abc123 [active]\n    Name: crypto-paper-forward-weekly\n",
        "  abc123def456 [unknown]\n    Name: crypto-paper-forward-weekly\n",
        "  abc123def456 [active]\n    Name: crypto-paper-forward-weekly\n"
        "    Schedule: 10 0 * * 1\n    Script: ../paper_forward_weekly.py\n"
        "    Mode: no-agent (script stdout delivered directly)\n"
        "    Workdir: /project\n",
    ],
)
def test_hermes_cli_parser_fails_closed_on_incomplete_or_unknown_output(
    tmp_path, output
):
    _write_jobs(tmp_path)

    with pytest.raises(SchedulerDeploymentError):
        parse_hermes_cron_list(
            output,
            expected_names={name for name, _, _ in JOB_SPECS.values()},
            project_root=tmp_path,
        )


def test_hermes_readback_verifies_exact_jobs_and_rejects_duplicates(tmp_path):
    contract = _write_jobs(tmp_path)
    jobs = _jobs_readback(tmp_path)

    verified = verify_hermes_jobs(jobs, contract=contract, project_root=tmp_path)
    assert verified["verified"] is True
    assert verified["job_count"] == 3

    duplicate = copy.deepcopy(jobs)
    duplicate.append(copy.deepcopy(jobs[0]))
    with pytest.raises(SchedulerDeploymentError):
        verify_hermes_jobs(duplicate, contract=contract, project_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", "true"),
        ("no_agent", 1),
        ("id", True),
        ("workdir", None),
        ("script_sha256", "0" * 64),
    ],
)
def test_hermes_readback_rejects_wrong_types_and_stale_wrapper_hash(
    tmp_path, field, value
):
    contract = _write_jobs(tmp_path)
    jobs = _jobs_readback(tmp_path)
    jobs[0][field] = value

    with pytest.raises(SchedulerDeploymentError):
        verify_hermes_jobs(jobs, contract=contract, project_root=tmp_path)


@pytest.mark.parametrize(
    ("area", "field", "value"),
    [
        ("action", "arguments", "-NoProfile -File other.ps1"),
        ("action", "working_directory", "C:/other/project"),
        ("action", "execute", "C:/Windows/System32/not-powershell.exe"),
        ("settings", "run_only_if_network_available", False),
        ("settings", "restart_interval", "PT1M"),
    ],
)
def test_windows_watchdog_verifier_detects_actual_action_and_setting_drift(
    tmp_path, area, field, value
):
    readback = _windows_readback(tmp_path)
    if area == "action":
        readback["actions"][0][field] = value
    else:
        readback["settings"][field] = value

    with pytest.raises(SchedulerDeploymentError):
        verify_windows_watchdog(
            readback, contract=WATCHDOG_CONTRACT, project_root=tmp_path
        )


def test_windows_watchdog_verifier_rejects_trigger_drift_and_duplicate_actions(tmp_path):
    readback = _windows_readback(tmp_path)
    readback["triggers"][1]["repetition_duration"] = "P2D"
    with pytest.raises(SchedulerDeploymentError):
        verify_windows_watchdog(
            readback, contract=WATCHDOG_CONTRACT, project_root=tmp_path
        )

    readback = _windows_readback(tmp_path)
    readback["actions"].append(copy.deepcopy(readback["actions"][0]))
    with pytest.raises(SchedulerDeploymentError):
        verify_windows_watchdog(
            readback, contract=WATCHDOG_CONTRACT, project_root=tmp_path
        )


def test_deployment_export_uses_supported_cli_and_round_trips_json(
    tmp_path, monkeypatch
):
    import subprocess

    import scripts.verify_scheduler_deployment as deployment

    project_root = tmp_path / "project"
    contract = _write_jobs(project_root)
    blocks = []
    for index, (name, expression, filename) in enumerate(JOB_SPECS.values()):
        blocks.append(
            f"  job-{index} [active]\n"
            f"    Name: {name}\n"
            f"    Schedule: {expression}\n"
            f"    Script: {filename}\n"
            "    Mode: no-agent (script stdout delivered directly)\n"
            f"    Workdir: {project_root}\n"
        )
    cli_output = "Scheduled Jobs\n\n" + "\n".join(blocks)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, cli_output, "")

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    destination = tmp_path / "hermes-readback.json"
    exported = export_hermes_readback(
        destination, contract=contract, project_root=project_root
    )

    jobs = load_hermes_readback(exported)
    assert len(jobs) == len(JOB_SPECS)
    assert calls[0][0] == ("hermes", "cron", "list", "--all")
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True


def test_deployment_export_and_json_loader_fail_closed(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    contract = {
        key: {
            "name": name,
            "hermes_trigger": expression,
            "script": filename,
            "wrapper_sha256": "0" * 64,
            "workdir": "[PROJECT_ROOT]",
        }
        for key, (name, expression, filename) in JOB_SPECS.items()
    }

    with pytest.raises(DeploymentVerificationError, match="outside the Git repository"):
        export_hermes_readback(
            project_root / "readback.json",
            contract=contract,
            project_root=project_root,
        )

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"jobs": []}', encoding="utf-8")
    with pytest.raises(DeploymentVerificationError, match="schema"):
        load_hermes_readback(malformed)
