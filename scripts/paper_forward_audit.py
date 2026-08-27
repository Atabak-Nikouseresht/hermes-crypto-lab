"""Hermes no-agent wrapper for the 00:21 UTC missed-window audit."""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
SCRIPT = PROJECT / "run_paper.py"
COMMAND = [str(PYTHON), str(SCRIPT), "--audit-missed"]


def should_launch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    return utc.weekday() == 0 and utc.hour == 0 and utc.minute == 21


def main() -> int:
    if not should_launch(datetime.now(timezone.utc)):
        return 0
    try:
        completed = subprocess.run(
            COMMAND,
            cwd=str(PROJECT),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("EXECUTION_ERROR: missed-window audit exceeded 120 seconds", file=sys.stderr)
        return 124
    if completed.returncode != 0:
        print((completed.stderr or completed.stdout).strip(), file=sys.stderr)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
