from pathlib import Path

import pytest

from src.backup_restore import create_verified_backup, verify_backup, verify_restore_to_temporary
from src.paper_broker import PaperConfig, PaperTradingSystem


def test_backup_and_temporary_restore_verify_without_overwriting_production(tmp_path):
    project = tmp_path / "project"
    (project / "database").mkdir(parents=True)
    (project / "forward_experiment").mkdir()
    (project / "reports" / "paper").mkdir(parents=True)
    (project / "forward_experiment" / "governance.json").write_text("{}", encoding="utf-8")
    database = project / "database" / "paper_trading.duckdb"
    system = PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))

    backup = create_verified_backup(
        project_root=project,
        database_path=database,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="2026-08-22T120000Z",
        commit_hash="abc123",
    )
    result = verify_backup(backup)
    restored = verify_restore_to_temporary(backup, tmp_path / "restore-check")

    assert result["valid"] is True
    assert restored["valid"] is True
    assert database.exists()
    assert restored["production_database_untouched"] is True


def test_corrupted_backup_is_detected(tmp_path):
    project = tmp_path / "project"
    (project / "database").mkdir(parents=True)
    (project / "forward_experiment").mkdir()
    database = project / "database" / "paper_trading.duckdb"
    PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))
    backup = create_verified_backup(
        project_root=project,
        database_path=database,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="2026-08-22T120001Z",
        commit_hash="abc123",
    )
    copied = backup / "paper_trading.duckdb"
    copied.write_bytes(copied.read_bytes() + b"corruption")

    with pytest.raises(ValueError, match="checksum"):
        verify_backup(backup)
