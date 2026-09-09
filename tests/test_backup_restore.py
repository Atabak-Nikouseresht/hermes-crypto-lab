from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from src.backup_restore import create_verified_backup, verify_backup, verify_restore_to_temporary
from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem, Quote, RuleReferencePrice, SymbolRules
from src.paper_store import PaperStore, ReconciliationResult
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


def _verified_execution_system(tmp_path, *, v16=False, rejected=False) -> tuple[PaperTradingSystem, Path]:
    project = tmp_path / "project"
    (project / "database").mkdir(parents=True)
    (project / "forward_experiment").mkdir()
    (project / "forward_experiment" / "governance.json").write_text("{}", encoding="utf-8")
    database = project / "database" / "paper_trading.duckdb"
    config = PaperConfig(assets=("BTC/USDT",), initial_cash=100.0, require_exchange_rules=v16)
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
    if v16:
        snapshot = MarketSnapshot(
            closes=snapshot.closes, quotes=snapshot.quotes, fetched_at=timestamp,
            symbol_rules={"BTC/USDT": SymbolRules(
                active=True, min_quantity=0.1, max_quantity=None, step_size=0.1, price_tick=0.01,
                min_notional=60.0 if rejected else 1.0,
                raw_min_quantity=Decimal("0.1"), raw_step_size=Decimal("0.1"),
                raw_min_notional=Decimal("60" if rejected else "1"),
                min_notional_applies_to_market=True,
            )},
            rule_reference_prices={"BTC/USDT": RuleReferencePrice(Decimal("100"), "REFERENCE_PRICE", timestamp)},
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
        ("UPDATE paper_quote_coherence_context SET max_timestamp_skew_seconds=999999", "max skew"),
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
            "max_quote_timestamp_skew_seconds": system.config.max_quote_timestamp_skew_seconds,
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


def test_backup_rejects_coordinated_execution_evidence_stripping(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project,
        database_path=system.store.path,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="2026-08-22T120099Z",
        commit_hash="abc123",
        reconciliation_settings={
            "account_id": system.config.account_id,
            "quantity_tolerance": system.config.quantity_tolerance,
            "fee_rate": system.config.fee_rate,
            "minimum_spread_rate": system.config.minimum_spread_rate,
            "slippage_rate": system.config.slippage_rate,
            "max_quote_timestamp_skew_seconds": system.config.max_quote_timestamp_skew_seconds,
        },
    )
    with system.store.connect() as connection:
        for table in (
            "paper_execution_outcomes",
            "paper_quote_coherence_context",
            "paper_execution_context",
            "paper_orders",
            "paper_fills",
        ):
            connection.execute(f"DELETE FROM {table} WHERE run_id='backup-run'")
        connection.execute("DELETE FROM cash_ledger WHERE run_id='backup-run'")
        connection.execute("DELETE FROM position_ledger WHERE run_id='backup-run'")
        connection.execute("DELETE FROM paper_positions")
        connection.execute("UPDATE paper_accounts SET cash=initial_cash")

    assert not system.store.reconcile().valid
    copied = backup / "paper_trading.duckdb"
    shutil.copy2(system.store.path, copied)
    _refresh_backup_database_checksum(backup)
    with pytest.raises(ValueError, match="runtime reconciliation"):
        verify_backup(backup)


@pytest.mark.parametrize(
    ("statement", "rejected"),
    [
        ("UPDATE paper_market_rule_evidence SET min_notional='60'", False),
        ("DELETE FROM paper_market_rule_evidence", False),
        ("UPDATE paper_market_rule_evidence SET reference_price_source='UNVERIFIABLE_AVERAGE'", False),
        ("UPDATE paper_order_rejections SET requested_quantity=1.0, notional=100.0", True),
    ],
    ids=["BACKUP-1-notional", "BACKUP-2-deletion", "BACKUP-3-reference", "BACKUP-4-rejection"],
)
def test_v16_backup_uses_runtime_semantics_and_is_read_only(tmp_path, statement, rejected):
    import duckdb

    system, project = _verified_execution_system(tmp_path, v16=True, rejected=rejected)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT market_rule_evidence_required FROM paper_runs").fetchone() == (True,)
        assert connection.execute("SELECT COUNT(*) FROM paper_market_rule_evidence").fetchone() == (1,)
        if rejected:
            assert connection.execute("SELECT reason FROM paper_order_rejections").fetchone() == ("below_min_notional",)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="v16", commit_hash="a" * 40,
        reconciliation_settings={
            "account_id": system.config.account_id,
            "quantity_tolerance": system.config.quantity_tolerance,
            "fee_rate": system.config.fee_rate,
            "minimum_spread_rate": system.config.minimum_spread_rate,
            "slippage_rate": system.config.slippage_rate,
            "max_quote_timestamp_skew_seconds": system.config.max_quote_timestamp_skew_seconds,
        },
    )
    database = backup / "paper_trading.duckdb"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert verify_backup(backup)["valid"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with duckdb.connect(str(database)) as connection:
        connection.execute("SET TimeZone='UTC'")
        connection.execute(statement)
        # Defeat the row digest deliberately: this regression must exercise
        # decision reconstruction, not just the independent integrity seal.
        if "UPDATE paper_market_rule_evidence" in statement:
            rows = connection.execute("SELECT * FROM paper_market_rule_evidence ORDER BY symbol").fetchall()
            diagnostics = json.loads(connection.execute("SELECT diagnostics FROM paper_forward_execution_evidence WHERE run_id='backup-run'").fetchone()[0])
            diagnostics["market_rule_evidence_sha256"] = PaperStore.market_rule_evidence_digest(rows)
            connection.execute("UPDATE paper_forward_execution_evidence SET diagnostics=? WHERE run_id='backup-run'", [json.dumps(diagnostics)])
    _refresh_backup_database_checksum(backup)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="runtime reconciliation"):
        verify_backup(backup)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
