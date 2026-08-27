"""Hermes no-agent UTC gate for the exact weekly paper command."""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(r"C:\Users\ataba\hermes-crypto-lab")
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
SCRIPT = PROJECT / "run_paper.py"
COMMAND = [str(PYTHON), str(SCRIPT), "--paper"]


def should_launch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    return utc.weekday() == 0 and utc.hour == 0 and 10 <= utc.minute <= 20


def claim_dispatch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    marker = PROJECT / "runtime" / f"weekly_dispatch_{utc.strftime('%Y-%m-%d')}.claim"
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
        print("EXECUTION_ERROR: weekly paper process exceeded 600 seconds", file=sys.stderr)
        return 124
    if completed.returncode != 0:
        print((completed.stderr or completed.stdout).strip(), file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
