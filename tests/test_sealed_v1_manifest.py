import hashlib
import json
from pathlib import Path

import pytest

from scripts.sealed_v1_manifest import generate_manifest, verify_manifest


def _write_run(root: Path, name: str, files: dict[str, bytes]) -> None:
    run = root / "experiments" / "runs" / name
    run.mkdir(parents=True)
    for filename, content in files.items():
        (run / filename).write_bytes(content)


def test_manifest_generation_is_path_sorted_and_verifies(tmp_path):
    _write_run(tmp_path, "run-b", {"z.bin": b"z", "a.json": b"a"})
    _write_run(tmp_path, "run-a", {"record.jsonl": b"ledger"})

    manifest = generate_manifest(tmp_path)

    paths = [item["path"] for item in manifest["files"]]
    assert paths == sorted(paths)
    assert verify_manifest(tmp_path, manifest, hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest())


@pytest.mark.parametrize("change", ["modify", "delete", "add"])
def test_frozen_manifest_detects_run_artifact_drift(tmp_path, change):
    _write_run(tmp_path, "run-a", {"artifact.bin": b"original"})
    manifest = generate_manifest(tmp_path)
    anchor = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = tmp_path / "experiments" / "runs" / "run-a" / "artifact.bin"
    if change == "modify":
        artifact.write_bytes(b"changed")
    elif change == "delete":
        artifact.unlink()
    else:
        (artifact.parent / "unexpected.bin").write_bytes(b"new")

    with pytest.raises(ValueError):
        verify_manifest(tmp_path, manifest, anchor)


def test_manifest_anchor_rejects_manifest_changes(tmp_path):
    _write_run(tmp_path, "run-a", {"artifact.bin": b"original"})
    manifest = generate_manifest(tmp_path)
    manifest["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="anchor"):
        verify_manifest(tmp_path, manifest, "1" * 64)
