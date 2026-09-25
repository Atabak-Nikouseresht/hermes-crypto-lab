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
        project_root=tmp_path,
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
            files=[parquet], output_path=tmp_path / "manifest.json", metadata={},
            project_root=tmp_path,
        )


def test_manifest_rejects_semantic_metadata_tampering_with_valid_hash(tmp_path):
    raw = tmp_path / "data.json"
    raw.write_text(json.dumps([[1704067200000, 1, 2, 0.5, 1.5, 10]]), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    build_data_integrity_manifest(
        files=[raw], output_path=manifest, metadata={}, project_root=tmp_path
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][0]["semantic"]["rows"] = 2
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="semantic"):
        verify_data_integrity_manifest(manifest, project_root=tmp_path)


@pytest.mark.parametrize(
    "metadata", [[], "metadata", {"bad": float("nan")}, {"nested": {1: "key"}}]
)
def test_manifest_rejects_malformed_metadata_at_build_time(tmp_path, metadata):
    raw = tmp_path / "data.bin"
    raw.write_bytes(b"data")
    with pytest.raises(ValueError, match="metadata"):
        build_data_integrity_manifest(
            files=[raw], output_path=tmp_path / "manifest.json",
            metadata=metadata, project_root=tmp_path,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", True),
        ("extreme_price_change_count", "0"),
        ("first_candle_open_utc", None),
    ],
)
def test_manifest_rejects_malformed_persisted_semantic_fields(tmp_path, field, value):
    raw = tmp_path / "data.json"
    raw.write_text(json.dumps([[1704067200000, 1, 2, 0.5, 1.5, 10]]), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    build_data_integrity_manifest(
        files=[raw], output_path=manifest, metadata={}, project_root=tmp_path
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][0]["semantic"][field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="semantic"):
        verify_data_integrity_manifest(manifest, project_root=tmp_path)


def test_manifest_uses_relative_paths_and_verifies_in_another_clone(tmp_path):
    original = tmp_path / "original"
    clone = tmp_path / "clone"
    original.mkdir()
    clone.mkdir()
    (original / "data.bin").write_bytes(b"portable")
    (clone / "data.bin").write_bytes(b"portable")
    (original / "nested").mkdir()
    (clone / "nested").mkdir()
    (original / "nested" / "more.bin").write_bytes(b"portable-too")
    (clone / "nested" / "more.bin").write_bytes(b"portable-too")
    manifest = original / "manifest.json"
    build_data_integrity_manifest(
        files=[original / "nested" / "more.bin", original / "data.bin"], output_path=manifest,
        metadata={}, project_root=original,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert [record["path"] for record in payload["files"]] == ["data.bin", "nested/more.bin"]
    assert all("\\" not in record["path"] for record in payload["files"])
    assert str(original) not in manifest.read_text(encoding="utf-8")

    clone_manifest = clone / "manifest.json"
    clone_manifest.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    assert verify_data_integrity_manifest(clone_manifest, project_root=clone)["valid"]
    rebuilt = build_data_integrity_manifest(
        files=[clone / "nested" / "more.bin", clone / "data.bin"],
        output_path=clone / "rebuilt.json", metadata={}, project_root=clone,
    )
    assert rebuilt["files"] == payload["files"]


@pytest.mark.parametrize("path", ["C:/Users/example/project/data.bin", "/home/example/project/data.bin"])
def test_manifest_rejects_absolute_windows_and_unix_paths(tmp_path, path):
    root = tmp_path / "project"
    root.mkdir()
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "created_at_utc": "2026-01-01T00:00:00+00:00",
        "metadata": {}, "files": [{"path": path, "sha256": "0" * 64,
        "semantic": {"bytes": 1}}], "forward_fill_used": False,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        verify_data_integrity_manifest(manifest, project_root=root)


def test_manifest_rejects_files_outside_project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="project root"):
        build_data_integrity_manifest(
            files=[outside], output_path=root / "manifest.json",
            metadata={}, project_root=root,
        )


def test_manifest_rejects_traversal_path_during_verification(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "created_at_utc": "2026-01-01T00:00:00+00:00",
        "metadata": {}, "files": [{"path": "../outside.bin", "sha256": "0" * 64,
        "semantic": {"bytes": 1}}], "forward_fill_used": False,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        verify_data_integrity_manifest(manifest, project_root=root)


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
