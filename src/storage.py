from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import pandas as pd


def save_raw_json(rows: list[list[float]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Immutable raw dataset already exists: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)
    return path


def save_clean_parquet(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Immutable processed dataset already exists: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def save_json_atomic(payload: object, path: Path, *, immutable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        raise FileExistsError(f"Immutable JSON artifact already exists: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
