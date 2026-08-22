from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from scripts.paper_forward_weekly import should_launch
from src.scheduler_contract import SchedulerContractError, verify_scheduler_job


def test_scheduler_readback_contract_requires_exact_utc_schedule_path_hash_and_gate(tmp_path):
    scripts_root = tmp_path / "scripts"
    scripts_root.mkdir()
    approved = scripts_root / "paper_forward_weekly.py"
    approved.write_text("approved wrapper", encoding="utf-8")
    digest = hashlib.sha256(approved.read_bytes()).hexdigest()
    workdir = tmp_path / "project"
    workdir.mkdir()
    job = {
        "id": "abc123",
        "name": "crypto-paper-forward-weekly",
        "schedule": {"kind": "cron", "expr": "10 9 * * 1"},
        "script": "paper_forward_weekly.py",
        "no_agent": True,
        "workdir": str(workdir),
        "enabled": True,
    }

    verified = verify_scheduler_job(
        job,
        expected_name="crypto-paper-forward-weekly",
        expected_expression="10 9 * * 1",
        expected_script=approved,
        expected_workdir=workdir,
        scripts_root=scripts_root,
        expected_script_sha256=digest,
    )

    assert verified["job_id"] == "abc123"
    assert verified["verified"] is True
    assert should_launch(datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc))
    assert should_launch(datetime(2026, 8, 24, 9, 35, tzinfo=timezone.utc))
    assert not should_launch(datetime(2026, 8, 24, 9, 36, tzinfo=timezone.utc))
    assert not should_launch(datetime(2026, 8, 24, 9, 9, tzinfo=timezone.utc))

    wrong_root = tmp_path / "other"
    wrong_root.mkdir()
    (wrong_root / "paper_forward_weekly.py").write_text("approved wrapper", encoding="utf-8")
    wrong = {**job, "script": str(wrong_root / "paper_forward_weekly.py")}
    with pytest.raises(SchedulerContractError):
        verify_scheduler_job(
            wrong,
            expected_name="crypto-paper-forward-weekly",
            expected_expression="10 9 * * 1",
            expected_script=approved,
            expected_workdir=workdir,
            scripts_root=scripts_root,
            expected_script_sha256=digest,
        )
