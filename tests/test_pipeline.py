from pathlib import Path
import json

import duckdb
import pytest

import run_data_pipeline
from run_data_pipeline import run_pipeline
from src.config import Settings


def test_pipeline_creates_raw_parquet_metadata_and_report(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        exchange="binance",
        timeframe="1d",
        since="2024-01-01T00:00:00Z",
        fetch_limit=1000,
        max_retries=1,
        backoff_base_seconds=0.0,
        request_timeout_ms=1000,
        assets_config=tmp_path / "config" / "assets.yaml",
        database_path=tmp_path / "database" / "trading.duckdb",
        log_level="INFO",
    )
    rows = [
        [1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0],
        [1704153600000, 11.0, 13.0, 10.0, 12.0, 110.0],
    ]

    result = run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda _exchange, _symbol, **_kwargs: rows,
        exchange=object(),
        run_id="test-run",
    )

    assert (tmp_path / "data" / "raw" / "test-run" / "BTC_USDT_1d.json").exists()
    versioned = tmp_path / "data" / "processed" / "test-run" / "BTC_USDT_1d.parquet"
    assert versioned.exists()
    manifest = json.loads(
        (tmp_path / "data" / "processed" / "dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["datasets"]["BTC/USDT"]["path"] == "test-run/BTC_USDT_1d.parquet"
    immutable_manifest = (
        tmp_path / "data" / "processed" / "test-run" / "dataset_manifest.json"
    )
    assert immutable_manifest.exists()
    assert json.loads(immutable_manifest.read_text(encoding="utf-8")) == manifest
    assert Path(result["markdown_report"]).exists()
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        assert connection.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM dataset_metadata").fetchone()[0] == 1


def test_pipeline_rejects_non_daily_canonical_data(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        exchange="binance",
        timeframe="4h",
        since="2024-01-01T00:00:00Z",
        fetch_limit=1000,
        max_retries=1,
        backoff_base_seconds=0.1,
        request_timeout_ms=1000,
        assets_config=tmp_path / "config" / "assets.yaml",
        database_path=tmp_path / "database" / "trading.duckdb",
        log_level="INFO",
    )

    with pytest.raises(ValueError, match="1d"):
        run_pipeline(settings=settings, assets=["BTC/USDT"], exchange=object())


def test_pipeline_fails_closed_instead_of_cleaning_invalid_rows(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        exchange="binance",
        timeframe="1d",
        since="2024-01-01T00:00:00Z",
        fetch_limit=1000,
        max_retries=1,
        backoff_base_seconds=0.1,
        request_timeout_ms=1000,
        assets_config=tmp_path / "config" / "assets.yaml",
        database_path=tmp_path / "database" / "trading.duckdb",
        log_level="INFO",
    )
    invalid = [
        [1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0],
        [1704240000000, 11.0, 10.0, 12.0, 0.0, 100.0],
    ]

    with pytest.raises(ValueError, match="quality validation failed"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: invalid,
            exchange=object(),
            run_id="invalid-run",
        )
    assert not (tmp_path / "data" / "processed" / "invalid-run").exists()


def test_pipeline_preserves_prior_dataset_version_when_current_pointer_advances(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        exchange="binance",
        timeframe="1d",
        since="2024-01-01T00:00:00Z",
        fetch_limit=1000,
        max_retries=1,
        backoff_base_seconds=0.0,
        request_timeout_ms=1000,
        assets_config=tmp_path / "config" / "assets.yaml",
        database_path=tmp_path / "database" / "trading.duckdb",
        log_level="INFO",
    )
    rows = [[1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0]]
    for run_id in ("run-1", "run-2"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: rows,
            exchange=object(),
            run_id=run_id,
        )

    old_manifest = tmp_path / "data" / "processed" / "run-1" / "dataset_manifest.json"
    current = json.loads(
        (tmp_path / "data" / "processed" / "dataset_manifest.json").read_text()
    )
    assert old_manifest.exists()
    assert json.loads(old_manifest.read_text())["run_id"] == "run-1"
    assert current["run_id"] == "run-2"


def test_pipeline_does_not_advance_current_pointer_before_run_completion(tmp_path, monkeypatch):
    settings = Settings(
        project_root=tmp_path,
        exchange="binance",
        timeframe="1d",
        since="2024-01-01T00:00:00Z",
        fetch_limit=1000,
        max_retries=1,
        backoff_base_seconds=0.0,
        request_timeout_ms=1000,
        assets_config=tmp_path / "config" / "assets.yaml",
        database_path=tmp_path / "database" / "trading.duckdb",
        log_level="INFO",
    )
    rows = [[1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0]]
    run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: rows,
        exchange=object(),
        run_id="complete-run",
    )
    monkeypatch.setattr(
        run_data_pipeline,
        "write_quality_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("report failure")),
    )

    with pytest.raises(OSError, match="report failure"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: rows,
            exchange=object(),
            run_id="failed-run",
        )

    current = json.loads(
        (settings.processed_dir / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert current["run_id"] == "complete-run"
