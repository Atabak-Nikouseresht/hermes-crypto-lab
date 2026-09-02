"""Fail-closed canonical research dataset loading and provenance."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.validate_data import COLUMNS, validate_ohlcv


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(asset: str) -> str:
    return asset.replace("/", "_").replace(":", "_")


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
            if _sha256(candidate) != entry["sha256"]:
                raise ValueError(f"Canonical dataset hash mismatch for {asset}")
            paths[asset] = candidate
        return paths, manifest, manifest_path
    raise ValueError("canonical dataset manifest is required")


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
        frame = pd.read_parquet(path, columns=COLUMNS, filters=filters)
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
        frame = pd.read_parquet(path, columns=["timestamp"])
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
