import hashlib
import json
from pathlib import Path

import pytest

from scripts import generate_hardening_manifest as manifest_generator
from scripts.generate_hardening_manifest import (
    CRITICAL_FILES,
    discover_critical_source_files,
    verify_critical_source_coverage,
)
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


def test_operational_roots_have_deterministic_transitive_local_dependency_closure(tmp_path):
    (tmp_path / "run_paper.py").write_text(
        "from scripts import operational\n", encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "operational.py").write_text(
        "from src import local_dependency\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "local_dependency.py").write_text(
        "VALUE = True\n", encoding="utf-8"
    )

    assert discover_critical_source_files(tmp_path) == (
        "run_paper.py",
        "scripts/operational.py",
        "src/local_dependency.py",
    )


def test_coverage_verification_rejects_omitted_reachable_dependency(tmp_path):
    (tmp_path / "run_paper.py").write_text(
        "from src import reachable\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "reachable.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="src/reachable.py"):
        verify_critical_source_coverage(tmp_path, critical_files=("run_paper.py",))


def test_checked_manifest_file_set_covers_all_reachable_local_dependencies():
    verify_critical_source_coverage(ROOT, CRITICAL_FILES)
    manifest_generator.verify_manifest_critical_source_coverage(
        ROOT, ROOT / "forward_experiment" / "hardening_manifest.json"
    )


def test_manifest_coverage_verifier_checks_checked_in_transitive_inventory(tmp_path):
    (tmp_path / "run_paper.py").write_text("from src import first\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "first.py").write_text(
        "from src import transitive\n", encoding="utf-8"
    )
    (tmp_path / "src" / "transitive.py").write_text("VALUE = True\n", encoding="utf-8")
    manifest = tmp_path / "hardening_manifest.json"
    manifest.write_text(
        json.dumps({"files": {"run_paper.py": "", "src/first.py": ""}}),
        encoding="utf-8",
    )

    verifier = getattr(manifest_generator, "verify_manifest_critical_source_coverage", None)
    assert callable(verifier), "CI needs a verifier against checked-in manifest inventory"
    with pytest.raises(ValueError, match="src/transitive.py"):
        verifier(tmp_path, manifest)


def test_new_root_run_entrypoint_is_itself_manifest_critical(tmp_path):
    entrypoint = tmp_path / "run_new_operation.py"
    entrypoint.write_text("print('governed')\n", encoding="utf-8")

    assert "run_new_operation.py" in discover_critical_source_files(tmp_path)


def test_operational_entrypoints_are_part_of_manifest_critical_closure():
    discovered = discover_critical_source_files(ROOT)

    assert "src/backup_restore.py" in discovered
    assert "src/paper_report.py" in discovered
    assert "src/paper_notifications.py" in discovered
    assert "scripts/sealed_v1_manifest.py" in CRITICAL_FILES


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


def test_active_manifest_verification_rejects_uncovered_new_entrypoint(tmp_path):
    source = tmp_path / "run_paper.py"
    source.write_text("print('safe')\n", encoding="utf-8")
    payload = {
        "purpose": "active publication-and-operations critical-file manifest",
        "files": {"run_paper.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    manifest = tmp_path / "hardening_manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    Path(str(manifest) + ".sha256").write_text(
        hashlib.sha256(manifest.read_bytes()).hexdigest() + " hardening_manifest.json\n",
        encoding="ascii",
    )

    assert verify_hardening_manifest(tmp_path, manifest)["valid"]

    new_entrypoint = tmp_path / "run_new.py"
    new_entrypoint.write_text("print('governed')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="run_new.py"):
        manifest_generator.verify_manifest_critical_source_coverage(tmp_path, manifest)
