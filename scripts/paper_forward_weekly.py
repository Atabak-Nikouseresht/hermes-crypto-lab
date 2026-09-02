"""Hermes no-agent UTC gate for the exact weekly paper command."""

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from scripts.interpreter import resolve_project_python
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from interpreter import resolve_project_python

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "run_paper.py"
MAX_ATTEMPTS = 3
RETRY_SECONDS = 60


def should_launch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    return utc.weekday() == 0 and utc.hour == 0 and 5 <= utc.minute <= 20


def main(
    now: datetime | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] = time.sleep,
    python_resolver: Callable[[Path], Path] = resolve_project_python,
) -> int:
    current = now or clock()
    if not should_launch(current):
        return 0
    command = [str(python_resolver(PROJECT)), str(SCRIPT), "--paper"]
    last_code = 1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            completed = subprocess.run(
                command,
                cwd=str(PROJECT),
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
            )
            last_code = completed.returncode
            if last_code == 0:
                return 0
            print((completed.stderr or completed.stdout).strip(), file=sys.stderr)
        except subprocess.TimeoutExpired:
            last_code = 124
            print(
                "EXECUTION_ERROR: weekly paper process exceeded 600 seconds",
                file=sys.stderr,
            )
        if attempt == MAX_ATTEMPTS:
            break
        sleeper(RETRY_SECONDS)
        current = clock()
        if not should_launch(current):
            break
    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
