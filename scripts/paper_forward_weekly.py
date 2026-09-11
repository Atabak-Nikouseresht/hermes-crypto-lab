"""Hermes no-agent UTC gate for the exact weekly paper command."""

import json
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
RETRYABLE_EXIT_CODE = 4
RETRYABLE_EXIT_CODES = {RETRYABLE_EXIT_CODE, 75}


def should_launch(now: datetime) -> bool:
    utc = now.astimezone(timezone.utc)
    return utc.weekday() == 0 and utc.hour == 0 and 5 <= utc.minute <= 20


def rate_limit_defer_marker(*streams: str | bytes | None) -> dict | None:
    """Find a complete defer event in normal or partially captured output."""
    for stream in streams:
        # TimeoutExpired may contain bytes even when run(text=True) was used.
        if isinstance(stream, bytes):
            stream = stream.decode("utf-8", errors="replace")
        if not isinstance(stream, str):
            continue
        for line in stream.splitlines():
            try:
                marker = json.loads(line)
            except (ValueError, TypeError):
                continue
            if (
                isinstance(marker, dict)
                and marker.get("event") == "PUBLIC_MARKET_RATE_LIMIT_DEFER"
                and marker.get("http_status") in (418, 429)
                and marker.get("retry_policy") == "suppress_remaining_weekly_attempts"
            ):
                return marker
    return None


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
            if last_code not in RETRYABLE_EXIT_CODES:
                return last_code
            # A rate-limit defer is stronger than generic D1 retry permission.
            # Inspect both streams even when ordinary diagnostics occupy stderr.
            marker = rate_limit_defer_marker(completed.stdout, completed.stderr)
            if marker is not None:
                # No sleep/re-entry, even without usable Retry-After. The
                # next normal governed scheduled invocation remains allowed.
                print(json.dumps(marker), file=sys.stderr)
                return last_code
        except subprocess.TimeoutExpired as exc:
            last_code = 124
            print(
                "EXECUTION_ERROR: weekly paper process exceeded 600 seconds",
                file=sys.stderr,
            )
            marker = rate_limit_defer_marker(exc.output, exc.stderr)
            if marker is not None:
                print(json.dumps(marker), file=sys.stderr)
                return last_code
        if attempt == MAX_ATTEMPTS:
            break
        sleeper(RETRY_SECONDS)
        current = clock()
        if not should_launch(current):
            break
    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
