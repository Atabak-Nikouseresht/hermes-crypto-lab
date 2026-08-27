"""Tamper-evident raw/Parquet integrity manifests with semantic checks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.validate_data import rows_to_frame, validate_ohlcv


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    volume_ratio = frame["volume"].replace(0, float("nan")).pct_change().abs()
    return {
        "rows": len(frame),
        "first_candle_open_utc": frame["timestamp"].iloc[0].isoformat(),
        "first_candle_close_utc": (frame["timestamp"].iloc[0] + pd.Timedelta(days=1)).isoformat(),
        "last_candle_open_utc": frame["timestamp"].iloc[-1].isoformat(),
        "last_candle_close_utc": (frame["timestamp"].iloc[-1] + pd.Timedelta(days=1)).isoformat(),
        "extreme_price_change_count": int((returns > 0.75).sum()),
        "extreme_volume_change_count": int((volume_ratio > 100).sum()),
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


def build_data_integrity_manifest(
    *, files: list[Path], output_path: Path, metadata: dict[str, Any]
) -> dict[str, Any]:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(output_path)
    records = []
    for source in sorted(Path(path).resolve() for path in files):
        if not source.is_file():
            raise FileNotFoundError(source)
        records.append(
            {
                "path": str(source),
                "sha256": file_sha256(source),
                "semantic": _semantic_check(source),
            }
        )
    payload = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "files": records,
        "forward_fill_used": False,
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def verify_data_integrity_manifest(manifest_path: Path) -> dict[str, Any]:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    for record in payload["files"]:
        path = Path(record["path"])
        if not path.is_file() or file_sha256(path) != record["sha256"]:
            raise ValueError(f"data hash mismatch: {path}")
        _semantic_check(path)
    return {"valid": True, "files": len(payload["files"]), "metadata": payload["metadata"]}
