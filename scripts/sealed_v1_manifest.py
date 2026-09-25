"""Generate and verify the frozen inventory of sealed V1 research runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path("experiments/sealed_v1_manifest.json")
RUNS_PATH = Path("experiments/runs")


def _canonical(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_manifest(root: Path) -> dict[str, Any]:
    """Inventory every regular file in every governed run directory."""
    root = Path(root)
    runs_root = root / RUNS_PATH
    if not runs_root.is_dir():
        raise ValueError(f"Sealed V1 runs directory is missing: {runs_root}")
    files = []
    for path in sorted(runs_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not valid sealed run artifacts: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            files.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not files:
        raise ValueError(f"No sealed V1 run artifacts found: {runs_root}")
    return {"files": files, "version": 1}


def verify_manifest(root: Path, manifest: dict[str, Any], expected_sha256: str) -> bool:
    """Verify the external manifest anchor and exact governed file inventory."""
    actual_anchor = hashlib.sha256(_canonical(manifest)).hexdigest()
    if actual_anchor != expected_sha256:
        raise ValueError("Sealed V1 manifest anchor mismatch")
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unsupported or malformed sealed V1 manifest")
    paths = [entry.get("path") for entry in manifest["files"]]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("Sealed V1 manifest paths must be unique and sorted")
    expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    actual = generate_manifest(root)
    actual_files = {entry["path"]: entry["sha256"] for entry in actual["files"]}
    if actual_files != expected:
        missing = sorted(expected.keys() - actual_files.keys())
        added = sorted(actual_files.keys() - expected.keys())
        modified = sorted(path for path in expected.keys() & actual_files.keys() if expected[path] != actual_files[path])
        raise ValueError(f"Sealed V1 artifacts drifted (missing={missing}, added={added}, modified={modified})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="explicitly generate/update the frozen manifest")
    parser.add_argument("--expected-sha256", help="external trust anchor required for verification")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / MANIFEST_PATH
    if args.generate:
        manifest = generate_manifest(root)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Generated {manifest_path.relative_to(root)} ({len(manifest['files'])} files)")
        print(f"SHA-256: {hashlib.sha256(_canonical(manifest)).hexdigest()}")
        return 0
    if not args.expected_sha256:
        parser.error("--expected-sha256 is required unless --generate is used")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_manifest(root, manifest, args.expected_sha256)
    print(f"PASS: verified {len(manifest['files'])} sealed V1 artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
