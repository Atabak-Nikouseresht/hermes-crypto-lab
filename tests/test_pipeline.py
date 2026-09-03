from pathlib import Path
import json
import subprocess
from dataclasses import replace

import duckdb
import pytest

import run_data_pipeline
from run_data_pipeline import run_pipeline
from src.config import Settings


@pytest.fixture(autouse=True)
def governed_assets(tmp_path):
    path = tmp_path / "config" / "assets.yaml"
    path.parent.mkdir()
    path.write_text("assets: [BTC/USDT]\n", encoding="utf-8")


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
        git_provenance=lambda _root: ("a" * 40, False),
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
    assert manifest["manifest_schema_version"] == 2
    assert manifest["source"]["exchange_id"] == "binance"
    assert manifest["source"]["ccxt_version"]
    assert manifest["source"]["since"] == settings.since
    assert manifest["source"]["fetch_limit"] == settings.fetch_limit
    assert manifest["ingestion_git_commit"] == "a" * 40
    assert manifest["git_dirty"] is False
    assert manifest["datasets"]["BTC/USDT"]["raw_path"] == "raw/test-run/BTC_USDT_1d.json"
    assert manifest["datasets"]["BTC/USDT"]["raw_sha256"]
    assert manifest["datasets"]["BTC/USDT"]["raw_rows"] == 2
    assert manifest["datasets"]["BTC/USDT"]["rows"] == 2
    assert manifest["datasets"]["BTC/USDT"]["start_utc"]
    assert manifest["datasets"]["BTC/USDT"]["end_utc"]
    immutable_manifest = (
        tmp_path / "data" / "processed" / "test-run" / "dataset_manifest.json"
    )
    assert immutable_manifest.exists()
    assert json.loads(immutable_manifest.read_text(encoding="utf-8")) == manifest
    assert Path(result["markdown_report"]).exists()
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        assert connection.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM dataset_metadata").fetchone()[0] == 1


def test_git_provenance_supports_clean_dirty_and_unavailable(monkeypatch, tmp_path):
    clean = iter(
        [
            subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(run_data_pipeline.subprocess, "run", lambda *_args, **_kwargs: next(clean))
    assert run_data_pipeline._git_provenance(tmp_path) == ("a" * 40, False)

    dirty = iter(
        [
            subprocess.CompletedProcess([], 0, "b" * 40 + "\n", ""),
            subprocess.CompletedProcess([], 0, " M data/file\n", ""),
        ]
    )
    monkeypatch.setattr(run_data_pipeline.subprocess, "run", lambda *_args, **_kwargs: next(dirty))
    assert run_data_pipeline._git_provenance(tmp_path) == ("b" * 40, True)

    monkeypatch.setattr(run_data_pipeline.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no git")))
    assert run_data_pipeline._git_provenance(tmp_path) == ("unavailable", None)


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


@pytest.mark.parametrize(
    ("settings_change", "assets", "message"),
    [
        ({"exchange": "other-public-exchange"}, ["BTC/USDT"], "exchange binance"),
        ({"assets_config": Path("other-assets.yaml")}, ["BTC/USDT"], "config/assets.yaml"),
        ({}, ["ETH/USDT"], "assets differ"),
    ],
)
def test_pipeline_rejects_exploratory_configuration_bypass(
    tmp_path, settings_change, assets, message
):
    base = Settings(
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
    if "assets_config" in settings_change:
        settings_change = {**settings_change, "assets_config": tmp_path / settings_change["assets_config"]}

    with pytest.raises(ValueError, match=message):
        run_pipeline(settings=replace(base, **settings_change), assets=assets, exchange=object())


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
