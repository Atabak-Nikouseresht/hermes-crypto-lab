import json
import hashlib
from pathlib import Path

import pandas as pd
import pytest

from src.research_data import load_canonical_close_prices


def _write_asset(path: Path, dates: list[str], closes: list[float]) -> None:
    close = pd.Series(closes, dtype=float)
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(dates, utc=True),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 100.0,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _write_manifest(processed: Path) -> None:
    datasets = {}
    for path in processed.glob("*_1d.parquet"):
        symbol = path.stem.removesuffix("_1d").replace("_", "/")
        datasets[symbol] = {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    payload = {
        "run_id": "fixture",
        "timeframe": "1d",
        "version_manifest_path": "fixture/dataset_manifest.json",
        "datasets": datasets,
    }
    immutable = processed / "fixture" / "dataset_manifest.json"
    immutable.parent.mkdir(parents=True, exist_ok=True)
    immutable.write_text(json.dumps(payload), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_canonical_loader_rejects_compressed_misaligned_calendars(tmp_path):
    processed = tmp_path / "processed"
    _write_asset(
        processed / "BTC_USDT_1d.parquet",
        ["2024-01-01", "2024-01-02", "2024-01-03"],
        [10.0, 11.0, 12.0],
    )
    _write_asset(
        processed / "ETH_USDT_1d.parquet",
        ["2024-01-01", "2024-01-03"],
        [20.0, 22.0],
    )
    _write_manifest(processed)

    with pytest.raises(ValueError, match="missing daily candles|calendar mismatch"):
        load_canonical_close_prices(processed, ["BTC/USDT", "ETH/USDT"], "1d")


def test_canonical_loader_rejects_duplicate_or_invalid_ohlcv(tmp_path):
    processed = tmp_path / "processed"
    _write_asset(
        processed / "BTC_USDT_1d.parquet",
        ["2024-01-01", "2024-01-01"],
        [10.0, -1.0],
    )
    _write_manifest(processed)

    with pytest.raises(ValueError, match="canonical OHLCV validation failed"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_enforces_daily_timeframe(tmp_path):
    with pytest.raises(ValueError, match="1d"):
        load_canonical_close_prices(tmp_path, ["BTC/USDT"], "4h")


def test_canonical_loader_requires_immutable_manifest_provenance(tmp_path):
    processed = tmp_path / "processed"
    _write_asset(
        processed / "BTC_USDT_1d.parquet",
        ["2024-01-01", "2024-01-02"],
        [10.0, 11.0],
    )

    with pytest.raises(ValueError, match="dataset manifest"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_uses_versioned_manifest_and_returns_provenance(tmp_path):
    processed = tmp_path / "processed"
    versioned = processed / "run-1" / "BTC_USDT_1d.parquet"
    _write_asset(versioned, ["2024-01-01", "2024-01-02"], [10.0, 11.0])
    manifest = {
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": hashlib.sha256(versioned.read_bytes()).hexdigest(),
            }
        },
    }
    (processed / "run-1" / "dataset_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (processed / "dataset_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    prices, provenance = load_canonical_close_prices(
        processed, ["BTC/USDT"], "1d", include_provenance=True
    )

    assert list(prices.columns) == ["BTC/USDT"]
    assert provenance["run_id"] == "run-1"
    assert provenance["manifest_sha256"]
    assert provenance["datasets"]["BTC/USDT"]["sha256"]


def test_canonical_loader_rejects_dataset_manifest_hash_mismatch(tmp_path):
    processed = tmp_path / "processed"
    versioned = processed / "run-1" / "BTC_USDT_1d.parquet"
    _write_asset(versioned, ["2024-01-01", "2024-01-02"], [10.0, 11.0])
    manifest = {
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {"path": "run-1/BTC_USDT_1d.parquet", "sha256": "0" * 64}
        },
    }
    (processed / "run-1" / "dataset_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_rejects_mutable_manifest_without_version_pointer(tmp_path):
    processed = tmp_path / "processed"
    asset = processed / "BTC_USDT_1d.parquet"
    _write_asset(asset, ["2024-01-01"], [10.0])
    (processed / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "mutable",
                "timeframe": "1d",
                "datasets": {
                    "BTC/USDT": {
                        "path": asset.name,
                        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="immutable version manifest"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_rejects_self_referential_manifest_pointer(tmp_path):
    processed = tmp_path / "processed"
    asset = processed / "BTC_USDT_1d.parquet"
    _write_asset(asset, ["2024-01-01"], [10.0])
    manifest = {
        "run_id": "mutable",
        "timeframe": "1d",
        "version_manifest_path": "dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {
                "path": asset.name,
                "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
            }
        },
    }
    (processed / "dataset_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="immutable version manifest"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")
