"""Non-overwriting, checksummed DuckDB backup and temporary restore verification."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any

import duckdb

from src.forward_operations import InterProcessLock
from src.release_provenance import ReleaseProvenanceError, capture_release_provenance
from src.paper_store import (
    CURRENT_SCHEMA_VERSION,
    PaperStore,
    resolve_persisted_quantity_tolerance,
)


# Explicit compatibility contract for pre-versioned historical backup manifests.
LEGACY_BACKUP_RECONCILIATION_SETTINGS = {
    "account_id": "locked_strategy",
    "quantity_tolerance": 1e-12,
    "fee_rate": 0.001,
    "minimum_spread_rate": 0.0002,
    "slippage_rate": 0.0005,
    "max_quote_timestamp_skew_seconds": 30,
}
BACKUP_MANIFEST_VERSION = 2
BACKUP_MANIFEST_V2_FIELDS = {
    "backup_manifest_version", "backup_timestamp", "commit_hash", "git_dirty",
    "hardening_manifest_sha256", "schema_version", "paper_schema",
    "execution_protocol_version", "locked_candidate_id", "experiment_id",
    "locked_strategy_hash", "governance_hash", "quantity_tolerance_authority_version",
    "checksums", "database_checks", "reconciliation_settings", "secrets_included",
    "retention_policy",
}


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _persisted_backup_authority(database_path: Path) -> dict[str, Any] | None:
    """Read immutable backup authority only from the persisted experiment record."""
    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = connection.execute(
            """SELECT experiment_id, locked_candidate_id, locked_strategy_hash,
                      governance_hash, specification
               FROM forward_experiments WHERE status='ACTIVE'"""
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("Persisted backup authority requires exactly one active experiment")
        experiment_id, candidate_id, strategy_hash, governance_hash, raw_specification = rows[0]
        if re.fullmatch(r"[0-9a-f]{64}", strategy_hash or "") is None or re.fullmatch(r"[0-9a-f]{64}", governance_hash or "") is None:
            raise ValueError("Persisted experiment identity hashes are malformed")
        if not isinstance(experiment_id, str) or not experiment_id or not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("Persisted experiment identity is malformed")
        try:
            specification = json.loads(raw_specification) if isinstance(raw_specification, str) else dict(raw_specification)
            if type(specification) is not dict:
                raise TypeError
            tolerance, tolerance_authority_version, has_quantity_authority = (
                resolve_persisted_quantity_tolerance(
                    specification,
                    legacy_tolerance=LEGACY_BACKUP_RECONCILIATION_SETTINGS["quantity_tolerance"],
                )
            )
            if not has_quantity_authority:
                raise ValueError("New backups require persisted quantity tolerance authority")
            candidate = specification["locked_strategy"]["candidate_id"]
            declared_strategy_hash = specification["locked_strategy_hash_sha256"]
            costs = specification["cost_assumptions"]
            raw_costs = [costs[k] for k in ("fee_rate", "minimum_spread_rate", "slippage_rate")]
            if (candidate != candidate_id or declared_strategy_hash != strategy_hash
                    or any(type(value) not in (int, float) for value in raw_costs)):
                raise ValueError
        except ValueError as error:
            if "quantity tolerance" in str(error).lower():
                raise
            raise ValueError("Persisted governed experiment identity/costs conflict") from error
        except (KeyError, TypeError) as error:
            raise ValueError("Persisted governed experiment identity/costs conflict") from error
        specification_protocol = specification.get("execution_protocol_version")
        if specification_protocol is not None and (
            type(specification_protocol) is not str or not specification_protocol
        ):
            raise ValueError("Persisted execution protocol identity is malformed")
        protocol_rows = connection.execute(
            "SELECT DISTINCT execution_protocol_version FROM paper_run_release_provenance"
        ).fetchall()
        if len(protocol_rows) > 1 or any(
            type(row[0]) is not str or not row[0] for row in protocol_rows
        ):
            raise ValueError("Persisted execution protocol identity is ambiguous")
        if protocol_rows and specification_protocol is not None and (
            protocol_rows[0][0] != specification_protocol
        ):
            raise ValueError("Persisted execution protocol conflicts with experiment specification")
        execution_protocol_version = (
            protocol_rows[0][0] if protocol_rows else specification_protocol
        )
        if execution_protocol_version is None:
            raise ValueError("Persisted execution protocol identity is missing")
        accounts = connection.execute("SELECT account_id FROM paper_accounts").fetchall()
        if len(accounts) != 1:
            raise ValueError("Persisted backup authority requires exactly one account")
        skew_rows = connection.execute(
            """SELECT DISTINCT q.max_timestamp_skew_seconds
               FROM paper_quote_coherence_context q JOIN paper_runs r ON r.run_id=q.run_id
               WHERE r.mode='PAPER' AND r.official_scheduled AND r.status='EXECUTED'
                 AND r.started_at_utc >= (SELECT applied_at_utc FROM paper_schema_versions WHERE version=13)"""
        ).fetchall()
        persisted_skew = specification.get("max_quote_timestamp_skew_seconds")
        if persisted_skew is not None and (
            type(persisted_skew) is not int or persisted_skew <= 0
        ):
            raise ValueError("Persisted quote-coherence reconciliation setting is malformed")
        if len(skew_rows) > 1 or any(
            type(row[0]) is not int or row[0] <= 0 for row in skew_rows
        ):
            raise ValueError("Persisted quote-coherence reconciliation setting is ambiguous")
        if skew_rows:
            if persisted_skew is not None and persisted_skew != skew_rows[0][0]:
                raise ValueError("Persisted quote-coherence setting conflicts with experiment specification")
            max_quote_timestamp_skew_seconds = skew_rows[0][0]
        elif persisted_skew is not None:
            max_quote_timestamp_skew_seconds = persisted_skew
        else:
            raise ValueError("Persisted quote-coherence reconciliation setting is missing")
        settings = {
            "account_id": accounts[0][0],
            "quantity_tolerance": float(tolerance),
            "fee_rate": float(costs["fee_rate"]),
            "minimum_spread_rate": float(costs["minimum_spread_rate"]),
            "slippage_rate": float(costs["slippage_rate"]),
            "max_quote_timestamp_skew_seconds": max_quote_timestamp_skew_seconds,
        }
        return {
            "experiment_id": experiment_id,
            "locked_candidate_id": candidate_id,
            "locked_strategy_hash": strategy_hash,
            "governance_hash": governance_hash,
            "quantity_tolerance": float(tolerance),
            "quantity_tolerance_authority_version": tolerance_authority_version,
            "execution_protocol_version": execution_protocol_version,
            "cost_assumptions": costs,
            "reconciliation_settings": settings,
        }


def _validate_v2_manifest(manifest: dict[str, Any]) -> None:
    if type(manifest) is not dict or set(manifest) != BACKUP_MANIFEST_V2_FIELDS:
        raise ValueError("Malformed backup manifest v2 fields")
    if type(manifest["backup_manifest_version"]) is not int or manifest["backup_manifest_version"] != 2:
        raise ValueError("Unsupported backup manifest version")
    for key in ("backup_timestamp", "commit_hash", "hardening_manifest_sha256", "paper_schema",
                "execution_protocol_version", "locked_candidate_id", "experiment_id",
            "locked_strategy_hash", "governance_hash"):
        if type(manifest[key]) is not str or not manifest[key]:
            raise ValueError(f"Malformed backup manifest field: {key}")
    try:
        backup_created_at = datetime.fromisoformat(
            manifest["backup_timestamp"].replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ValueError("Backup creation timestamp must be UTC") from error
    if backup_created_at.tzinfo is None or backup_created_at.utcoffset() != timedelta(0):
        raise ValueError("Backup creation timestamp must be UTC")
    if type(manifest["git_dirty"]) is not bool or manifest["git_dirty"]:
        raise ValueError("Backup provenance must identify a clean Git state")
    if type(manifest["quantity_tolerance_authority_version"]) is not str or manifest[
        "quantity_tolerance_authority_version"
    ] != "quantity-tolerance-v1":
        raise ValueError("Backup quantity tolerance authority version is unsupported")
    if re.fullmatch(r"[0-9a-f]{40}", manifest["commit_hash"]) is None:
        raise ValueError("Backup commit hash must be an exact Git SHA")
    if re.fullmatch(r"[0-9a-f]{64}", manifest["hardening_manifest_sha256"]) is None:
        raise ValueError("Backup hardening manifest SHA-256 is malformed")
    if re.fullmatch(r"[0-9a-f]{64}", manifest["locked_strategy_hash"]) is None or re.fullmatch(r"[0-9a-f]{64}", manifest["governance_hash"]) is None:
        raise ValueError("Backup persisted governance identity hashes are malformed")
    if not manifest["locked_candidate_id"] or not manifest["experiment_id"]:
        raise ValueError("Backup governed identity is empty")
    if type(manifest["schema_version"]) is not int or type(manifest["checksums"]) is not dict:
        raise ValueError("Malformed backup manifest schema/checksums")
    if manifest["paper_schema"] != f"paper-schema-v{manifest['schema_version']}":
        raise ValueError("Backup paper schema identifier conflicts with schema version")
    if any(type(k) is not str or not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
           for k, v in manifest["checksums"].items()):
        raise ValueError("Malformed backup checksum mapping")
    settings = manifest["reconciliation_settings"]
    expected = {"account_id", "quantity_tolerance", "fee_rate", "minimum_spread_rate",
                "slippage_rate", "max_quote_timestamp_skew_seconds"}
    if type(settings) is not dict or set(settings) != expected:
        raise ValueError("Malformed backup reconciliation settings")
    numeric_settings = ("quantity_tolerance", "fee_rate", "minimum_spread_rate", "slippage_rate")
    if type(settings["account_id"]) is not str or not settings["account_id"] or any(
        not _finite_number(settings[k]) for k in numeric_settings
    ) or type(settings["max_quote_timestamp_skew_seconds"]) is not int:
        raise ValueError("Malformed backup reconciliation setting types")
    if (not 0 < float(settings["quantity_tolerance"]) <= 1e-7
            or any(not 0 <= float(settings[k]) < 1 for k in numeric_settings[1:])
            or settings["max_quote_timestamp_skew_seconds"] <= 0):
        raise ValueError("Backup reconciliation settings are out of bounds")
    check_fields = {"tables", "schema_version", "cash_reconciles", "position_mismatches",
                    "orphan_fills", "account_statuses", "runtime_reconciliation"}
    checks = manifest["database_checks"]
    if type(checks) is not dict or set(checks) != check_fields:
        raise ValueError("Malformed backup database checks")
    if (type(checks["tables"]) is not list or any(type(name) is not str for name in checks["tables"])
            or type(checks["schema_version"]) is not int
            or type(checks["cash_reconciles"]) is not bool
            or type(checks["position_mismatches"]) is not int
            or type(checks["orphan_fills"]) is not int
            or type(checks["account_statuses"]) is not dict
            or type(checks["runtime_reconciliation"]) is not dict
            or set(checks["runtime_reconciliation"]) != {"valid", "message"}
            or type(checks["runtime_reconciliation"]["valid"]) is not bool
            or type(checks["runtime_reconciliation"]["message"]) is not str):
        raise ValueError("Malformed backup database check types")
    if type(manifest["secrets_included"]) is not bool or manifest["secrets_included"]:
        raise ValueError("Backup manifest secrets_included must be exactly false")
    if type(manifest["retention_policy"]) is not str:
        raise ValueError("Malformed backup retention policy")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_checks(
    database_path: Path, reconciliation_settings: dict[str, Any]
) -> dict[str, Any]:
    PaperStore.assert_supported_schema(database_path)
    with duckdb.connect(str(database_path), read_only=True) as connection:
        connection.execute("SET TimeZone='UTC'")
        tables = sorted(row[0] for row in connection.execute("SHOW TABLES").fetchall())
        account = connection.execute(
            "SELECT account_id, cash, status FROM paper_accounts"
        ).fetchall()
        ledger_cash = connection.execute(
            "SELECT account_id, SUM(amount) FROM cash_ledger GROUP BY account_id"
        ).fetchall()
        position_mismatch = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT COALESCE(p.account_id,l.account_id) account_id,
                         COALESCE(p.symbol,l.symbol) symbol,
                         COALESCE(p.quantity,0) current_qty,
                         COALESCE(l.ledger_qty,0) ledger_qty
                  FROM paper_positions p FULL OUTER JOIN (
                    SELECT account_id,symbol,SUM(quantity_delta) ledger_qty
                    FROM position_ledger GROUP BY account_id,symbol
                  ) l ON p.account_id=l.account_id AND p.symbol=l.symbol
                ) q WHERE ABS(current_qty-ledger_qty)>?
                """,
                [float(reconciliation_settings["quantity_tolerance"])],
            ).fetchone()[0]
        )
        orphan_fills = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM paper_fills f LEFT JOIN paper_orders o ON o.order_id=f.order_id
                WHERE o.order_id IS NULL OR f.filled_quantity<=0
                """
            ).fetchone()[0]
        )
        schema_version = int(
            connection.execute("SELECT COALESCE(MAX(version),0) FROM paper_schema_versions").fetchone()[0]
        )
    account_cash = {row[0]: float(row[1]) for row in account}
    ledger_cash_map = {row[0]: float(row[1]) for row in ledger_cash}
    cash_valid = all(abs(value - ledger_cash_map.get(key, 0.0)) <= 1e-7 for key, value in account_cash.items())
    reconciliation = PaperStore.reconcile_database(
        database_path,
        account_id=str(reconciliation_settings["account_id"]),
        quantity_tolerance=float(reconciliation_settings["quantity_tolerance"]),
        fee_rate=float(reconciliation_settings["fee_rate"]),
        minimum_spread_rate=float(reconciliation_settings["minimum_spread_rate"]),
        slippage_rate=float(reconciliation_settings["slippage_rate"]),
        max_quote_timestamp_skew_seconds=int(
            reconciliation_settings["max_quote_timestamp_skew_seconds"]
        ),
    )
    return {
        "tables": tables,
        "schema_version": schema_version,
        "cash_reconciles": cash_valid,
        "position_mismatches": position_mismatch,
        "orphan_fills": orphan_fills,
        "account_statuses": {row[0]: row[2] for row in account},
        "runtime_reconciliation": {
            "valid": reconciliation.valid,
            "message": reconciliation.message,
        },
    }


def create_verified_backup(
    *,
    project_root: Path,
    database_path: Path,
    output_root: Path,
    lock_path: Path,
    timestamp: str,
    commit_hash: str,
    reconciliation_settings: dict[str, Any] | None = None,
) -> Path:
    project_root = Path(project_root).resolve()
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise ValueError(f"backup source must be an existing regular file: {database_path}")
    PaperStore.assert_supported_schema(database_path)
    authority = _persisted_backup_authority(database_path)
    if authority is None:
        raise ValueError("New backup requires an active persisted forward experiment")
    reconciliation_settings = authority["reconciliation_settings"]
    backup_dir = Path(output_root).resolve() / timestamp
    if backup_dir.exists():
        raise FileExistsError(f"backup destination already exists: {backup_dir}")
    with InterProcessLock(lock_path, timeout_seconds=10, command_name="forward-backup"):
        if backup_dir.exists():
            raise FileExistsError(f"backup destination already exists: {backup_dir}")
        try:
            current_provenance = capture_release_provenance(project_root)
        except ReleaseProvenanceError as error:
            raise ValueError(f"Cannot create governed backup: {error}") from error
        # The source may have changed while waiting for the writer lock.
        PaperStore.assert_supported_schema(database_path)
        locked_authority = _persisted_backup_authority(database_path)
        if locked_authority != authority:
            raise ValueError("Persisted backup authority changed while waiting for the writer lock")
        if current_provenance.execution_protocol_version != authority["execution_protocol_version"]:
            raise ValueError("Active execution protocol conflicts with persisted backup authority")
        with duckdb.connect(str(database_path)) as connection:
            connection.execute("CHECKPOINT")
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        # A same-filesystem staging directory keeps partial copies unpublished;
        # its context removes all staged artifacts on copy or verification errors.
        with tempfile.TemporaryDirectory(prefix=".forward-backup-", dir=backup_dir.parent) as temporary:
            staging_dir = Path(temporary)
            copied_database = staging_dir / "paper_trading.duckdb"
            shutil.copy2(database_path, copied_database)
            for relative in ("forward_experiment", "reports/paper", "reports/forward_monthly"):
                source = project_root / relative
                if source.exists():
                    shutil.copytree(source, staging_dir / relative, dirs_exist_ok=False)
            checksums = {}
            for path in sorted(staging_dir.rglob("*")):
                if path.is_file():
                    checksums[str(path.relative_to(staging_dir)).replace("\\", "/")] = _sha256(path)
            db_checks = _database_checks(copied_database, reconciliation_settings)
            manifest = {
                "backup_timestamp": current_provenance.captured_at_utc.astimezone(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "commit_hash": commit_hash,
                "schema_version": db_checks["schema_version"],
                "checksums": checksums,
                "database_checks": db_checks,
                "reconciliation_settings": reconciliation_settings,
                "secrets_included": False,
                "retention_policy": "non-destructive; deletion requires explicit human approval",
            }
            manifest.update({
                "backup_manifest_version": BACKUP_MANIFEST_VERSION,
                "commit_hash": current_provenance.git_commit,
                "git_dirty": current_provenance.git_dirty,
                "hardening_manifest_sha256": current_provenance.hardening_manifest_sha256,
                "paper_schema": f"paper-schema-v{db_checks['schema_version']}",
                "execution_protocol_version": current_provenance.execution_protocol_version,
                "locked_candidate_id": authority["locked_candidate_id"],
                "experiment_id": authority["experiment_id"],
                "locked_strategy_hash": authority["locked_strategy_hash"],
                "governance_hash": authority["governance_hash"],
                "quantity_tolerance_authority_version": authority[
                    "quantity_tolerance_authority_version"
                ],
            })
            _validate_v2_manifest(manifest)
            manifest_path = staging_dir / "backup_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            (staging_dir / "backup_manifest.sha256").write_text(
                f"{_sha256(manifest_path)}  backup_manifest.json\n", encoding="ascii"
            )
            verify_backup(staging_dir)
            if backup_dir.exists():
                raise FileExistsError(f"backup destination already exists: {backup_dir}")
            staging_dir.rename(backup_dir)
    return backup_dir


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = Path(backup_dir).resolve()
    manifest_path = backup_dir / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(manifest) is not dict:
        raise ValueError("Malformed backup manifest object")
    version = manifest.get("backup_manifest_version", 1)
    if type(version) is not int or version not in (1, BACKUP_MANIFEST_VERSION):
        raise ValueError("Unsupported backup manifest version")
    declared_schema_version = manifest.get("schema_version")
    if type(declared_schema_version) is int and declared_schema_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported future backup schema version {declared_schema_version}; "
            f"maximum supported is {CURRENT_SCHEMA_VERSION}"
        )
    if version == BACKUP_MANIFEST_VERSION:
        _validate_v2_manifest(manifest)
    elif set(manifest) not in (
        {"backup_manifest_version", "backup_timestamp", "commit_hash", "schema_version", "checksums",
         "database_checks", "reconciliation_settings", "secrets_included", "retention_policy"},
        {"backup_manifest_version", "backup_timestamp", "commit_hash", "schema_version", "checksums",
         "database_checks", "secrets_included", "retention_policy"},
        {"backup_timestamp", "commit_hash", "schema_version", "checksums", "database_checks",
         "reconciliation_settings", "secrets_included", "retention_policy"},
        {"backup_timestamp", "commit_hash", "schema_version", "checksums", "database_checks",
         "secrets_included", "retention_policy"},
    ):
        raise ValueError("Malformed legacy backup manifest fields")
    if version == 1:
        if (
            type(manifest.get("backup_timestamp")) is not str
            or not manifest["backup_timestamp"]
            or type(manifest.get("commit_hash")) is not str
            or not manifest["commit_hash"]
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] < 0
            or type(manifest.get("checksums")) is not dict
            or type(manifest.get("database_checks")) is not dict
            or type(manifest.get("secrets_included")) is not bool
            or manifest["secrets_included"]
            or type(manifest.get("retention_policy")) is not str
        ):
            raise ValueError("Malformed legacy backup manifest values")
        legacy_settings = manifest.get("reconciliation_settings")
        if legacy_settings is not None:
            expected_settings = {
                "account_id", "quantity_tolerance", "fee_rate", "minimum_spread_rate",
                "slippage_rate", "max_quote_timestamp_skew_seconds",
            }
            numeric_settings = (
                "quantity_tolerance", "fee_rate", "minimum_spread_rate", "slippage_rate"
            )
            if (
                type(legacy_settings) is not dict
                or set(legacy_settings) != expected_settings
                or type(legacy_settings["account_id"]) is not str
                or not legacy_settings["account_id"]
                or any(not _finite_number(legacy_settings[key]) for key in numeric_settings)
                or type(legacy_settings["max_quote_timestamp_skew_seconds"]) is not int
                or not 0 < float(legacy_settings["quantity_tolerance"]) <= 1e-7
                or any(
                    not 0 <= float(legacy_settings[key]) < 1
                    for key in numeric_settings[1:]
                )
                or legacy_settings["max_quote_timestamp_skew_seconds"] <= 0
            ):
                raise ValueError("Malformed legacy backup reconciliation settings")
    if type(manifest["checksums"]) is not dict:
        raise ValueError("Malformed backup checksum mapping")
    expected_files: set[str] = set()
    for relative, expected in manifest["checksums"].items():
        if (
            type(relative) is not str
            or "\\" in relative
            or ":" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
            or type(expected) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise ValueError("Malformed backup checksum entry")
        path = backup_dir.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.resolve().is_relative_to(backup_dir) or not path.is_file() or _sha256(path) != expected:
            raise ValueError(f"backup checksum mismatch: {relative}")
        expected_files.add(relative)
    actual_files = set()
    for path in backup_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError("Backup must not contain symbolic links")
        if path.is_file():
            relative = path.relative_to(backup_dir).as_posix()
            if relative not in {"backup_manifest.json", "backup_manifest.sha256"}:
                actual_files.add(relative)
    if actual_files != expected_files:
        raise ValueError("Backup files do not exactly match the checksummed manifest entries")
    sidecar_expected = (backup_dir / "backup_manifest.sha256").read_text(encoding="ascii").split()[0]
    if _sha256(manifest_path) != sidecar_expected:
        raise ValueError("backup manifest checksum mismatch")
    if version == BACKUP_MANIFEST_VERSION:
        archived_hardening_manifest = backup_dir / "forward_experiment" / "hardening_manifest.json"
        if (
            not archived_hardening_manifest.is_file()
            or _sha256(archived_hardening_manifest) != manifest["hardening_manifest_sha256"]
        ):
            raise ValueError("backup hardening manifest SHA-256 does not match archived evidence")
    manifest_schema_version = manifest.get("schema_version")
    if type(manifest_schema_version) is int and manifest_schema_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported future backup schema version {manifest_schema_version}; "
            f"maximum supported is {CURRENT_SCHEMA_VERSION}"
        )
    try:
        authority = _persisted_backup_authority(backup_dir / "paper_trading.duckdb") if version == BACKUP_MANIFEST_VERSION else None
    except ValueError as error:
        raise ValueError(f"backup database failed runtime reconciliation: {error}") from error
    if version == BACKUP_MANIFEST_VERSION:
        if authority is None:
            raise ValueError("V2 backup lacks persisted governed reconciliation authority")
        if (authority["experiment_id"] != manifest["experiment_id"]
                or authority["locked_candidate_id"] != manifest["locked_candidate_id"]
                or authority["locked_strategy_hash"] != manifest["locked_strategy_hash"]
                or authority["governance_hash"] != manifest["governance_hash"]
                or authority["quantity_tolerance"] != manifest["reconciliation_settings"]["quantity_tolerance"]
                or authority["quantity_tolerance_authority_version"]
                != manifest["quantity_tolerance_authority_version"]
                or authority["execution_protocol_version"]
                != manifest["execution_protocol_version"]
                or authority["reconciliation_settings"] != manifest["reconciliation_settings"]
                or manifest["database_checks"]["schema_version"] != manifest["schema_version"]):
            raise ValueError("backup database failed runtime reconciliation: manifest conflicts with persisted authority")
        reconciliation_settings = manifest["reconciliation_settings"]
    else:
        reconciliation_settings = {
            **LEGACY_BACKUP_RECONCILIATION_SETTINGS,
            **manifest.get("reconciliation_settings", {}),
        }
    checks = _database_checks(backup_dir / "paper_trading.duckdb", reconciliation_settings)
    valid = bool(
        checks["cash_reconciles"]
        and checks["position_mismatches"] == 0
        and checks["orphan_fills"] == 0
        and checks["schema_version"] == manifest["schema_version"]
        and checks["runtime_reconciliation"]["valid"]
    )
    if not valid:
        raise ValueError(
            "backup database failed runtime reconciliation: "
            f"{checks['runtime_reconciliation']['message']}"
        )
    if version == BACKUP_MANIFEST_VERSION and checks != manifest["database_checks"]:
        raise ValueError("Backup database checks do not match recomputed persisted checks")
    return {"valid": True, "database_checks": checks, "checksums": len(manifest["checksums"])}


def verify_restore_to_temporary(backup_dir: Path, temporary_root: Path) -> dict[str, Any]:
    verify_backup(backup_dir)
    restore_dir = Path(temporary_root).resolve()
    restore_dir.mkdir(parents=True, exist_ok=False)
    restored_database = restore_dir / "paper_trading.duckdb"
    shutil.copy2(Path(backup_dir) / "paper_trading.duckdb", restored_database)
    manifest = json.loads((Path(backup_dir) / "backup_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("backup_manifest_version") == BACKUP_MANIFEST_VERSION:
        reconciliation_settings = manifest["reconciliation_settings"]
    else:
        reconciliation_settings = {
            **LEGACY_BACKUP_RECONCILIATION_SETTINGS,
            **manifest.get("reconciliation_settings", {}),
        }
    checks = _database_checks(restored_database, reconciliation_settings)
    return {
        "valid": bool(
            checks["cash_reconciles"]
            and checks["position_mismatches"] == 0
            and checks["orphan_fills"] == 0
            and checks["runtime_reconciliation"]["valid"]
        ),
        "restored_database": str(restored_database),
        "production_database_untouched": True,
        "database_checks": checks,
    }
