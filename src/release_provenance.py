"""Local, verified maintenance-release provenance for official paper runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import subprocess
from pathlib import Path

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.hardening_manifest import verify_hardening_manifest


_GIT_SHA256 = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ReleaseProvenance:
    git_commit: str
    git_dirty: bool
    hardening_manifest_sha256: str
    execution_protocol_version: str
    captured_at_utc: datetime


def capture_release_provenance(
    project_root: Path, *, now: datetime | None = None
) -> ReleaseProvenance:
    """Fail closed unless local Git and the active hardening manifest verify."""
    root = Path(project_root).resolve()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Release provenance requires local Git metadata") from error
    if _GIT_SHA256.fullmatch(commit) is None:
        raise RuntimeError("Release provenance Git commit is not an exact SHA-1")
    if status:
        raise RuntimeError("Release provenance refuses a dirty Git working tree")
    try:
        hardening = verify_hardening_manifest(
            root, root / "forward_experiment" / "hardening_manifest.json"
        )
    except (OSError, ValueError) as error:
        raise RuntimeError("Release provenance hardening manifest verification failed") from error
    manifest_sha256 = hardening.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise RuntimeError("Release provenance hardening manifest hash is invalid")
    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise ValueError("Release provenance timestamp must be timezone-aware")
    return ReleaseProvenance(
        git_commit=commit,
        git_dirty=False,
        hardening_manifest_sha256=manifest_sha256,
        execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
        captured_at_utc=captured_at.astimezone(timezone.utc),
    )
