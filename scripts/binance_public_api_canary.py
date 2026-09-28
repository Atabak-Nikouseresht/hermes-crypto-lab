"""Hermes no-agent wrapper for the read-only public Binance canary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SUBPROCESS_TIMEOUT_SECONDS = 45


def main() -> int:
    """Run only the project's existing Canary module and propagate its status."""
    project = Path.cwd().resolve()
    module_file = project / "scripts" / "check_binance_public_api.py"
    if not (project / "pyproject.toml").is_file() or not module_file.is_file():
        print("CANARY_EXECUTION_ERROR: configured project context is missing", file=sys.stderr)
        return 127

    scripts_directory = project / "scripts"
    sys.path.insert(0, str(scripts_directory))
    try:
        from interpreter import resolve_project_python

        python = resolve_project_python(project)
    except (FileNotFoundError, ImportError, OSError) as error:
        print(f"CANARY_EXECUTION_ERROR: project interpreter unavailable: {error}", file=sys.stderr)
        return 127

    try:
        completed = subprocess.run(
            [str(python), "-m", "scripts.check_binance_public_api"],
            cwd=project,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            "CANARY_EXECUTION_ERROR: Canary subprocess exceeded 45 seconds",
            file=sys.stderr,
        )
        return 124
    except OSError as error:
        print(f"CANARY_EXECUTION_ERROR: could not start Canary subprocess: {error}", file=sys.stderr)
        return 127

    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
