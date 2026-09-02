import hashlib
import json
from pathlib import Path

import pytest

from scripts.generate_hardening_manifest import CRITICAL_FILES, discover_critical_source_files
from src.hardening_manifest import verify_hardening_manifest

ROOT = Path(__file__).resolve().parents[1]


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


def test_economic_entrypoint_dependencies_are_manifest_critical():
    discovered = discover_critical_source_files(ROOT)

    assert "src/portfolio.py" in discovered
    assert "src/strategy.py" in discovered
    assert "src/backtest.py" in discovered
    assert "src/metrics.py" in discovered
    assert "src/experiment_manager.py" in discovered
    assert "src/storage.py" in discovered
    assert "src/database.py" in discovered
    assert "src/report.py" in discovered
    assert "scripts/interpreter.py" in CRITICAL_FILES


def test_active_manifest_rejects_portfolio_source_tampering(tmp_path):
    manifest = ROOT / "forward_experiment" / "hardening_manifest.json"
    sidecar = Path(str(manifest) + ".sha256")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for relative in payload["files"]:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    copied_manifest = tmp_path / "forward_experiment" / "hardening_manifest.json"
    copied_manifest.write_bytes(manifest.read_bytes())
    Path(str(copied_manifest) + ".sha256").write_bytes(sidecar.read_bytes())
    (tmp_path / "src" / "portfolio.py").write_text("tampered = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="src/portfolio.py"):
        verify_hardening_manifest(tmp_path, copied_manifest)
