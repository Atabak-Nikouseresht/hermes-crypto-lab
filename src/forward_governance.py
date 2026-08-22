"""Immutable governance and locked-strategy identity for forward research."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from src.forward_operations import verify_immutable_manifest
from src.paper_broker import PaperConfig
from src.paper_store import PaperStore

LOCKED_STRATEGY_HASH_SHA256 = "29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6"
CHECKPOINT_MANIFEST_HASH_SHA256 = "97e8d1770a1d78010566760ac3d4121b8b6eafd8f13b617782286cbfaab31c4b"
GOVERNANCE_HASH_SHA256 = "f73f0897c15666daf3c6ff09b2ca4ec9737f412f9cf462bc2c3db8216bd0ef69"


def locked_strategy_spec(config: PaperConfig) -> dict[str, Any]:
    strategy = config.strategy_config
    return {
        "candidate_id": config.locked_candidate_id,
        "parameters": {
            "momentum_window": strategy.momentum_long_days,
            "skip_window": strategy.momentum_skip_days,
            "trend_filter": strategy.btc_moving_average_days,
            "selected_assets": strategy.max_assets,
            "rebalance_days": config.rebalance_days,
            "volatility_window": strategy.volatility_days,
        },
    }


def locked_strategy_hash(config: PaperConfig) -> str:
    canonical = json.dumps(
        locked_strategy_spec(config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def create_immutable_governance(
    path: Path, payload: dict[str, Any]
) -> tuple[str, Path]:
    path = Path(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        with sidecar.open("x", encoding="ascii", newline="\n") as handle:
            handle.write(f"{digest}  {path.name}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    for immutable_path in (path, sidecar):
        try:
            immutable_path.chmod(stat.S_IREAD)
        except OSError:
            pass
    return digest, sidecar


def verify_governance(path: Path, sidecar: Path) -> str:
    return verify_immutable_manifest(path, sidecar)


def verify_trust_anchors(project_root: Path, config: PaperConfig) -> dict[str, str]:
    """Verify files against code-anchored release digests, not mutable sidecars alone."""
    project_root = Path(project_root)
    checkpoint = project_root / "forward_experiment" / "checkpoint_manifest.json"
    governance = project_root / "forward_experiment" / "governance.json"
    actual_checkpoint = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    actual_governance = hashlib.sha256(governance.read_bytes()).hexdigest()
    actual_locked = locked_strategy_hash(config)
    expected = {
        "checkpoint": CHECKPOINT_MANIFEST_HASH_SHA256,
        "governance": GOVERNANCE_HASH_SHA256,
        "locked_strategy": LOCKED_STRATEGY_HASH_SHA256,
    }
    actual = {
        "checkpoint": actual_checkpoint,
        "governance": actual_governance,
        "locked_strategy": actual_locked,
    }
    if actual != expected:
        raise ValueError(f"Forward trust-anchor mismatch: expected={expected}, actual={actual}")
    verify_immutable_manifest(
        checkpoint, project_root / "forward_experiment" / "checkpoint_manifest.sha256"
    )
    verify_immutable_manifest(
        governance, project_root / "forward_experiment" / "governance.json.sha256"
    )
    return actual


def bootstrap_forward_experiment(
    store: PaperStore, project_root: Path, config: PaperConfig
) -> str:
    """Idempotently register the code-anchored governance in DuckDB."""
    verified = verify_trust_anchors(project_root, config)
    governance_path = Path(project_root) / "forward_experiment" / "governance.json"
    payload = json.loads(governance_path.read_text(encoding="utf-8"))
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT experiment_id, locked_candidate_id, locked_strategy_hash, governance_hash, status "
            "FROM forward_experiments"
        ).fetchall()
        if not rows:
            connection.execute(
                "INSERT INTO forward_experiments VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')",
                [
                    payload["experiment_id"],
                    payload["experiment_start_utc"],
                    payload["locked_strategy"]["candidate_id"],
                    verified["locked_strategy"],
                    verified["governance"],
                    json.dumps(payload, sort_keys=True),
                ],
            )
        else:
            if len(rows) != 1:
                raise ValueError("Multiple forward experiments exist; automatic selection is forbidden")
            expected = (
                payload["experiment_id"],
                payload["locked_strategy"]["candidate_id"],
                verified["locked_strategy"],
                verified["governance"],
                "ACTIVE",
            )
            if tuple(rows[0]) != expected:
                raise ValueError(
                    f"Persistent forward governance differs from release anchor: {rows[0]}"
                )
    return str(payload["experiment_id"])
