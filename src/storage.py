from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def save_raw_json(rows: list[list[float]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    return path


def save_clean_parquet(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False, engine="pyarrow")
    return path
