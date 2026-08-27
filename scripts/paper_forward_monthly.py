"""Hermes no-agent UTC gate for the forward-only monthly report."""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
SCRIPT = PROJECT / "run_monthly_report.py"
COMMAND = [str(PYTHON), str(SCRIPT)]


def should_launch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    return utc.day == 1 and utc.hour == 9 and utc.minute == 0


def claim_dispatch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    marker = PROJECT / "runtime" / f"monthly_dispatch_{utc.strftime('%Y-%m')}.claim"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="ascii") as handle:
            handle.write(utc.isoformat())
        return True
    except FileExistsError:
        return False


def main() -> int:
    now = datetime.now(timezone.utc)
    if not should_launch(now) or not claim_dispatch(now):
        return 0
    try:
        completed = subprocess.run(
            COMMAND,
            cwd=str(PROJECT),
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("EXECUTION_ERROR: monthly report process exceeded 600 seconds", file=sys.stderr)
        return 124
    if completed.returncode != 0:
        print((completed.stderr or completed.stdout).strip(), file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
