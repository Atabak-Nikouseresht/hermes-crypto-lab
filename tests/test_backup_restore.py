from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from src.backup_restore import create_verified_backup, verify_backup, verify_restore_to_temporary
from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem, Quote
from src.paper_store import ReconciliationResult
from src.release_provenance import ReleaseProvenance


def test_backup_and_temporary_restore_verify_without_overwriting_production(tmp_path):
    project = tmp_path / "project"
    (project / "database").mkdir(parents=True)
    (project / "forward_experiment").mkdir()
    (project / "reports" / "paper").mkdir(parents=True)
    (project / "forward_experiment" / "governance.json").write_text("{}", encoding="utf-8")
    database = project / "database" / "paper_trading.duckdb"
    PaperTradingSystem(database, PaperConfig(assets=("BTC/USDT",)))

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


def _verified_execution_system(tmp_path) -> tuple[PaperTradingSystem, Path]:
    project = tmp_path / "project"
    (project / "database").mkdir(parents=True)
    (project / "forward_experiment").mkdir()
    (project / "forward_experiment" / "governance.json").write_text("{}", encoding="utf-8")
    database = project / "database" / "paper_trading.duckdb"
    config = PaperConfig(assets=("BTC/USDT",), initial_cash=100.0)
    system = PaperTradingSystem(database, config)
    now = datetime.now(timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES ('forward', ?, 'locked', 'strategy', 'governance', '{}', 'ACTIVE')",
            [now],
        )
    provenance = ReleaseProvenance(
        git_commit="a" * 40,
        git_dirty=False,
        hardening_manifest_sha256="b" * 64,
        execution_protocol_version="paper-exec-v3-ask-bid-minspread-utc0010",
        captured_at_utc=now,
    )
    system.store.insert_run(
        run_id="backup-run",
        started_at=now,
        mode="PAPER",
        schedule_key="backup-schedule",
        signal_timestamp=datetime(2024, 8, 4, tzinfo=timezone.utc),
        data_timestamp=datetime(2024, 8, 4, tzinfo=timezone.utc),
        official_scheduled=True,
        release_provenance=provenance,
    )
    timestamp = pd.Timestamp("2024-08-05T09:10:00Z")
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(
            {"BTC/USDT": [100.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-08-04T00:00:00Z")]),
        ),
        quotes={"BTC/USDT": Quote(100.0, 100.0, 100.0, timestamp)},
        fetched_at=timestamp,
    )
    system._execute(
        run_id="backup-run",
        signal_timestamp=pd.Timestamp("2024-08-04T00:00:00Z"),
        proposals=[
            {
                "idempotency_key": "backup-order",
                "symbol": "BTC/USDT",
                "side": "BUY",
                "requested_quantity": 0.5,
                "target_weight": 0.5,
            }
        ],
        snapshot=snapshot,
        now=timestamp,
    )
    system.store.finish_run(
        run_id="backup-run",
        status="EXECUTED",
        completed_at=now,
        message="backup parity fixture",
        reconciliation=ReconciliationResult(True, "fixture"),
    )
    assert system.store.reconcile().valid
    return system, project


def _refresh_backup_database_checksum(backup: Path) -> None:
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database = backup / "paper_trading.duckdb"
    manifest["checksums"]["paper_trading.duckdb"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (backup / "backup_manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  backup_manifest.json\n",
        encoding="ascii",
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("UPDATE paper_accounts SET cash=cash+1.0", "cash mismatch"),
        ("UPDATE paper_run_release_provenance SET git_commit='g'", "release provenance"),
        ("UPDATE paper_execution_context SET bid=ask+1.0", "bid/ask"),
        ("UPDATE paper_quote_coherence_context SET contract_version='unknown-contract'", "quote coherence contract"),
    ],
)
def test_backup_semantic_validity_cannot_exceed_runtime_reconciliation(
    tmp_path, statement, expected
):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project,
        database_path=system.store.path,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp=f"2026-08-22T12000{len(statement)}Z",
        commit_hash="abc123",
        reconciliation_settings={
            "account_id": system.config.account_id,
            "quantity_tolerance": system.config.quantity_tolerance,
            "fee_rate": system.config.fee_rate,
            "minimum_spread_rate": system.config.minimum_spread_rate,
            "slippage_rate": system.config.slippage_rate,
        },
    )
    copied = backup / "paper_trading.duckdb"
    before_verify = hashlib.sha256(copied.read_bytes()).hexdigest()
    assert verify_backup(backup)["valid"] is True
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == before_verify

    with system.store.connect() as connection:
        connection.execute(statement)
    runtime = system.store.reconcile()
    assert not runtime.valid
    assert expected in runtime.message.lower()

    shutil.copy2(system.store.path, copied)
    _refresh_backup_database_checksum(backup)
    with pytest.raises(ValueError, match="runtime reconciliation"):
        verify_backup(backup)
