"""Hermes no-agent UTC gate for the forward-only monthly report."""

import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

try:
    from scripts.interpreter import resolve_project_python
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from interpreter import resolve_project_python

PROJECT = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT_SECONDS = 600


def resolve_python(project: Path) -> Path:
    return resolve_project_python(project)


def should_launch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    return utc.day == 1 and utc.hour == 9 and 0 <= utc.minute <= 15


def _marker_paths(now: datetime, project: Path) -> tuple[Path, Path]:
    utc = now.astimezone(timezone.utc)
    stem = project / "runtime" / f"monthly_dispatch_{utc.strftime('%Y-%m')}"
    return Path(f"{stem}.inprogress"), Path(f"{stem}.completed")


@dataclass
class DispatchClaim:
    lock: FileLock
    completed: Path
    dispatch_time: datetime

    def release(self) -> None:
        self.lock.release()

    def complete(self) -> None:
        temporary = self.completed.with_name(
            f"{self.completed.name}.{uuid.uuid4().hex}.tmp"
        )
        with temporary.open("x", encoding="ascii") as handle:
            handle.write(self.dispatch_time.astimezone(timezone.utc).isoformat())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.completed)
        self.release()


def claim_dispatch(now: datetime, *, project: Path = PROJECT) -> DispatchClaim | None:
    claim_path, completed = _marker_paths(now, project)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    if completed.exists():
        return None
    lock = FileLock(claim_path)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return None
    if completed.exists():
        lock.release()
        return None
    return DispatchClaim(lock=lock, completed=completed, dispatch_time=now)


def main(
    *,
    now: datetime | None = None,
    project: Path = PROJECT,
    run: Callable[..., Any] = subprocess.run,
) -> int:
    dispatch_time = now or datetime.now(timezone.utc)
    if not should_launch(dispatch_time):
        return 0
    claim = claim_dispatch(dispatch_time, project=project)
    if claim is None:
        return 0
    try:
        command = [str(resolve_python(project)), str(project / "run_monthly_report.py")]
        completed = run(
            command,
            cwd=str(project),
            text=True,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        claim.release()
        print("EXECUTION_ERROR: monthly report process exceeded 600 seconds", file=sys.stderr)
        return 124
    except OSError as error:
        claim.release()
        print(f"EXECUTION_ERROR: {error}", file=sys.stderr)
        return 127
    if completed.returncode != 0:
        claim.release()
        print((completed.stderr or completed.stdout).strip(), file=sys.stderr)
        return completed.returncode
    claim.complete()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
