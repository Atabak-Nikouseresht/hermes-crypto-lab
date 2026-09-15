import json

import pandas as pd
import pytest

from src.data_integrity import (
    build_data_integrity_manifest,
    verify_data_integrity_manifest,
    volume_transition_anomalies,
)


def test_data_hash_manifest_detects_corrupted_raw_and_parquet(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([[1704067200000, 1, 2, 0.5, 1.5, 10]]), encoding="utf-8")
    parquet = tmp_path / "clean.parquet"
    pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-01T00:00:00Z")],
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "volume": [10.0],
        }
    ).to_parquet(parquet, index=False)
    manifest = tmp_path / "manifest.json"

    build_data_integrity_manifest(
        files=[raw, parquet],
        output_path=manifest,
        metadata={"exchange": "binance", "ccxt_version": "test"},
    )
    assert verify_data_integrity_manifest(manifest)["valid"] is True

    raw.write_text("corrupted", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_data_integrity_manifest(manifest)


def test_parquet_semantic_corruption_is_rejected(tmp_path):
    parquet = tmp_path / "bad.parquet"
    pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-01T00:00:00Z")],
            "open": [2.0],
            "high": [1.0],
            "low": [3.0],
            "close": [-1.0],
            "volume": [10.0],
        }
    ).to_parquet(parquet, index=False)

    with pytest.raises(ValueError, match="invalid processed dataset"):
        build_data_integrity_manifest(
            files=[parquet], output_path=tmp_path / "manifest.json", metadata={}
        )


@pytest.mark.parametrize(
    "volumes,expected",
    [
        ([0.0, 0.0], [False, False]),
        ([0.0, 0.01], [False, True]),
        ([0.0, 1_000_000.0], [False, True]),
        ([1.0, 0.0], [False, True]),
        ([100.0, 150.0], [False, False]),
        ([1.0, 102.0], [False, True]),
    ],
)
def test_volume_transition_anomalies_handle_zero_transitions_without_division_by_zero(
    volumes, expected
):
    assert volume_transition_anomalies(pd.Series(volumes), max_volume_ratio=100.0).tolist() == expected


@pytest.mark.parametrize("volumes", [[1.0, float("nan")], [1.0, float("inf")], [1.0, -1.0]])
def test_volume_transition_anomalies_fail_closed_for_invalid_volume(volumes):
    with pytest.raises(ValueError, match="volume"):
        volume_transition_anomalies(pd.Series(volumes), max_volume_ratio=100.0)
