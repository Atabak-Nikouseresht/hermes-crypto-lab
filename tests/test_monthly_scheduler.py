from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import paper_forward_monthly


def _project(tmp_path: Path, *, unix: bool = False) -> Path:
    project = tmp_path / "project"
    python = (
        project / ".venv" / "bin" / "python"
        if unix
        else project / ".venv" / "Scripts" / "python.exe"
    )
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="ascii")
    (project / "run_monthly_report.py").write_text("pass\n", encoding="ascii")
    return project


def test_monthly_launch_window_is_bounded_in_utc():
    assert not paper_forward_monthly.should_launch(
        datetime(2026, 9, 1, 8, 59, tzinfo=timezone.utc)
    )
    assert paper_forward_monthly.should_launch(
        datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    )
    assert paper_forward_monthly.should_launch(
        datetime(2026, 9, 1, 9, 15, 59, tzinfo=timezone.utc)
    )
    assert not paper_forward_monthly.should_launch(
        datetime(2026, 9, 1, 9, 16, tzinfo=timezone.utc)
    )
    assert not paper_forward_monthly.should_launch(
        datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    )


def test_monthly_failure_releases_claim_and_success_is_idempotent(tmp_path):
    project = _project(tmp_path)
    now = datetime(2026, 9, 1, 9, 5, tzinfo=timezone.utc)
    calls = []

    def failing(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7, stderr="failed", stdout="")

    assert paper_forward_monthly.main(now=now, project=project, run=failing) == 7

    def succeeding(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    assert paper_forward_monthly.main(now=now, project=project, run=succeeding) == 0
    assert paper_forward_monthly.main(now=now, project=project, run=succeeding) == 0
    assert len(calls) == 2
    assert (project / "runtime" / "monthly_dispatch_2026-09.completed").is_file()


def test_monthly_timeout_releases_claim_for_retry(tmp_path):
    project = _project(tmp_path)
    now = datetime(2026, 9, 1, 9, 5, tzinfo=timezone.utc)

    def timeout(*_args, **_kwargs):
        raise paper_forward_monthly.subprocess.TimeoutExpired("monthly", 600)

    assert paper_forward_monthly.main(now=now, project=project, run=timeout) == 124
    def success(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    assert paper_forward_monthly.main(now=now, project=project, run=success) == 0


def test_monthly_os_lock_prevents_concurrent_dispatch_and_is_crash_retryable(tmp_path):
    project = _project(tmp_path)
    now = datetime(2026, 9, 1, 9, 5, tzinfo=timezone.utc)
    first = paper_forward_monthly.claim_dispatch(now, project=project)
    assert first is not None
    assert paper_forward_monthly.claim_dispatch(now, project=project) is None

    first.release()  # OS locks are likewise released automatically if the process dies.
    retry = paper_forward_monthly.claim_dispatch(now, project=project)
    assert retry is not None
    retry.release()


def test_stale_completion_temp_does_not_block_success(tmp_path):
    project = _project(tmp_path)
    now = datetime(2026, 9, 1, 9, 5, tzinfo=timezone.utc)
    claim = paper_forward_monthly.claim_dispatch(now, project=project)
    assert claim is not None
    _inprogress, completed = paper_forward_monthly._marker_paths(now, project)
    completed.with_suffix(".completed.tmp").write_text("stale", encoding="ascii")

    claim.complete()

    assert completed.exists()


def test_completed_dispatch_remains_idempotent_after_lock_release(tmp_path):
    project = _project(tmp_path)
    now = datetime(2026, 9, 1, 9, 5, tzinfo=timezone.utc)
    claim = paper_forward_monthly.claim_dispatch(now, project=project)
    assert claim is not None
    claim.complete()

    assert paper_forward_monthly.claim_dispatch(now, project=project) is None


def test_monthly_interpreter_resolution_prefers_windows_and_supports_unix(tmp_path):
    windows_project = _project(tmp_path / "windows")
    assert paper_forward_monthly.resolve_python(windows_project).name == "python.exe"

    unix_project = _project(tmp_path / "unix", unix=True)
    assert paper_forward_monthly.resolve_python(unix_project) == (
        unix_project / ".venv" / "bin" / "python"
    )

    with pytest.raises(FileNotFoundError, match="project virtual-environment interpreter"):
        paper_forward_monthly.resolve_python(tmp_path / "missing")
