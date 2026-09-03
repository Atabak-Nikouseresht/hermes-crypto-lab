import json
import hashlib
from pathlib import Path

import pandas as pd
import pytest

from src.research_data import _validate_schema_v2, load_canonical_close_prices
from src.research_data import load_canonical_timestamp_index


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


@pytest.mark.parametrize("version", [3, 2.0, "2", True])
def test_canonical_loader_rejects_unknown_manifest_schema_version(tmp_path, version):
    processed = tmp_path / "processed"
    _write_asset(processed / "BTC_USDT_1d.parquet", ["2024-01-01"], [10.0])
    _write_manifest(processed)
    for path in (processed / "dataset_manifest.json", processed / "fixture" / "dataset_manifest.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["manifest_schema_version"] = version
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported canonical dataset manifest schema version"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


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


def test_provenance_v2_rejects_tampered_raw_evidence(tmp_path):
    data = tmp_path / "data"
    processed = data / "processed"
    raw = data / "raw" / "run-1" / "BTC_USDT_1d.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("[[1704067200000,10,11,9,10,100]]", encoding="utf-8")
    parquet = processed / "run-1" / "BTC_USDT_1d.parquet"
    _write_asset(parquet, ["2024-01-01"], [10.0])
    manifest = {
        "manifest_schema_version": 2,
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "source": {"exchange_id": "binance", "ccxt_version": "test", "since": "2024-01-01T00:00:00Z"},
        "ingestion_git_commit": "a" * 40,
        "git_dirty": False,
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
                "raw_path": "raw/run-1/BTC_USDT_1d.json",
                "raw_sha256": "0" * 64,
                "rows": 1,
                "raw_rows": 1,
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2024-01-01T00:00:00Z",
            }
        },
    }
    immutable = processed / "run-1" / "dataset_manifest.json"
    immutable.write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="raw evidence hash mismatch"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda manifest: manifest.update(version_manifest_path="missing/dataset_manifest.json"), "immutable version manifest"),
        (lambda manifest: manifest["datasets"]["BTC/USDT"].update(path="../escape.parquet"), "path escapes"),
        (lambda manifest: manifest["datasets"].pop("BTC/USDT"), "missing BTC/USDT"),
        (lambda manifest: manifest.update(timeframe="4h"), "timeframe mismatch"),
    ],
)
def test_canonical_loader_rejects_manifest_boundary_mutations(tmp_path, mutate, message):
    processed = tmp_path / "processed"
    parquet = processed / "run-1" / "BTC_USDT_1d.parquet"
    _write_asset(parquet, ["2024-01-01"], [10.0])
    manifest = {
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
            }
        },
    }
    mutate(manifest)
    immutable = processed / "run-1" / "dataset_manifest.json"
    immutable.write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_rejects_malformed_manifest_and_unreadable_parquet(tmp_path):
    processed = tmp_path / "processed"
    (processed / "dataset_manifest.json").parent.mkdir(parents=True)
    (processed / "dataset_manifest.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")

    parquet = processed / "run-1" / "BTC_USDT_1d.parquet"
    parquet.parent.mkdir(parents=True, exist_ok=True)
    parquet.write_bytes(b"not parquet")
    manifest = {
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "datasets": {"BTC/USDT": {"path": "run-1/BTC_USDT_1d.parquet", "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest()}},
    }
    (processed / "run-1" / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Parquet is unreadable"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_timestamp_loader_rejects_empty_end_filter_and_negative_volume(tmp_path):
    processed = tmp_path / "processed"
    _write_asset(processed / "BTC_USDT_1d.parquet", ["2024-01-01"], [10.0])
    _write_manifest(processed)
    with pytest.raises(ValueError, match="empty_dataset"):
        load_canonical_close_prices(
            processed, ["BTC/USDT"], "1d", end=pd.Timestamp("2023-12-31T00:00:00Z")
        )
    index, _provenance = load_canonical_timestamp_index(processed, ["BTC/USDT"], "1d")
    assert index[0] == pd.Timestamp("2024-01-01T00:00:00Z")

    frame = pd.read_parquet(processed / "BTC_USDT_1d.parquet")
    frame.loc[0, "volume"] = -1.0
    frame.to_parquet(processed / "BTC_USDT_1d.parquet", index=False)
    _write_manifest(processed)
    with pytest.raises(ValueError, match="canonical OHLCV validation failed"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_timestamp_index_loader_rejects_non_daily_and_calendar_mismatch(tmp_path):
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

    with pytest.raises(ValueError, match="1d timeframe"):
        load_canonical_timestamp_index(processed, ["BTC/USDT"], "4h")
    with pytest.raises(ValueError, match="missing daily candles"):
        load_canonical_timestamp_index(processed, ["BTC/USDT", "ETH/USDT"], "1d")


def test_canonical_loader_rejects_missing_referenced_dataset(tmp_path):
    processed = tmp_path / "processed"
    manifest = {
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "datasets": {"BTC/USDT": {"path": "run-1/missing.parquet", "sha256": "0" * 64}},
    }
    immutable = processed / "run-1" / "dataset_manifest.json"
    immutable.parent.mkdir(parents=True)
    immutable.write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="referenced dataset is missing"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_rejects_pointer_mismatch_and_manifest_path_escape(tmp_path):
    processed = tmp_path / "processed"
    parquet = processed / "run-1" / "BTC_USDT_1d.parquet"
    _write_asset(parquet, ["2024-01-01"], [10.0])
    manifest = {
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
            }
        },
    }
    immutable = processed / "run-1" / "dataset_manifest.json"
    immutable.write_text(json.dumps({**manifest, "run_id": "other"}), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="pointer and immutable manifest mismatch"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")

    manifest["version_manifest_path"] = "../escape.json"
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest path escapes"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_canonical_loader_rejects_missing_schema_and_nonmonotonic_timestamps(tmp_path):
    processed = tmp_path / "processed"
    parquet = processed / "BTC_USDT_1d.parquet"
    parquet.parent.mkdir(parents=True)
    pd.DataFrame({"timestamp": [pd.Timestamp("2024-01-01T00:00:00Z")]}).to_parquet(
        parquet, index=False
    )
    _write_manifest(processed)
    with pytest.raises(ValueError, match="Parquet is unreadable"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")

    _write_asset(
        parquet,
        ["2024-01-02", "2024-01-01"],
        [11.0, 10.0],
    )
    _write_manifest(processed)
    with pytest.raises(ValueError, match="timestamp order"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


def test_provenance_v2_rejects_missing_or_escaped_raw_evidence(tmp_path):
    data = tmp_path / "data"
    processed = data / "processed"
    parquet = processed / "run-1" / "BTC_USDT_1d.parquet"
    _write_asset(parquet, ["2024-01-01"], [10.0])
    manifest = {
        "manifest_schema_version": 2,
        "run_id": "run-1",
        "timeframe": "1d",
        "version_manifest_path": "run-1/dataset_manifest.json",
        "source": {"exchange_id": "binance", "ccxt_version": "test", "since": "2024-01-01T00:00:00Z"},
        "ingestion_git_commit": "a" * 40,
        "git_dirty": False,
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
                "raw_path": "raw/run-1/missing.json",
                "raw_sha256": "0" * 64,
                "rows": 1,
                "raw_rows": 1,
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2024-01-01T00:00:00Z",
            }
        },
    }
    immutable = processed / "run-1" / "dataset_manifest.json"
    immutable.write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="raw evidence hash mismatch"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")

    manifest["datasets"]["BTC/USDT"]["raw_path"] = "../escape.json"
    immutable.write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="raw evidence path escapes"):
        load_canonical_close_prices(processed, ["BTC/USDT"], "1d")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.pop("source"), "required provenance"),
        (lambda payload: payload["source"].update(exchange_id="other"), "required provenance"),
        (lambda payload: payload["datasets"]["BTC/USDT"].update(sha256="bad"), "invalid sha256"),
        (lambda payload: payload["datasets"]["BTC/USDT"].update(rows="1"), "invalid row counts"),
        (lambda payload: payload.update(ingestion_git_commit="bad"), "invalid Git provenance"),
        (lambda payload: payload.update(ingestion_git_commit="unavailable", git_dirty=False), "unavailable Git provenance"),
        (lambda payload: payload.update(datasets={}), "invalid datasets"),
        (lambda payload: payload["datasets"]["BTC/USDT"].update(start_utc="bad"), "invalid UTC bounds"),
    ],
)
def test_schema_v2_manifest_required_provenance_is_fail_closed(tmp_path, mutate, message):
    payload = {
        "run_id": "run-1",
        "source": {"exchange_id": "binance", "ccxt_version": "test", "since": "2024-01-01T00:00:00Z"},
        "ingestion_git_commit": "a" * 40,
        "git_dirty": False,
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": "a" * 64,
                "raw_path": "raw/run-1/BTC_USDT_1d.json",
                "raw_sha256": "b" * 64,
                "rows": 1,
                "raw_rows": 1,
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2024-01-01T00:00:00Z",
            }
        },
    }
    mutate(payload)
    with pytest.raises(ValueError, match=message):
        _validate_schema_v2(payload, tmp_path / "processed")


def test_schema_v2_manifest_accepts_complete_declared_contract(tmp_path):
    payload = {
        "run_id": "run-1",
        "source": {"exchange_id": "binance", "ccxt_version": "test", "since": "2024-01-01T00:00:00Z"},
        "ingestion_git_commit": "a" * 40,
        "git_dirty": False,
        "datasets": {
            "BTC/USDT": {
                "path": "run-1/BTC_USDT_1d.parquet",
                "sha256": "a" * 64,
                "raw_path": "raw/run-1/BTC_USDT_1d.json",
                "raw_sha256": "b" * 64,
                "rows": 1,
                "raw_rows": 1,
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2024-01-01T00:00:00Z",
            }
        },
    }
    _validate_schema_v2(payload, tmp_path / "processed")
