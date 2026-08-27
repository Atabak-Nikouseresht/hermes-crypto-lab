import hashlib
import json

import pytest

from src.hardening_manifest import verify_hardening_manifest


def test_hardening_manifest_verifies_sidecar_and_every_file(tmp_path):
    source = tmp_path / "critical.py"
    source.write_text("safe = True\n", encoding="utf-8")
    payload = {
        "locked_strategy_sha256": "expected",
        "files": {"critical.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    manifest = tmp_path / "hardening_manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    sidecar = tmp_path / "hardening_manifest.json.sha256"
    sidecar.write_text(
        hashlib.sha256(manifest.read_bytes()).hexdigest() + " hardening_manifest.json\n",
        encoding="utf-8",
    )

    result = verify_hardening_manifest(tmp_path, manifest)
    assert result["valid"] is True
    assert result["files_verified"] == 1

    source.write_text("safe = False\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_hardening_manifest(tmp_path, manifest)
