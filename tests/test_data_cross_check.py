import hashlib
import json

import pandas as pd
import pytest

from scripts import data_cross_check


def _write_canonical_dataset(processed, run_id, asset, close):
    path = processed / run_id / f"{asset.replace('/', '_')}_1d.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-05T00:00:00Z")],
            "open": [close],
            "high": [close + 1],
            "low": [close - 1],
            "close": [close],
            "volume": [100.0],
        }
    ).to_parquet(path, index=False)
    return path


@pytest.mark.parametrize("legacy_close", [999.0, None], ids=["stale-flat-present", "no-flat-files"])
def test_cross_check_resolves_active_versioned_canonical_manifest_not_legacy_file(
    tmp_path, monkeypatch, legacy_close
):
    project = tmp_path / "project"
    processed = project / "data" / "processed"
    btc = _write_canonical_dataset(processed, "active", "BTC/USDT", 101.0)
    eth = _write_canonical_dataset(processed, "active", "ETH/USDT", 202.0)
    manifest = {
        "run_id": "active",
        "timeframe": "1d",
        "version_manifest_path": "active/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {"path": "active/BTC_USDT_1d.parquet", "sha256": hashlib.sha256(btc.read_bytes()).hexdigest()},
            "ETH/USDT": {"path": "active/ETH_USDT_1d.parquet", "sha256": hashlib.sha256(eth.read_bytes()).hexdigest()},
        },
    }
    (processed / "active" / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # A stale flat file must not be read even if it exists.
    if legacy_close is not None:
        _write_canonical_dataset(processed, ".", "BTC/USDT", legacy_close)
    monkeypatch.setattr(data_cross_check, "SAMPLES", ("2024-01-05",))
    monkeypatch.setattr(data_cross_check, "_vision_close", lambda symbol, _day: {"BTCUSDT": 101.0, "ETHUSDT": 202.0}[symbol])

    def fake_get(url):
        if "coingecko" in url:
            return json.dumps({"prices": [[1704412800000, 101.0], [1704499200000, 101.0]]}).encode()
        return json.dumps([[1704412800000, "0", "0", "0", "101", "0"]]).encode()

    monkeypatch.setattr(data_cross_check, "_get", fake_get)
    result = data_cross_check.run(project, project / "audits" / "cross.json")

    assert result["status"] == "PASS"
    assert result["canonical_provenance"]["run_id"] == "active"
    assert result["canonical_provenance"]["immutable_manifest_path"] == "active/dataset_manifest.json"
    assert {row["processed_close"] for row in result["archive_checks"]} == {101.0, 202.0}


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "referenced dataset is missing"),
        ("pointer-mismatch", "pointer and immutable manifest mismatch"),
        ("hash", "hash mismatch"),
    ],
)
def test_cross_check_fails_closed_on_active_canonical_pointer_or_artifact_corruption(
    tmp_path, kind, message
):
    project = tmp_path / "project"
    processed = project / "data" / "processed"
    path = _write_canonical_dataset(processed, "active", "BTC/USDT", 101.0)
    manifest = {
        "run_id": "active",
        "timeframe": "1d",
        "version_manifest_path": "active/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {
                "path": "active/missing.parquet" if kind == "missing" else "active/BTC_USDT_1d.parquet",
                "sha256": "0" * 64 if kind == "hash" else hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        },
    }
    immutable = dict(manifest)
    if kind == "pointer-mismatch":
        immutable["run_id"] = "other"
    (processed / "active" / "dataset_manifest.json").write_text(json.dumps(immutable), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        data_cross_check.run(project, project / "audits" / "cross.json")
