from __future__ import annotations

import json
import os
import errno
from pathlib import Path
import uuid

import pandas as pd


def _publish_immutable(temporary: Path, path: Path) -> Path:
    """Atomically publish a same-directory file without replacing a winner."""
    try:
        os.link(temporary, path)
    except OSError as error:
        if error.errno == errno.EEXIST or getattr(error, "winerror", None) == 183:
            raise FileExistsError(f"Immutable artifact already exists: {path}") from error
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return path


def save_raw_json(rows: list[list[float]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
        return _publish_immutable(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_clean_parquet(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        return _publish_immutable(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json_atomic(payload: object, path: Path, *, immutable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if immutable:
            return _publish_immutable(temporary, path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
