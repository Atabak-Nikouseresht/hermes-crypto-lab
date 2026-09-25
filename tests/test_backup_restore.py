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
from src.release_provenance import ReleaseProvenance, ReleaseProvenanceError


@pytest.fixture(autouse=True)
def _controlled_current_release_provenance(monkeypatch):
    def provenance(root):
        manifest = Path(root) / "forward_experiment" / "hardening_manifest.json"
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        return ReleaseProvenance(
            git_commit="c" * 40,
            git_dirty=False,
            hardening_manifest_sha256=digest,
            execution_protocol_version="paper-exec-v3-ask-bid-minspread-utc0010",
            captured_at_utc=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "src.backup_restore.capture_release_provenance",
        provenance,
    )


def test_backup_and_temporary_restore_verify_without_overwriting_production(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    database = system.store.path

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
    system, project = _verified_execution_system(tmp_path)
    database = system.store.path
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
    (project / "forward_experiment" / "hardening_manifest.json").write_text("{}", encoding="utf-8")
    database = project / "database" / "paper_trading.duckdb"
    config = PaperConfig(assets=("BTC/USDT",), initial_cash=100.0, require_exchange_rules=v16)
    from run_paper import load_paper_configuration
    from src.forward_governance import economic_spec_hash_v2, economic_spec_v2

    governed_config, _ = load_paper_configuration(Path(__file__).resolve().parents[1])
    persisted_economic_spec = economic_spec_v2(governed_config)
    assert persisted_economic_spec["execution"]["quantity_tolerance"] == config.quantity_tolerance
    system = PaperTradingSystem(database, config)
    now = datetime.now(timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES ('forward', ?, 'locked', ?, ?, "
            "?, 'ACTIVE')",
            [now, "e" * 64, "f" * 64, json.dumps({
                "locked_strategy": {"candidate_id": "locked"},
                "locked_strategy_hash_sha256": "e" * 64,
                "economic_spec_v2": persisted_economic_spec,
                "economic_spec_v2_sha256": economic_spec_hash_v2(governed_config),
                "quantity_tolerance_authority_version": "quantity-tolerance-v1",
                "cost_assumptions": {
                    "fee_rate": 0.001,
                    "minimum_spread_rate": 0.0002,
                    "slippage_rate": 0.0005,
                },
            })],
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
            rule_reference_prices={"BTC/USDT": RuleReferencePrice(Decimal("100"), "REFERENCE_PRICE", timestamp, timestamp)},
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
    for relative in manifest["checksums"]:
        source = backup / relative
        manifest["checksums"][relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (backup / "backup_manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  backup_manifest.json\n",
        encoding="ascii",
    )


@pytest.mark.parametrize(
    "authority_damage",
    [
        "quantity_tolerance_authority_version",
        "economic_spec_v2_sha256",
        "null_economic_spec_without_v2_markers",
        "remove_economic_spec_and_version_marker",
        "remove_all_v2_authority_fields",
    ],
)
def test_v2_backup_creation_and_verification_require_tolerance_authority_fields(
    tmp_path, authority_damage
):
    import duckdb

    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project,
        database_path=system.store.path,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="valid-authority",
        commit_hash="a" * 40,
    )

    def remove_authority_field(database_path: Path) -> None:
        with duckdb.connect(str(database_path)) as connection:
            raw = connection.execute(
                "SELECT specification FROM forward_experiments WHERE experiment_id='forward'"
            ).fetchone()[0]
            specification = json.loads(raw)
            if authority_damage == "null_economic_spec_without_v2_markers":
                specification["economic_spec_v2"] = None
                specification.pop("quantity_tolerance_authority_version")
                specification.pop("economic_spec_v2_sha256")
            elif authority_damage == "remove_economic_spec_and_version_marker":
                specification.pop("economic_spec_v2")
                specification.pop("quantity_tolerance_authority_version")
            elif authority_damage == "remove_all_v2_authority_fields":
                for key in (
                    "economic_spec_v2",
                    "economic_spec_v2_sha256",
                    "quantity_tolerance_authority_version",
                ):
                    specification.pop(key)
            else:
                specification.pop(authority_damage)
            connection.execute(
                "UPDATE forward_experiments SET specification=? WHERE experiment_id='forward'",
                [json.dumps(specification)],
            )

    remove_authority_field(system.store.path)
    with pytest.raises(ValueError, match="(?i)persisted quantity tolerance"):
        create_verified_backup(
            project_root=project,
            database_path=system.store.path,
            output_root=tmp_path / "backups",
            lock_path=project / "runtime" / "forward_writer.lock",
            timestamp="missing-authority",
            commit_hash="a" * 40,
        )

    remove_authority_field(backup / "paper_trading.duckdb")
    _refresh_backup_database_checksum(backup)
    with pytest.raises(ValueError, match="runtime reconciliation.*quantity tolerance"):
        verify_backup(backup)


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
        ("UPDATE paper_market_rule_evidence SET reference_price_acquired_at_utc=NULL", False),
        ("UPDATE paper_market_rule_evidence SET reference_price_timestamp_utc=captured_at_utc-INTERVAL 301 SECOND, reference_price_acquired_at_utc=captured_at_utc-INTERVAL 300 SECOND", False),
        ("UPDATE paper_market_rule_evidence SET contract_version='binance-market-rule-evidence-v1', reference_price_acquired_at_utc=NULL", False),
        ("UPDATE paper_market_rule_evidence SET captured_at_utc=captured_at_utc-INTERVAL 1 SECOND", False),
        ("UPDATE paper_market_rule_evidence SET reference_price_acquired_at_utc=captured_at_utc+INTERVAL 1 SECOND", True),
        ("UPDATE paper_market_rule_evidence SET reference_price_timestamp_utc=reference_price_acquired_at_utc-INTERVAL 301 SECOND", False),
        ("UPDATE paper_market_rule_evidence SET reference_price_timestamp_utc=reference_price_acquired_at_utc+INTERVAL 1 SECOND", False),
        ("UPDATE paper_market_rule_evidence SET reference_price_source='LAST_FALLBACK'", False),
        ("UPDATE paper_market_rule_evidence SET contract_version='binance-market-rule-evidence-v1'", False),
        ("UPDATE paper_market_rule_evidence SET reference_price_timestamp_utc=NULL", False),
    ],
    ids=["notional", "deletion", "reference", "rejection", "BACKUP-3-null-acquisition", "final-stale", "coordinated-downgrade", "admission", "zero-order-future", "BACKUP-1-stale-reference", "BACKUP-2-future-reference", "BACKUP-5-source-coherence", "BACKUP-4-contract-downgrade", "missing-reference-timestamp"],
)
def test_v16_backup_uses_runtime_semantics_and_is_read_only(tmp_path, statement, rejected):
    import duckdb

    system, project = _verified_execution_system(tmp_path, v16=True, rejected=rejected)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT market_rule_evidence_required FROM paper_runs").fetchone() == (True,)
        assert connection.execute("SELECT COUNT(*) FROM paper_market_rule_evidence").fetchone() == (1,)
        assert connection.execute("SELECT contract_version FROM paper_market_rule_evidence").fetchone() == ('binance-market-rule-evidence-v2',)
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


def test_current_backup_manifest_has_strict_versioned_provenance(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    diagnostic = PaperStore.open_diagnostic_read_only(system.store.path)
    assert diagnostic.quantity_tolerance == 1e-12
    backup = create_verified_backup(
        project_root=project,
        database_path=system.store.path,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="manifest-v2",
        commit_hash="a" * 40,
    )
    manifest = json.loads((backup / "backup_manifest.json").read_text(encoding="utf-8"))
    assert manifest["backup_manifest_version"] == 2
    assert manifest["git_dirty"] is False
    assert manifest["commit_hash"] == "c" * 40
    assert manifest["hardening_manifest_sha256"] == hashlib.sha256(
        (project / "forward_experiment" / "hardening_manifest.json").read_bytes()
    ).hexdigest()
    assert manifest["backup_timestamp"].endswith("Z")
    assert manifest["locked_candidate_id"] == "locked"
    assert manifest["experiment_id"] == "forward"
    assert manifest["reconciliation_settings"]["quantity_tolerance"] == 1e-12
    assert manifest["execution_protocol_version"] == "paper-exec-v3-ask-bid-minspread-utc0010"


def test_new_backup_from_legacy_experiment_without_tolerance_fails_closed(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    with system.store.connect() as connection:
        raw = connection.execute(
            "SELECT specification FROM forward_experiments WHERE experiment_id='forward'"
        ).fetchone()[0]
        specification = json.loads(raw)
        specification.pop("economic_spec_v2")
        specification.pop("economic_spec_v2_sha256")
        specification.pop("quantity_tolerance_authority_version")
        connection.execute(
            "UPDATE forward_experiments SET specification=? WHERE experiment_id='forward'",
            [json.dumps(specification)],
        )

    with pytest.raises(ValueError, match="(?i)persisted quantity tolerance authority"):
        create_verified_backup(
            project_root=project, database_path=system.store.path,
            output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
            timestamp="legacy-authority-new-backup", commit_hash="a" * 40,
        )


def test_v2_verification_checks_archived_hardening_hash_not_current_tree(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="hardening-evidence", commit_hash="a" * 40,
    )
    archived = backup / "forward_experiment" / "hardening_manifest.json"
    archived.write_text("{\"changed\":true}", encoding="utf-8")
    _refresh_backup_database_checksum(backup)

    with pytest.raises(ValueError, match="hardening manifest SHA-256"):
        verify_backup(backup)


def test_current_v2_manifest_rejects_missing_field_and_malformed_schema(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="malformed-v2", commit_hash="a" * 40,
    )
    path = backup / "backup_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["git_dirty"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed backup manifest v2 fields"):
        verify_backup(backup)
    manifest["git_dirty"] = False
    manifest["schema_version"] = True
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="schema/checksums"):
        verify_backup(backup)


def test_current_v2_manifest_rejects_non_utc_creation_timestamp(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="invalid-time", commit_hash="a" * 40,
    )
    path = backup / "backup_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["backup_timestamp"] = "2026-08-22T12:00:00"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="creation timestamp must be UTC"):
        verify_backup(backup)


def test_v2_verification_rejects_persisted_execution_protocol_mismatch(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="protocol-mismatch", commit_hash="a" * 40,
    )
    path = backup / "backup_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["execution_protocol_version"] = "paper-exec-untrusted"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    _refresh_backup_database_checksum(backup)

    with pytest.raises(ValueError, match="manifest conflicts with persisted authority"):
        verify_backup(backup)


def test_current_governed_backup_fails_closed_when_tree_is_dirty(tmp_path, monkeypatch):
    system, project = _verified_execution_system(tmp_path)
    def dirty(_root):
        raise ReleaseProvenanceError("Release provenance refuses a dirty Git working tree", retryable=False)
    monkeypatch.setattr("src.backup_restore.capture_release_provenance", dirty)
    with pytest.raises(ValueError, match="dirty Git working tree"):
        create_verified_backup(
            project_root=project, database_path=system.store.path,
            output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
            timestamp="dirty-tree", commit_hash="a" * 40,
        )
    assert not (tmp_path / "backups").exists()


def test_runtime_reconciliation_uses_persisted_quantity_tolerance(tmp_path):
    system, _project = _verified_execution_system(tmp_path)
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_positions SET quantity=quantity+5e-8")
    result = system.store.reconcile()
    assert not result.valid
    assert "position" in result.message.lower()


def test_reconciliation_rejects_zero_persisted_quantity_tolerance(tmp_path, monkeypatch):
    system, _project = _verified_execution_system(tmp_path)
    with system.store.connect() as connection:
        specification = json.loads(
            connection.execute(
                "SELECT specification FROM forward_experiments WHERE status='ACTIVE'"
            ).fetchone()[0]
        )
        specification["economic_spec_v2"]["execution"]["quantity_tolerance"] = 0
        digest = hashlib.sha256(
            json.dumps(
                specification["economic_spec_v2"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        specification["economic_spec_v2_sha256"] = digest
        monkeypatch.setattr(
            "src.forward_governance.ECONOMIC_SPEC_HASH_V2_SHA256", digest
        )
        connection.execute(
            "UPDATE forward_experiments SET specification=? WHERE status='ACTIVE'",
            [json.dumps(specification)],
        )

    result = system.store.reconcile()
    assert not result.valid
    assert "quantity tolerance is invalid" in result.message.lower()


@pytest.mark.parametrize("verification", ["backup", "restore"])
def test_zero_manifest_tolerance_fails_backup_and_restore_verification(tmp_path, verification):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project,
        database_path=system.store.path,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp=f"zero-tolerance-{verification}",
        commit_hash="a" * 40,
    )
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reconciliation_settings"]["quantity_tolerance"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="out of bounds"):
        if verification == "backup":
            verify_backup(backup)
        else:
            verify_restore_to_temporary(backup, tmp_path / "restore")


def test_reconcile_database_rejects_quantity_tolerance_override(tmp_path):
    system, _project = _verified_execution_system(tmp_path)

    result = PaperStore.reconcile_database(
        system.store.path,
        account_id=system.config.account_id,
        quantity_tolerance=5e-8,
        fee_rate=system.config.fee_rate,
        minimum_spread_rate=system.config.minimum_spread_rate,
        slippage_rate=system.config.slippage_rate,
        max_quote_timestamp_skew_seconds=system.config.max_quote_timestamp_skew_seconds,
    )

    assert not result.valid
    assert "differs from persisted authority" in result.message


def test_v2_verification_is_independent_of_current_configuration(tmp_path, monkeypatch, capsys):
    from scripts import backup_forward
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="v2-config-independent", commit_hash="a" * 40,
    )
    def forbidden(*_args, **_kwargs):
        pytest.fail("verification read current runtime configuration")
    monkeypatch.setattr(backup_forward, "load_settings", forbidden)
    monkeypatch.setattr(backup_forward, "load_paper_configuration", forbidden)
    monkeypatch.setattr(backup_forward.sys, "argv", ["backup_forward.py", "verify", str(backup)])
    backup_forward.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "VERIFIED"
    assert result["temporary_restore_valid"] is True
@pytest.mark.parametrize("version", [999, "2", True, None, 2.0])
def test_unknown_or_malformed_backup_manifest_version_is_rejected(tmp_path, version):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="future-manifest", commit_hash="a" * 40,
    )
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_manifest_version"] = version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="backup manifest version"):
        verify_backup(backup)


def test_current_v2_backup_cannot_downgrade_to_unversioned_legacy(tmp_path):
    system, project = _verified_execution_system(tmp_path)
    backup = create_verified_backup(
        project_root=project,
        database_path=system.store.path,
        output_root=tmp_path / "backups",
        lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="versionless-v2",
        commit_hash="a" * 40,
    )
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["backup_manifest_version"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Malformed legacy backup manifest fields"):
        verify_backup(backup)
