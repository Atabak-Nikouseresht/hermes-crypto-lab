"""Fail-closed canonical research dataset loading and provenance."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

import pandas as pd

from src.validate_data import COLUMNS, validate_ohlcv


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(asset: str) -> str:
    return asset.replace("/", "_").replace(":", "_")


def _validate_schema_v2(manifest: dict[str, Any], processed_dir: Path) -> None:
    source = manifest.get("source")
    if (
        not isinstance(manifest.get("run_id"), str)
        or not manifest["run_id"]
        or not isinstance(source, dict)
        or source.get("exchange_id") != "binance"
        or not all(
            isinstance(source.get(field), str) and source[field]
            for field in ("exchange_id", "ccxt_version", "since")
        )
    ):
        raise ValueError("Canonical schema-v2 manifest has invalid required provenance")
    commit = manifest.get("ingestion_git_commit")
    dirty = manifest.get("git_dirty")
    if commit == "unavailable":
        raise ValueError("Canonical schema-v2 unavailable Git provenance")
    if (
        not isinstance(commit, str)
        or _GIT_COMMIT_RE.fullmatch(commit) is None
        or dirty is not False
    ):
        raise ValueError("Canonical schema-v2 manifest has invalid Git provenance")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict) or not datasets:
        raise ValueError("Canonical schema-v2 manifest has invalid datasets")
    common_start_value = manifest.get("canonical_common_start_utc")
    common_end_value = manifest.get("canonical_common_end_utc")
    if (common_start_value is None) != (common_end_value is None):
        raise ValueError("Canonical schema-v2 manifest has incomplete common calendar")
    common_start = common_end = None
    if common_start_value is not None:
        if not isinstance(common_start_value, str) or not isinstance(common_end_value, str):
            raise ValueError("Canonical schema-v2 manifest has invalid common calendar")
        try:
            common_start = pd.Timestamp(common_start_value)
            common_end = pd.Timestamp(common_end_value)
        except (TypeError, ValueError) as error:
            raise ValueError("Canonical schema-v2 manifest has invalid common calendar") from error
        if common_start.tzinfo is None or common_end.tzinfo is None or common_start > common_end:
            raise ValueError("Canonical schema-v2 manifest has invalid common calendar")
    data_root = processed_dir.parent.resolve()
    for asset, entry in datasets.items():
        if not isinstance(asset, str) or not isinstance(entry, dict):
            raise ValueError("Canonical schema-v2 manifest has invalid dataset entry")
        for field in ("path", "raw_path", "start_utc", "end_utc"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise ValueError(f"Canonical schema-v2 manifest is missing {field} for {asset}")
        for field in ("sha256", "raw_sha256"):
            if not isinstance(entry.get(field), str) or _SHA256_RE.fullmatch(entry[field]) is None:
                raise ValueError(f"Canonical schema-v2 manifest has invalid {field} for {asset}")
        if (
            not isinstance(entry.get("rows"), int)
            or not isinstance(entry.get("raw_rows"), int)
            or entry["rows"] <= 0
            or entry["raw_rows"] < entry["rows"]
        ):
            raise ValueError(f"Canonical schema-v2 manifest has invalid row counts for {asset}")
        try:
            start = pd.Timestamp(entry["start_utc"])
            end = pd.Timestamp(entry["end_utc"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"Canonical schema-v2 manifest has invalid UTC bounds for {asset}") from error
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError(f"Canonical schema-v2 manifest has invalid UTC bounds for {asset}")
        if common_start is not None and (start != common_start or end != common_end):
            raise ValueError(f"Canonical schema-v2 manifest common-calendar mismatch for {asset}")
        for root, relative, label in (
            (processed_dir.resolve(), entry["path"], "dataset"),
            (data_root, entry["raw_path"], "raw evidence"),
        ):
            try:
                (root / relative).resolve().relative_to(root)
            except ValueError as error:
                raise ValueError(f"Canonical schema-v2 {label} path escapes data directory") from error


def _validate_dataset_semantics(
    asset: str, entry: dict[str, Any], parquet_path: Path, raw_path: Path
) -> None:
    try:
        frame = pd.read_parquet(parquet_path, columns=["timestamp"])
    except Exception as error:
        raise ValueError(f"Canonical Parquet is unreadable for {asset}") from error
    if len(frame) != entry["rows"]:
        raise ValueError(
            f"Canonical manifest row-count mismatch for {asset}: "
            f"declared={entry['rows']} actual={len(frame)}"
        )
    if frame.empty:
        raise ValueError(f"Canonical manifest dataset is empty for {asset}")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    declared_start = pd.Timestamp(entry["start_utc"]).tz_convert("UTC")
    declared_end = pd.Timestamp(entry["end_utc"]).tz_convert("UTC")
    actual_start = timestamps.min()
    actual_end = timestamps.max()
    if declared_start != actual_start:
        raise ValueError(
            f"Canonical manifest start timestamp mismatch for {asset}: "
            f"declared={declared_start.isoformat()} actual={actual_start.isoformat()}"
        )
    if declared_end != actual_end:
        raise ValueError(
            f"Canonical manifest end timestamp mismatch for {asset}: "
            f"declared={declared_end.isoformat()} actual={actual_end.isoformat()}"
        )
    try:
        raw_rows = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Canonical raw artifact is unreadable for {asset}") from error
    if not isinstance(raw_rows, list) or not all(isinstance(row, list) for row in raw_rows):
        raise ValueError(f"Canonical raw artifact is not a row list for {asset}")
    if len(raw_rows) != entry["raw_rows"]:
        raise ValueError(
            f"Canonical manifest raw row-count mismatch for {asset}: "
            f"declared={entry['raw_rows']} actual={len(raw_rows)}"
        )


def _paths_and_manifest(
    processed_dir: Path, assets: list[str], timeframe: str
) -> tuple[dict[str, Path], dict[str, Any], Path | None]:
    manifest_path = processed_dir / "dataset_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version_manifest = manifest.get("version_manifest_path")
        if not isinstance(version_manifest, str) or not version_manifest:
            raise ValueError("canonical dataset pointer requires an immutable version manifest")
        candidate_manifest = (processed_dir / version_manifest).resolve()
        try:
            candidate_manifest.relative_to(processed_dir.resolve())
        except ValueError as error:
            raise ValueError("Canonical manifest path escapes processed directory") from error
        if (
            candidate_manifest == manifest_path.resolve()
            or candidate_manifest.parent == processed_dir.resolve()
            or not candidate_manifest.is_file()
        ):
            raise ValueError("canonical dataset pointer requires an immutable version manifest")
        immutable_payload = json.loads(candidate_manifest.read_text(encoding="utf-8"))
        if immutable_payload != manifest:
            raise ValueError("Canonical dataset pointer and immutable manifest mismatch")
        manifest_path = candidate_manifest
        if manifest.get("timeframe") != timeframe:
            raise ValueError("Canonical dataset manifest timeframe mismatch")
        schema_version = manifest.get("manifest_schema_version")
        if schema_version is not None and (
            type(schema_version) is not int or schema_version != 2
        ):
            raise ValueError(f"Unsupported canonical dataset manifest schema version: {schema_version}")
        provenance_v2 = type(schema_version) is int and schema_version == 2
        if provenance_v2:
            _validate_schema_v2(manifest, processed_dir)
        datasets = manifest.get("datasets")
        if not isinstance(datasets, dict):
            raise ValueError("Canonical dataset manifest is invalid")

        paths: dict[str, Path] = {}
        for asset in assets:
            entry = datasets.get(asset)
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("sha256"), str)
            ):
                raise ValueError(f"Canonical dataset manifest is missing {asset}")
            candidate = (processed_dir / entry["path"]).resolve()
            try:
                candidate.relative_to(processed_dir.resolve())
            except ValueError as error:
                raise ValueError("Canonical dataset path escapes processed directory") from error
            if not candidate.is_file():
                raise ValueError(f"Canonical referenced dataset is missing for {asset}")
            if _sha256(candidate) != entry["sha256"]:
                raise ValueError(f"Canonical dataset hash mismatch for {asset}")
            if provenance_v2:
                raw_path = entry.get("raw_path")
                raw_sha256 = entry.get("raw_sha256")
                if not isinstance(raw_path, str) or not isinstance(raw_sha256, str):
                    raise ValueError(f"Canonical raw provenance is missing for {asset}")
                raw_candidate = (processed_dir.parent / raw_path).resolve()
                try:
                    raw_candidate.relative_to(processed_dir.parent.resolve())
                except ValueError as error:
                    raise ValueError("Canonical raw evidence path escapes data directory") from error
                if not raw_candidate.is_file() or _sha256(raw_candidate) != raw_sha256:
                    raise ValueError(f"Canonical raw evidence hash mismatch for {asset}")
                _validate_dataset_semantics(asset, entry, candidate, raw_candidate)
            paths[asset] = candidate
        return paths, manifest, manifest_path
    raise ValueError("canonical dataset manifest is required")


def resolve_canonical_dataset(
    processed_dir: Path, timeframe: str = "1d", expected_assets: list[str] | None = None
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Resolve immutable artifacts; expected assets use exact, order-free set equality."""
    processed_dir = processed_dir.resolve()
    if expected_assets is not None and (
        not isinstance(expected_assets, list)
        or not expected_assets
        or not all(
            isinstance(asset, str) and re.fullmatch(r"[A-Z0-9]+/[A-Z0-9]+", asset)
            for asset in expected_assets
        )
        or len(set(expected_assets)) != len(expected_assets)
    ):
        raise ValueError("Invalid governed assets: require a nonempty unique list of BASE/QUOTE symbols")
    if timeframe != "1d":
        raise ValueError("Canonical research requires the 1d timeframe")
    pointer = processed_dir / "dataset_manifest.json"
    try:
        pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("canonical dataset manifest is required") from error
    datasets = pointer_payload.get("datasets") if isinstance(pointer_payload, dict) else None
    if not isinstance(datasets, dict) or not datasets:
        raise ValueError("Canonical dataset manifest is invalid")
    assets = sorted(datasets)
    if not all(isinstance(asset, str) and asset for asset in assets):
        raise ValueError("Canonical dataset manifest has invalid dataset symbol")
    # Governance specifies a universe, not an insertion/serialization order.
    if expected_assets is not None and set(datasets) != set(expected_assets):
        raise ValueError("Canonical dataset manifest assets differ from governed assets")
    paths, manifest, immutable_path = _paths_and_manifest(processed_dir, assets, timeframe)
    # Publication may advance between pointer reads; govern the resolved version too.
    if expected_assets is not None and set(manifest["datasets"]) != set(expected_assets):
        raise ValueError("Canonical dataset manifest assets differ from governed assets")
    if immutable_path is None:
        raise ValueError("canonical dataset pointer requires an immutable version manifest")
    return paths, {
        "run_id": manifest.get("run_id", "unknown"),
        "timeframe": timeframe,
        "immutable_manifest_path": immutable_path.relative_to(processed_dir).as_posix(),
        "immutable_manifest_sha256": _sha256(immutable_path),
        "datasets": {
            asset: {
                "path": paths[asset].relative_to(processed_dir).as_posix(),
                "sha256": _sha256(paths[asset]),
            }
            for asset in assets
        },
    }


def load_canonical_close_prices(
    processed_dir: Path,
    assets: list[str],
    timeframe: str,
    *,
    end: pd.Timestamp | None = None,
    include_provenance: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, dict[str, Any]]:
    """Load exact aligned daily bars without imputation or inner-join compression."""
    if timeframe != "1d":
        raise ValueError("Canonical research requires the 1d timeframe")
    paths, manifest, manifest_path = _paths_and_manifest(processed_dir, assets, timeframe)
    expected_index: pd.DatetimeIndex | None = None
    series: list[pd.Series] = []
    dataset_provenance: dict[str, dict[str, Any]] = {}
    end_utc = pd.Timestamp(end).tz_convert("UTC") if end is not None else None
    for asset in assets:
        path = paths[asset]
        filters = (
            [("timestamp", "<=", end_utc.to_pydatetime())]
            if end_utc is not None
            else None
        )
        try:
            frame = pd.read_parquet(path, columns=COLUMNS, filters=filters)
        except Exception as error:
            raise ValueError(f"Canonical Parquet is unreadable for {asset}") from error
        quality = validate_ohlcv(frame)
        numeric = frame[COLUMNS[1:]].apply(pd.to_numeric, errors="coerce")
        finite = numeric.map(lambda value: math.isfinite(float(value))).all(axis=None)
        if not quality.is_valid or not finite or numeric["volume"].lt(0).any():
            reason = (
                "missing daily candles"
                if quality.summary["missing_dates"]
                else "canonical OHLCV validation failed"
            )
            raise ValueError(f"{reason} for {asset}: {quality.summary}")
        index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True))
        if not index.is_monotonic_increasing or index.has_duplicates:
            raise ValueError(f"Canonical OHLCV validation failed for {asset}: timestamp order")
        if expected_index is None:
            expected_index = index
        elif not index.equals(expected_index):
            raise ValueError(f"Canonical asset calendar mismatch for {asset}")
        series.append(pd.Series(numeric["close"].to_numpy(), index=index, name=asset))
        dataset_provenance[asset] = {
            "path": path.relative_to(processed_dir).as_posix(),
            "sha256": _sha256(path),
            "rows": len(frame),
            "start_utc": index.min().isoformat(),
            "end_utc": index.max().isoformat(),
        }
    if expected_index is None or expected_index.empty:
        raise ValueError("No canonical close-price history across configured assets")
    prices = pd.concat(series, axis=1).sort_index()
    provenance = {
        "run_id": manifest.get("run_id", "unknown"),
        "timeframe": timeframe,
        "manifest_path": manifest_path.name if manifest_path else None,
        "manifest_sha256": _sha256(manifest_path) if manifest_path else None,
        "datasets": dataset_provenance,
    }
    return (prices, provenance) if include_provenance else prices


def load_canonical_timestamp_index(
    processed_dir: Path, assets: list[str], timeframe: str
) -> tuple[pd.DatetimeIndex, dict[str, Any]]:
    """Validate calendar coherence without opening final-test price values."""
    if timeframe != "1d":
        raise ValueError("Canonical research requires the 1d timeframe")
    paths, manifest, manifest_path = _paths_and_manifest(processed_dir, assets, timeframe)
    expected: pd.DatetimeIndex | None = None
    datasets: dict[str, dict[str, Any]] = {}
    for asset in assets:
        path = paths[asset]
        try:
            frame = pd.read_parquet(path, columns=["timestamp"])
        except Exception as error:
            raise ValueError(f"Canonical Parquet is unreadable for {asset}") from error
        index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True))
        if not index.is_monotonic_increasing or index.has_duplicates:
            raise ValueError(f"Canonical timestamp validation failed for {asset}")
        expected_daily = pd.date_range(index.min(), index.max(), freq="D", tz="UTC")
        if not index.equals(expected_daily):
            raise ValueError(f"Canonical data has missing daily candles for {asset}")
        if expected is None:
            expected = index
        elif not index.equals(expected):
            raise ValueError(f"Canonical asset calendar mismatch for {asset}")
        datasets[asset] = {
            "path": path.relative_to(processed_dir).as_posix(),
            "sha256": _sha256(path),
        }
    if expected is None or expected.empty:
        raise ValueError("No canonical timestamp history across configured assets")
    return expected, {
        "run_id": manifest.get("run_id", "unknown"),
        "timeframe": timeframe,
        "manifest_path": manifest_path.name if manifest_path else None,
        "manifest_sha256": _sha256(manifest_path) if manifest_path else None,
        "datasets": datasets,
    }
