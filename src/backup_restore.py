"""Non-overwriting, checksummed DuckDB backup and temporary restore verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

import duckdb

from src.forward_operations import InterProcessLock
from src.paper_store import PaperStore


DEFAULT_RECONCILIATION_SETTINGS = {
    "account_id": "locked_strategy",
    "quantity_tolerance": 1e-12,
    "fee_rate": 0.001,
    "minimum_spread_rate": 0.0002,
    "slippage_rate": 0.0005,
    "max_quote_timestamp_skew_seconds": 30,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_checks(
    database_path: Path, reconciliation_settings: dict[str, Any]
) -> dict[str, Any]:
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
                ) q WHERE ABS(current_qty-ledger_qty)>1e-7
                """
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
    reconciliation_settings = {
        **DEFAULT_RECONCILIATION_SETTINGS,
        **(reconciliation_settings or {}),
    }
    backup_dir = Path(output_root).resolve() / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    with InterProcessLock(lock_path, timeout_seconds=10, command_name="forward-backup"):
        with duckdb.connect(str(database_path)) as connection:
            connection.execute("CHECKPOINT")
        copied_database = backup_dir / "paper_trading.duckdb"
        shutil.copy2(database_path, copied_database)
        for relative in ("forward_experiment", "reports/paper", "reports/forward_monthly"):
            source = project_root / relative
            if source.exists():
                shutil.copytree(source, backup_dir / relative, dirs_exist_ok=False)
        checksums = {}
        for path in sorted(backup_dir.rglob("*")):
            if path.is_file():
                checksums[str(path.relative_to(backup_dir)).replace("\\", "/")] = _sha256(path)
        db_checks = _database_checks(copied_database, reconciliation_settings)
        manifest = {
            "backup_timestamp": timestamp,
            "commit_hash": commit_hash,
            "schema_version": db_checks["schema_version"],
            "checksums": checksums,
            "database_checks": db_checks,
            "reconciliation_settings": reconciliation_settings,
            "secrets_included": False,
            "retention_policy": "non-destructive; deletion requires explicit human approval",
        }
        manifest_path = backup_dir / "backup_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        (backup_dir / "backup_manifest.sha256").write_text(
            f"{_sha256(manifest_path)}  backup_manifest.json\n", encoding="ascii"
        )
    verify_backup(backup_dir)
    return backup_dir


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = Path(backup_dir).resolve()
    manifest_path = backup_dir / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative, expected in manifest["checksums"].items():
        path = backup_dir / relative
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError(f"backup checksum mismatch: {relative}")
    sidecar_expected = (backup_dir / "backup_manifest.sha256").read_text(encoding="ascii").split()[0]
    if _sha256(manifest_path) != sidecar_expected:
        raise ValueError("backup manifest checksum mismatch")
    reconciliation_settings = {
        **DEFAULT_RECONCILIATION_SETTINGS,
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
    return {"valid": True, "database_checks": checks, "checksums": len(manifest["checksums"])}


def verify_restore_to_temporary(backup_dir: Path, temporary_root: Path) -> dict[str, Any]:
    verify_backup(backup_dir)
    restore_dir = Path(temporary_root).resolve()
    restore_dir.mkdir(parents=True, exist_ok=False)
    restored_database = restore_dir / "paper_trading.duckdb"
    shutil.copy2(Path(backup_dir) / "paper_trading.duckdb", restored_database)
    manifest = json.loads((Path(backup_dir) / "backup_manifest.json").read_text(encoding="utf-8"))
    reconciliation_settings = {
        **DEFAULT_RECONCILIATION_SETTINGS,
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
