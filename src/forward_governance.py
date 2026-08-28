"""Immutable governance and locked-strategy identity for forward research."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.forward_operations import verify_immutable_manifest
from src.paper_broker import PaperConfig
from src.paper_store import PaperStore

LOCKED_STRATEGY_HASH_SHA256 = "29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6"
CHECKPOINT_MANIFEST_HASH_SHA256 = "97e8d1770a1d78010566760ac3d4121b8b6eafd8f13b617782286cbfaab31c4b"
GOVERNANCE_HASH_SHA256 = "f73f0897c15666daf3c6ff09b2ca4ec9737f412f9cf462bc2c3db8216bd0ef69"
GOVERNANCE_AMENDMENT_HASH_SHA256 = (
    "5d4a9077dec6cca6ed590694fb34d5c6804295654656fe1c752294378dc64517"
)
ECONOMIC_SPEC_HASH_V2_SHA256 = (
    "1dc381ce5e8c663b1e1aab496503837945547d77944d082f873eab8f127eb395"
)
ECONOMIC_GOVERNANCE_AMENDMENT_HASH_SHA256 = (
    "0b85b8dc4b90e304dfb7ac900d25dcdeca8adf06e4f96ce55a4798eea94bd424"
)


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


def economic_spec_v2(config: PaperConfig) -> dict[str, Any]:
    """Return the comprehensive, deterministic forward economic specification."""
    strategy = config.strategy_config
    return {
        "version": "economic-spec-v2",
        "candidate_id": config.locked_candidate_id,
        "universe": sorted(config.assets),
        "capital": {
            "initial_cash": config.initial_cash,
            "accounting_currency": config.accounting_currency,
        },
        "signal": {
            "momentum_short_days": strategy.momentum_short_days,
            "momentum_long_days": strategy.momentum_long_days,
            "momentum_skip_days": strategy.momentum_skip_days,
            "btc_moving_average_days": strategy.btc_moving_average_days,
            "volatility_days": strategy.volatility_days,
            "annualization_days": strategy.annualization_days,
        },
        "allocation": {
            "max_assets": strategy.max_assets,
            "asset_caps": dict(sorted(strategy.asset_caps.items())),
            "altcoins": sorted(strategy.altcoins),
            "max_altcoin_weight": strategy.max_altcoin_weight,
            "rebalance_days": config.rebalance_days,
        },
        "execution": {
            "protocol_version": EXECUTION_PROTOCOL_VERSION,
            "fee_rate": config.fee_rate,
            "minimum_spread_rate": config.minimum_spread_rate,
            "slippage_rate": config.slippage_rate,
            "require_exchange_rules": config.require_exchange_rules,
            "quantity_tolerance": config.quantity_tolerance,
        },
        "market_data": {
            "exchange_id": config.exchange_id,
            "lookback_days": config.lookback_days,
            "max_data_staleness_minutes": config.max_data_staleness_minutes,
            "max_quote_staleness_minutes": config.max_quote_staleness_minutes,
            "max_abs_daily_return": config.max_abs_daily_return,
            "max_volume_ratio": config.max_volume_ratio,
        },
        "schedule": {
            "schedule_weekday": config.schedule_weekday,
            "schedule_hour": config.schedule_hour,
            "schedule_minute": config.schedule_minute,
            "execution_target_minute": config.execution_target_minute,
            "schedule_window_minutes": config.schedule_window_minutes,
            "timezone": "UTC",
        },
        "constraints": {
            "long_only": True,
            "leverage": False,
            "private_exchange_api": False,
            "live_orders": False,
            "llm_signals_or_weights": False,
        },
    }


def economic_spec_hash_v2(config: PaperConfig) -> str:
    canonical = json.dumps(
        economic_spec_v2(config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verify_economic_spec_v2(config: PaperConfig) -> str:
    actual = economic_spec_hash_v2(config)
    if actual != ECONOMIC_SPEC_HASH_V2_SHA256:
        raise ValueError(
            "Comprehensive economic specification mismatch: "
            f"expected={ECONOMIC_SPEC_HASH_V2_SHA256}, actual={actual}"
        )
    return actual


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
    amendment = project_root / "forward_experiment" / "governance_amendment_v2.json"
    economic_amendment = (
        project_root
        / "forward_experiment"
        / "governance_amendment_v3_economic_spec.json"
    )
    actual_checkpoint = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    actual_governance = hashlib.sha256(governance.read_bytes()).hexdigest()
    actual_locked = locked_strategy_hash(config)
    actual_amendment = hashlib.sha256(amendment.read_bytes()).hexdigest()
    actual_economic_spec = verify_economic_spec_v2(config)
    actual_economic_amendment = hashlib.sha256(
        economic_amendment.read_bytes()
    ).hexdigest()
    expected = {
        "checkpoint": CHECKPOINT_MANIFEST_HASH_SHA256,
        "governance": GOVERNANCE_HASH_SHA256,
        "locked_strategy": LOCKED_STRATEGY_HASH_SHA256,
        "governance_amendment": GOVERNANCE_AMENDMENT_HASH_SHA256,
        "economic_spec_v2": ECONOMIC_SPEC_HASH_V2_SHA256,
        "economic_governance_amendment": ECONOMIC_GOVERNANCE_AMENDMENT_HASH_SHA256,
    }
    actual = {
        "checkpoint": actual_checkpoint,
        "governance": actual_governance,
        "locked_strategy": actual_locked,
        "governance_amendment": actual_amendment,
        "economic_spec_v2": actual_economic_spec,
        "economic_governance_amendment": actual_economic_amendment,
    }
    if actual != expected:
        raise ValueError(f"Forward trust-anchor mismatch: expected={expected}, actual={actual}")
    verify_immutable_manifest(
        checkpoint, project_root / "forward_experiment" / "checkpoint_manifest.sha256"
    )
    verify_immutable_manifest(
        governance, project_root / "forward_experiment" / "governance.json.sha256"
    )
    verify_immutable_manifest(
        amendment,
        project_root / "forward_experiment" / "governance_amendment_v2.json.sha256",
    )
    verify_immutable_manifest(
        economic_amendment,
        project_root
        / "forward_experiment"
        / "governance_amendment_v3_economic_spec.json.sha256",
    )
    amendment_payload = json.loads(amendment.read_text(encoding="utf-8"))
    if amendment_payload["base_governance_sha256"] != actual_governance:
        raise ValueError("Operational governance amendment does not anchor the sealed governance")
    if amendment_payload["locked_strategy_sha256"] != actual_locked:
        raise ValueError("Operational governance amendment does not anchor the locked strategy")
    if amendment_payload["execution_protocol_version"] != EXECUTION_PROTOCOL_VERSION:
        raise ValueError("Execution protocol differs from the governance amendment")
    economic_payload = json.loads(economic_amendment.read_text(encoding="utf-8"))
    if economic_payload["base_governance_sha256"] != actual_governance:
        raise ValueError("Economic integrity amendment does not anchor sealed governance")
    if economic_payload["prior_operational_amendment_sha256"] != actual_amendment:
        raise ValueError("Economic integrity amendment does not anchor the prior amendment")
    if economic_payload["legacy_locked_strategy_sha256"] != actual_locked:
        raise ValueError("Economic integrity amendment does not anchor the legacy strategy")
    if economic_payload["comprehensive_economic_spec_sha256"] != actual_economic_spec:
        raise ValueError("Economic integrity amendment does not anchor economic spec v2")
    if any(
        economic_payload[field]
        for field in (
            "strategy_reselection",
            "parameter_retuning",
            "research_results_changed",
        )
    ):
        raise ValueError("Economic integrity amendment declares a prohibited research change")
    declared_schedule = amendment_payload["operational_schedule"]
    actual_schedule = {
        "schedule_weekday": config.schedule_weekday,
        "schedule_hour": config.schedule_hour,
        "schedule_minute": config.schedule_minute,
        "execution_target_minute": config.execution_target_minute,
        "schedule_window_minutes": config.schedule_window_minutes,
        "timezone": "UTC",
    }
    if declared_schedule != actual_schedule:
        raise ValueError(
            "Forward operational schedule differs from the code-anchored governance amendment: "
            f"expected={declared_schedule}, actual={actual_schedule}"
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
