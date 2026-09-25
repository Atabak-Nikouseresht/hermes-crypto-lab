"""Backup publication and offline authority regression tests."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import duckdb
import pytest

from run_paper import load_paper_configuration
from scripts import backup_forward
from src import backup_restore
from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.forward_governance import economic_spec_hash_v2, economic_spec_v2
from src.paper_broker import PaperConfig, PaperTradingSystem
from src.paper_store import CURRENT_SCHEMA_VERSION
from src.release_provenance import ReleaseProvenance


def _backup_arguments(tmp_path: Path) -> dict:
    project = tmp_path / "project"
    database = project / "database" / "paper_trading.duckdb"
    database.parent.mkdir(parents=True)
    return {
        "project_root": project,
        "database_path": database,
        "output_root": tmp_path / "backups",
        "lock_path": project / "runtime" / "forward_writer.lock",
        "timestamp": "20260922T120000Z",
        "commit_hash": "a" * 40,
    }


def _initialized_arguments(tmp_path: Path, config: PaperConfig | None = None) -> dict:
    arguments = _backup_arguments(tmp_path)
    config = config or PaperConfig(assets=("BTC/USDT",))
    project_root = Path(__file__).resolve().parents[1]
    governed_config, _ = load_paper_configuration(project_root)
    persisted_economic_spec = economic_spec_v2(governed_config)
    assert persisted_economic_spec["execution"]["quantity_tolerance"] == config.quantity_tolerance
    experiment_root = arguments["project_root"] / "forward_experiment"
    experiment_root.mkdir(parents=True)
    (experiment_root / "hardening_manifest.json").write_text("{}", encoding="utf-8")
    PaperTradingSystem(
        arguments["database_path"], config
    )
    with duckdb.connect(str(arguments["database_path"])) as connection:
        specification = {
            "locked_strategy": {"candidate_id": "test-candidate"},
            "locked_strategy_hash_sha256": "e" * 64,
            "economic_spec_v2": persisted_economic_spec,
            "economic_spec_v2_sha256": economic_spec_hash_v2(governed_config),
            "quantity_tolerance_authority_version": "quantity-tolerance-v1",
            "max_quote_timestamp_skew_seconds": config.max_quote_timestamp_skew_seconds,
            "execution_protocol_version": EXECUTION_PROTOCOL_VERSION,
            "cost_assumptions": {
                "fee_rate": config.fee_rate,
                "minimum_spread_rate": config.minimum_spread_rate,
                "slippage_rate": config.slippage_rate,
            },
        }
        connection.execute(
            "INSERT INTO forward_experiments VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')",
            [
                "test-experiment",
                datetime.now(timezone.utc),
                "test-candidate",
                "e" * 64,
                "f" * 64,
                json.dumps(specification),
            ],
        )
    return arguments


@pytest.fixture(autouse=True)
def _controlled_release_provenance(monkeypatch):
    def capture(project_root):
        manifest = Path(project_root) / "forward_experiment" / "hardening_manifest.json"
        return ReleaseProvenance(
            git_commit="c" * 40,
            git_dirty=False,
            hardening_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            execution_protocol_version="paper-exec-v3-ask-bid-minspread-utc0010",
            captured_at_utc=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(backup_restore, "capture_release_provenance", capture)


def test_current_schema_backup_without_persisted_experiment_fails_closed(tmp_path):
    arguments = _backup_arguments(tmp_path)
    PaperTradingSystem(arguments["database_path"], PaperConfig(assets=("BTC/USDT",)))

    with pytest.raises(ValueError, match="active persisted forward experiment"):
        backup_restore.create_verified_backup(**arguments)

    assert not arguments["output_root"].exists()


def _tree_state(root: Path) -> dict:
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("failure", [FileNotFoundError("missing config"), ValueError("broken config")])
def test_verify_cli_ignores_current_configuration(tmp_path, monkeypatch, capsys, failure):
    arguments = _initialized_arguments(tmp_path)
    backup = backup_restore.create_verified_backup(**arguments)
    before = _tree_state(tmp_path)

    def broken_configuration(*args, **kwargs):
        raise failure

    settings_calls = []

    def current_settings():
        settings_calls.append(True)
        return SimpleNamespace(project_root=arguments["project_root"])

    monkeypatch.setattr(backup_forward, "load_settings", current_settings)
    monkeypatch.setattr(backup_forward, "load_paper_configuration", broken_configuration)
    monkeypatch.setattr(backup_forward.sys, "argv", ["backup_forward.py", "verify", str(backup)])
    backup_forward.main()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "VERIFIED"
    assert result["temporary_restore_valid"] is True
    assert result["production_database_untouched"] is True
    assert "locked_candidate" not in result
    assert settings_calls == []
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("source_kind", ["missing", "directory"])
def test_create_requires_existing_regular_database_without_side_effects(tmp_path, source_kind):
    arguments = _backup_arguments(tmp_path)
    if source_kind == "directory":
        arguments["database_path"].mkdir()
    before = _tree_state(tmp_path)
    try:
        with pytest.raises(ValueError, match="existing regular file"):
            backup_restore.create_verified_backup(**arguments)
    finally:
        assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("failure_stage", ["copy", "archive_copy", "database_checks", "verification", "publication"])
def test_failed_backup_never_publishes_partial_directory(tmp_path, monkeypatch, failure_stage):
    arguments = _initialized_arguments(tmp_path)
    final = arguments["output_root"] / arguments["timestamp"]
    final_visible_during_failure = []

    def fail(*args, **kwargs):
        final_visible_during_failure.append(final.exists())
        if failure_stage == "copy":
            Path(args[1]).write_bytes(b"incomplete copy")
        elif failure_stage == "archive_copy":
            destination = Path(args[1])
            destination.mkdir()
            (destination / "partial.json").write_text("{", encoding="utf-8")
        raise OSError("injected backup failure")

    if failure_stage == "copy":
        monkeypatch.setattr(backup_restore.shutil, "copy2", fail)
    elif failure_stage == "archive_copy":
        (arguments["project_root"] / "forward_experiment").mkdir(exist_ok=True)
        monkeypatch.setattr(backup_restore.shutil, "copytree", fail)
    elif failure_stage == "database_checks":
        monkeypatch.setattr(backup_restore, "_database_checks", fail)
    elif failure_stage == "verification":
        monkeypatch.setattr(backup_restore, "verify_backup", fail)
    else:
        monkeypatch.setattr(Path, "rename", fail)

    with pytest.raises(OSError, match="injected backup failure"):
        backup_restore.create_verified_backup(**arguments)

    assert final_visible_during_failure == [False]
    assert not final.exists()
    assert not arguments["output_root"].exists() or list(arguments["output_root"].iterdir()) == []


def _make_future_database(database: Path, *, remove_current_table: bool = False) -> None:
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "UPDATE paper_schema_versions SET version=? "
            "WHERE version=(SELECT MAX(version) FROM paper_schema_versions)",
            [CURRENT_SCHEMA_VERSION + 1],
        )
        if remove_current_table:
            connection.execute("DROP TABLE paper_accounts")


def _write_manifest(backup: Path, manifest: dict) -> None:
    path = backup / "backup_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (backup / "backup_manifest.sha256").write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  backup_manifest.json\n",
        encoding="ascii",
    )


def _create_legacy_backup_fixture(tmp_path: Path, *, account_id: str, include_settings: bool) -> Path:
    arguments = _backup_arguments(tmp_path)
    config = PaperConfig(assets=("BTC/USDT",), account_id=account_id)
    PaperTradingSystem(arguments["database_path"], config)
    backup = tmp_path / "legacy-backup"
    backup.mkdir()
    database_copy = backup / "paper_trading.duckdb"
    shutil.copy2(arguments["database_path"], database_copy)
    checksums = {"paper_trading.duckdb": hashlib.sha256(database_copy.read_bytes()).hexdigest()}
    settings = {
        "account_id": account_id,
        "quantity_tolerance": config.quantity_tolerance,
        "fee_rate": config.fee_rate,
        "minimum_spread_rate": config.minimum_spread_rate,
        "slippage_rate": config.slippage_rate,
        "max_quote_timestamp_skew_seconds": config.max_quote_timestamp_skew_seconds,
    }
    checks = backup_restore._database_checks(database_copy, settings)
    manifest = {
        "backup_manifest_version": 1,
        "backup_timestamp": "20260822T120000Z",
        "commit_hash": "a" * 40,
        "schema_version": checks["schema_version"],
        "checksums": checksums,
        "database_checks": checks,
        "secrets_included": False,
        "retention_policy": "legacy fixture; no automatic deletion",
    }
    if include_settings:
        manifest["reconciliation_settings"] = settings
    _write_manifest(backup, manifest)
    return backup


def test_create_rejects_future_source_before_writable_checkpoint(tmp_path, monkeypatch):
    arguments = _initialized_arguments(tmp_path)
    _make_future_database(arguments["database_path"])
    before = _tree_state(tmp_path)
    connect = duckdb.connect
    writable_connections = []

    def track_connect(*args, **kwargs):
        if not kwargs.get("read_only", False):
            writable_connections.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(backup_restore.duckdb, "connect", track_connect)
    with pytest.raises(ValueError, match="future.*schema|future paper schema"):
        backup_restore.create_verified_backup(**arguments)
    assert writable_connections == []
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("remove_current_table", [False, True])
def test_verify_future_snapshot_rejects_before_current_queries_without_mutation(
    tmp_path, remove_current_table
):
    arguments = _initialized_arguments(tmp_path)
    backup = backup_restore.create_verified_backup(**arguments)
    database = backup / "paper_trading.duckdb"
    _make_future_database(database, remove_current_table=remove_current_table)
    manifest = json.loads((backup / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    manifest["paper_schema"] = f"paper-schema-v{CURRENT_SCHEMA_VERSION + 1}"
    manifest["database_checks"]["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    manifest["checksums"]["paper_trading.duckdb"] = hashlib.sha256(database.read_bytes()).hexdigest()
    _write_manifest(backup, manifest)
    before = _tree_state(tmp_path)

    with pytest.raises(ValueError, match="future.*schema|future paper schema"):
        backup_restore.verify_backup(backup)
    with pytest.raises(ValueError, match="future.*schema|future paper schema"):
        backup_restore.verify_restore_to_temporary(backup, tmp_path / "restore")
    assert _tree_state(tmp_path) == before


def test_verify_rejects_manifest_declaring_future_schema(tmp_path):
    arguments = _initialized_arguments(tmp_path)
    backup = backup_restore.create_verified_backup(**arguments)
    manifest = json.loads((backup / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    manifest["paper_schema"] = f"paper-schema-v{CURRENT_SCHEMA_VERSION + 1}"
    _write_manifest(backup, manifest)

    with pytest.raises(ValueError, match="future.*schema|future paper schema"):
        backup_restore.verify_backup(backup)


def test_publication_follows_verification_and_preserves_existing_backup(tmp_path, monkeypatch):
    arguments = _initialized_arguments(tmp_path)
    final = arguments["output_root"] / arguments["timestamp"]
    verify = backup_restore.verify_backup
    verified_paths = []

    def verify_staged(path):
        assert not final.exists()
        assert path.parent == final.parent
        assert path.name.startswith(".forward-backup-")
        verified_paths.append(path)
        return verify(path)

    monkeypatch.setattr(backup_restore, "verify_backup", verify_staged)
    backup = backup_restore.create_verified_backup(**arguments)
    assert backup == final
    assert len(verified_paths) == 1
    assert not verified_paths[0].exists()
    assert list(arguments["output_root"].iterdir()) == [final]
    assert verify(final)["valid"] is True
    before = _tree_state(tmp_path)
    with pytest.raises(FileExistsError):
        backup_restore.create_verified_backup(**arguments)
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("legacy_settings", [False, True])
def test_verification_uses_manifest_settings_or_explicit_legacy_defaults(tmp_path, legacy_settings):
    backup = _create_legacy_backup_fixture(
        tmp_path,
        account_id="locked_strategy" if legacy_settings else "archived_account",
        include_settings=not legacy_settings,
    )
    before = _tree_state(backup)
    assert backup_restore.verify_backup(backup)["valid"] is True
    assert backup_restore.verify_restore_to_temporary(backup, tmp_path / "restore")["valid"] is True
    assert _tree_state(backup) == before


@pytest.mark.parametrize("include_settings", [False, True])
def test_unversioned_pre_batch2_backup_manifests_remain_verifiable(tmp_path, include_settings):
    backup = _create_legacy_backup_fixture(
        tmp_path,
        account_id="archived_account" if include_settings else "locked_strategy",
        include_settings=include_settings,
    )
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["backup_manifest_version"]
    _write_manifest(backup, manifest)
    before = _tree_state(backup)

    assert backup_restore.verify_backup(backup)["valid"] is True
    assert backup_restore.verify_restore_to_temporary(backup, tmp_path / "restore")["valid"] is True
    assert _tree_state(backup) == before


def test_legacy_backup_cannot_override_pre_adoption_forward_tolerance(tmp_path):
    backup = _create_legacy_backup_fixture(
        tmp_path, account_id="locked_strategy", include_settings=True
    )
    database = backup / "paper_trading.duckdb"
    legacy_specification = json.loads(
        (Path(__file__).resolve().parents[1] / "forward_experiment" / "governance.json")
        .read_text(encoding="utf-8")
    )
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES ('historic', ?, 'candidate', ?, ?, ?, 'ACTIVE')",
            [
                datetime.now(timezone.utc),
                "a" * 64,
                "b" * 64,
                json.dumps(legacy_specification, sort_keys=True),
            ],
        )

    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reconciliation_settings"]["quantity_tolerance"] = 1e-7
    manifest["checksums"]["paper_trading.duckdb"] = hashlib.sha256(database.read_bytes()).hexdigest()
    _write_manifest(backup, manifest)

    with pytest.raises(ValueError, match="legacy forward reconciliation contract"):
        backup_restore.verify_backup(backup)
    with pytest.raises(ValueError, match="legacy forward reconciliation contract"):
        backup_restore.verify_restore_to_temporary(backup, tmp_path / "restore")


def test_current_v2_backup_cannot_fall_back_when_reconciliation_settings_are_missing(tmp_path):
    arguments = _initialized_arguments(tmp_path)
    backup = backup_restore.create_verified_backup(**arguments)
    manifest = json.loads((backup / "backup_manifest.json").read_text(encoding="utf-8"))
    del manifest["reconciliation_settings"]
    _write_manifest(backup, manifest)

    with pytest.raises(ValueError, match="Malformed backup manifest v2 fields"):
        backup_restore.verify_backup(backup)


@pytest.mark.parametrize("source_change", ["missing", "future"])
def test_create_rechecks_source_after_waiting_for_writer_lock(tmp_path, monkeypatch, source_change):
    arguments = _initialized_arguments(tmp_path)
    lock = backup_restore.InterProcessLock
    connect = duckdb.connect
    writable_connections = []
    source_state = []

    def track_connect(*args, **kwargs):
        if not kwargs.get("read_only", False):
            writable_connections.append(args[0])
        return connect(*args, **kwargs)

    @contextmanager
    def changed_source_lock(*args, **kwargs):
        with lock(*args, **kwargs):
            if source_change == "missing":
                arguments["database_path"].unlink()
            else:
                _make_future_database(arguments["database_path"])
            writable_connections.clear()
            source_state.append(_tree_state(arguments["database_path"].parent))
            yield

    monkeypatch.setattr(backup_restore.duckdb, "connect", track_connect)
    monkeypatch.setattr(backup_restore, "InterProcessLock", changed_source_lock)
    with pytest.raises((ValueError, FileNotFoundError), match="future.*schema|does not exist"):
        backup_restore.create_verified_backup(**arguments)
    assert writable_connections == []
    assert _tree_state(arguments["database_path"].parent) == source_state[0]
    assert not arguments["output_root"].exists()
