"""Tamper-evident raw/Parquet integrity manifests with semantic checks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import pandas as pd

from src.validate_data import rows_to_frame, validate_ohlcv


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def volume_transition_anomalies(
    volume: pd.Series, *, max_volume_ratio: float
) -> pd.Series:
    """Flag explicit zero discontinuities and large positive-to-positive moves."""
    try:
        values = pd.to_numeric(volume, errors="raise").astype(float)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid volume") from error
    if values.isna().any() or not all(math.isfinite(value) for value in values):
        raise ValueError("invalid volume")
    if (values < 0).any():
        raise ValueError("invalid volume")
    if (
        isinstance(max_volume_ratio, bool)
        or not isinstance(max_volume_ratio, (int, float))
        or not math.isfinite(max_volume_ratio)
        or max_volume_ratio <= 0
    ):
        raise ValueError("invalid max_volume_ratio")
    previous = values.shift()
    zero_transition = ((previous == 0) & (values > 0)) | ((previous > 0) & (values == 0))
    positive_ratio = (previous > 0) & (values > 0) & (
        (values / previous - 1.0).abs() > max_volume_ratio
    )
    return (zero_transition | positive_ratio).fillna(False).astype(bool)


def _validate_frame(frame: pd.DataFrame, source: Path) -> dict[str, Any]:
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    if list(frame.columns) != required:
        raise ValueError(f"invalid processed dataset columns: {source}")
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    if frame.empty or frame["timestamp"].duplicated().any() or not frame["timestamp"].is_monotonic_increasing:
        raise ValueError(f"invalid processed dataset timestamps: {source}")
    numeric = frame[required[1:]]
    if numeric.isna().any().any() or not all(
        math.isfinite(float(value)) for value in numeric.to_numpy().ravel()
    ):
        raise ValueError(f"invalid processed dataset non-finite values: {source}")
    validation = validate_ohlcv(frame)
    if not validation.is_valid or (frame["volume"] < 0).any():
        raise ValueError(f"invalid processed dataset OHLCV: {source}")
    returns = frame["close"].pct_change().abs()
    volume_anomalies = volume_transition_anomalies(
        frame["volume"], max_volume_ratio=100.0
    )
    return {
        "rows": len(frame),
        "first_candle_open_utc": frame["timestamp"].iloc[0].isoformat(),
        "first_candle_close_utc": (frame["timestamp"].iloc[0] + pd.Timedelta(days=1)).isoformat(),
        "last_candle_open_utc": frame["timestamp"].iloc[-1].isoformat(),
        "last_candle_close_utc": (frame["timestamp"].iloc[-1] + pd.Timedelta(days=1)).isoformat(),
        "extreme_price_change_count": int((returns > 0.75).sum()),
        "extreme_volume_change_count": int(volume_anomalies.sum()),
    }


def _semantic_check(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return _validate_frame(pd.read_parquet(path), path)
    if suffix == ".json":
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(f"invalid raw JSON: {path}") from error
        if not isinstance(rows, list) or not rows or any(
            not isinstance(row, list) or len(row) != 6 for row in rows
        ):
            raise ValueError(f"invalid raw response shape: {path}")
        return _validate_frame(rows_to_frame(rows), path)
    return {"bytes": path.stat().st_size}


def _validate_metadata(metadata: Any) -> None:
    def is_json_value(value: Any) -> bool:
        if value is None or isinstance(value, (str, bool, int)):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, list):
            return all(is_json_value(item) for item in value)
        if isinstance(value, dict):
            return all(isinstance(key, str) and is_json_value(item) for key, item in value.items())
        return False

    if not isinstance(metadata, dict) or not is_json_value(metadata):
        raise ValueError("invalid manifest metadata: expected a JSON object with finite JSON values")


def _relative_path(path: str) -> Path:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).is_absolute()
        or ":" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError(f"invalid manifest path: {path!r}")
    return Path(*path.split("/"))


def _validate_semantic(semantic: Any) -> None:
    if not isinstance(semantic, dict):
        raise ValueError("invalid semantic metadata")
    if set(semantic) == {"bytes"}:
        if type(semantic["bytes"]) is not int or semantic["bytes"] < 0:
            raise ValueError("invalid semantic metadata")
        return
    expected = {
        "rows", "first_candle_open_utc", "first_candle_close_utc",
        "last_candle_open_utc", "last_candle_close_utc",
        "extreme_price_change_count", "extreme_volume_change_count",
    }
    if set(semantic) != expected:
        raise ValueError("invalid semantic metadata")
    for key in ("rows", "extreme_price_change_count", "extreme_volume_change_count"):
        if type(semantic[key]) is not int or semantic[key] < 0:
            raise ValueError("invalid semantic metadata")
    for key in expected - {"rows", "extreme_price_change_count", "extreme_volume_change_count"}:
        if not isinstance(semantic[key], str):
            raise ValueError("invalid semantic metadata")


def build_data_integrity_manifest(
    *, files: list[Path], output_path: Path, metadata: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    output_path = Path(output_path)
    root = Path(project_root).resolve()
    _validate_metadata(metadata)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(output_path)
    normalized_sources = []
    for source in (Path(path).resolve() for path in files):
        if not source.is_file():
            raise FileNotFoundError(source)
        try:
            relative = source.relative_to(root)
        except ValueError as error:
            raise ValueError(f"file is outside project root: {source}") from error
        normalized_sources.append((relative.as_posix(), source))
    records = []
    for relative_path, source in sorted(normalized_sources, key=lambda item: item[0]):
        records.append(
            {
                "path": relative_path,
                "sha256": file_sha256(source),
                "semantic": _semantic_check(source),
            }
        )
    payload = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "files": records,
        "forward_fill_used": False,
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def verify_data_integrity_manifest(
    manifest_path: Path, *, project_root: Path | None = None
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("invalid manifest JSON") from error
    required = {"schema_version", "created_at_utc", "metadata", "files", "forward_fill_used"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("invalid manifest schema")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("invalid manifest schema version")
    if not isinstance(payload["created_at_utc"], str):
        raise ValueError("invalid manifest created_at_utc")
    try:
        datetime.fromisoformat(payload["created_at_utc"])
    except ValueError as error:
        raise ValueError("invalid manifest created_at_utc") from error
    _validate_metadata(payload["metadata"])
    if payload["forward_fill_used"] is not False or not isinstance(payload["files"], list):
        raise ValueError("invalid manifest schema")
    root = Path(project_root if project_root is not None else Path(manifest_path).parent).resolve()
    seen_paths: set[str] = set()
    for record in payload["files"]:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "semantic"}:
            raise ValueError("invalid manifest file record")
        relative = _relative_path(record["path"])
        normalized = relative.as_posix()
        if normalized in seen_paths:
            raise ValueError(f"duplicate manifest path: {normalized}")
        seen_paths.add(normalized)
        digest = record["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid manifest sha256")
        _validate_semantic(record["semantic"])
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"invalid manifest path outside project root: {normalized}") from error
        if not path.is_file() or file_sha256(path) != record["sha256"]:
            raise ValueError(f"data hash mismatch: {path}")
        if _semantic_check(path) != record["semantic"]:
            raise ValueError(f"data semantic mismatch: {path}")
    return {"valid": True, "files": len(payload["files"]), "metadata": payload["metadata"]}
