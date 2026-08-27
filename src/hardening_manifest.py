"""Verification for the tamper-evident hardening hash chain."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_hardening_manifest(project_root: Path, manifest_path: Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    manifest_path = Path(manifest_path).resolve()
    sidecar_path = Path(str(manifest_path) + ".sha256")
    if not manifest_path.is_file() or not sidecar_path.is_file():
        raise FileNotFoundError("hardening manifest or sidecar is missing")
    expected_manifest_hash = sidecar_path.read_text(encoding="ascii").split()[0]
    if _sha256(manifest_path) != expected_manifest_hash:
        raise ValueError("hardening manifest sidecar hash mismatch")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("hardening manifest file chain is empty")
    for relative, expected in files.items():
        candidate = (project_root / relative).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as error:
            raise ValueError(f"manifest path escapes project root: {relative}") from error
        if not candidate.is_file() or _sha256(candidate) != expected:
            raise ValueError(f"hardening file hash mismatch: {relative}")
    return {"valid": True, "files_verified": len(files), "manifest_sha256": expected_manifest_hash}
