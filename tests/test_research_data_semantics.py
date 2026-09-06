import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from src.research_data import load_canonical_close_prices


def _write_complete_v2_manifest(processed: Path) -> dict:
    parquet = processed / "run-1" / "BTC_USDT_1d.parquet"
    parquet.parent.mkdir(parents=True, exist_ok=True)
    close = pd.Series([10.0, 11.0])
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 100.0,
        }
    ).to_parquet(parquet, index=False)
    raw = processed.parent / "raw" / "run-1" / "BTC_USDT_1d.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "[[1704067200000,10,11,9,10,100],[1704153600000,11,12,10,11,100],[1704240000000,12,13,11,12,100]]",
        encoding="utf-8",
    )
    manifest = {
        "manifest_schema_version": 2,
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "source": {
            "exchange_id": "binance",
            "ccxt_version": "test",
            "since": "2024-01-01T00:00:00Z",
        },
        "ingestion_git_commit": "a" * 40,
        "git_dirty": False,
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
                "raw_path": "raw/run-1/BTC_USDT_1d.json",
                "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                "rows": 2,
                "raw_rows": 3,
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2024-01-02T00:00:00Z",
            }
        },
    }
    immutable = processed / "run-1" / "dataset_manifest.json"
    immutable.write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _write_pointer_pair(processed: Path, manifest: dict) -> None:
    for path in (
        processed / "dataset_manifest.json",
        processed / "run-1" / "dataset_manifest.json",
    ):
        path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rows", 3, "row-count mismatch"),
        ("raw_rows", 4, "raw row-count mismatch"),
        ("start_utc", "2024-01-01T02:00:00+01:00", "start timestamp mismatch"),
        ("end_utc", "2024-01-03T00:00:00Z", "end timestamp mismatch"),
    ],
)
def test_schema_v2_loader_rejects_semantic_metadata_mismatch(
    tmp_path, field, value, message
):
    processed = tmp_path / "data" / "processed"
    manifest = _write_complete_v2_manifest(processed)
    manifest["datasets"]["BTC/USDT"][field] = value
    _write_pointer_pair(processed, manifest)

    with pytest.raises(ValueError, match=message):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_schema_v2_loader_rejects_malformed_raw_and_accepts_equivalent_utc(tmp_path):
    processed = tmp_path / "data" / "processed"
    manifest = _write_complete_v2_manifest(processed)
    manifest["datasets"]["BTC/USDT"]["start_utc"] = "2023-12-31T19:00:00-05:00"
    manifest["datasets"]["BTC/USDT"]["end_utc"] = "2024-01-01T19:00:00-05:00"
    _write_pointer_pair(processed, manifest)
    assert not load_canonical_close_prices(processed, ["BTC/USDT"], "1d").empty

    raw = processed.parent / "raw" / "run-1" / "BTC_USDT_1d.json"
    raw.write_text("{}", encoding="utf-8")
    manifest["datasets"]["BTC/USDT"]["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    _write_pointer_pair(processed, manifest)
    with pytest.raises(ValueError, match="raw artifact is not a row list"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")
