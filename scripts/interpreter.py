"""Resolve the project-local Python interpreter for scheduler wrappers."""

from pathlib import Path


def resolve_project_python(project: Path) -> Path:
    """Prefer the deployed Windows venv, then the standard Unix venv."""
    for candidate in (
        project / ".venv" / "Scripts" / "python.exe",
        project / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("project virtual-environment interpreter not found")
