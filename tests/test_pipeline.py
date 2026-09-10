from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import subprocess
from dataclasses import replace

import duckdb
import pandas as pd
import pytest

import run_data_pipeline
from run_data_pipeline import run_pipeline as _run_pipeline
from src.config import Settings, load_settings
from src.database import start_run
from src.forward_operations import AlreadyRunningError, InterProcessLock
from src.research_data import load_canonical_close_prices


@pytest.fixture(autouse=True)
def governed_assets(tmp_path):
    path = tmp_path / "config" / "assets.yaml"
    path.parent.mkdir()
    path.write_text("assets: [BTC/USDT]\n", encoding="utf-8")
    (path.parent / "canonical_research.yaml").write_text(
        "canonical_history_since: \"2024-01-01T00:00:00Z\"\n", encoding="utf-8"
    )


def run_pipeline(**kwargs):
    kwargs.setdefault("git_provenance", lambda _root: ("a" * 40, False))
    kwargs.setdefault("now_utc", datetime(2024, 1, 2, tzinfo=timezone.utc))
    return _run_pipeline(**kwargs)


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
        now_utc=datetime(2024, 1, 3, tzinfo=timezone.utc),
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


def test_canonical_publication_trims_to_validated_common_calendar(tmp_path):
    assets = ["BTC/USDT", "ETH/USDT", "XRP/USDT"]
    (tmp_path / "config" / "assets.yaml").write_text(
        "assets: [BTC/USDT, ETH/USDT, XRP/USDT]\n", encoding="utf-8"
    )
    settings = _pipeline_settings(tmp_path)

    def rows(first_day: int, last_day: int = 10):
        return [
            [
                int(pd.Timestamp(f"2024-01-{day:02d}T00:00:00Z").timestamp() * 1000),
                float(100 + day), float(101 + day), float(99 + day), float(100 + day), 100.0,
            ]
            for day in range(first_day, last_day + 1)
        ]

    source_rows = {
        "BTC/USDT": rows(1),
        "ETH/USDT": rows(3),
        "XRP/USDT": rows(5),
    }
    result = run_pipeline(
        settings=settings,
        assets=assets,
        downloader=lambda _market, symbol, **_kwargs: source_rows[symbol],
        exchange=object(),
        run_id="different-starts",
        now_utc=datetime(2024, 1, 11, tzinfo=timezone.utc),
    )

    expected = pd.date_range("2024-01-05", "2024-01-10", freq="D", tz="UTC")
    loaded = load_canonical_close_prices(settings.processed_dir, assets, "1d")
    assert loaded.index.equals(expected)
    assert list(loaded.columns) == assets
    manifest = json.loads(Path(result["dataset_manifest"]).read_text(encoding="utf-8"))
    assert manifest["canonical_common_start_utc"] == expected[0].isoformat()
    assert manifest["canonical_common_end_utc"] == expected[-1].isoformat()
    for symbol, raw_rows in source_rows.items():
        entry = manifest["datasets"][symbol]
        processed = pd.read_parquet(settings.processed_dir / entry["path"])
        assert pd.DatetimeIndex(pd.to_datetime(processed["timestamp"], utc=True)).equals(expected)
        assert entry["rows"] == len(expected)
        assert entry["start_utc"] == expected[0].isoformat()
        assert entry["end_utc"] == expected[-1].isoformat()
        assert json.loads((tmp_path / "data" / entry["raw_path"]).read_text(encoding="utf-8")) == raw_rows


def test_canonical_publication_rejects_internal_gap_after_common_start(tmp_path):
    assets = ["BTC/USDT", "ETH/USDT"]
    (tmp_path / "config" / "assets.yaml").write_text(
        "assets: [BTC/USDT, ETH/USDT]\n", encoding="utf-8"
    )
    settings = _pipeline_settings(tmp_path)

    def row(day):
        timestamp = int(pd.Timestamp(f"2024-01-{day:02d}T00:00:00Z").timestamp() * 1000)
        return [timestamp, 100.0, 101.0, 99.0, 100.0, 100.0]

    source_rows = {
        "BTC/USDT": [row(day) for day in range(1, 11)],
        "ETH/USDT": [row(day) for day in (5, 6, 8, 9, 10)],
    }
    with pytest.raises(ValueError, match="quality validation failed"):
        run_pipeline(
            settings=settings, assets=assets,
            downloader=lambda _market, symbol, **_kwargs: source_rows[symbol],
            exchange=object(), run_id="internal-gap",
            now_utc=datetime(2024, 1, 11, tzinfo=timezone.utc),
        )
    # Source rows are immutable raw evidence even though no canonical pointer was published.
    assert (settings.raw_dir / "internal-gap" / "ETH_USDT_1d.json").is_file()
    assert not (settings.processed_dir / "dataset_manifest.json").exists()


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
    with pytest.raises(run_data_pipeline.CanonicalProvenanceError, match="clean Git tree"):
        run_data_pipeline._git_provenance(tmp_path)

    monkeypatch.setattr(run_data_pipeline.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no git")))
    with pytest.raises(run_data_pipeline.CanonicalProvenanceError, match="unavailable"):
        run_data_pipeline._git_provenance(tmp_path)


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


def _pipeline_settings(tmp_path):
    return Settings(
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


def _valid_rows():
    return [[1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0]]


def _run_status(database_path, run_id):
    with duckdb.connect(str(database_path), read_only=True) as connection:
        return connection.execute(
            "SELECT status, completed_at_utc, error_message FROM ingestion_runs WHERE run_id=?",
            [run_id],
        ).fetchone()


def test_exchange_creation_failure_after_start_terminalizes_ingestion_run(tmp_path, monkeypatch):
    settings = _pipeline_settings(tmp_path)
    monkeypatch.setattr(
        run_data_pipeline,
        "create_exchange",
        lambda *_args: (_ for _ in ()).throw(OSError("exchange unavailable")),
    )

    with pytest.raises(OSError, match="exchange unavailable"):
        run_pipeline(settings=settings, assets=["BTC/USDT"], run_id="create-fails")

    status, completed_at, error = _run_status(settings.database_path, "create-fails")
    assert status == "failed"
    assert completed_at is not None
    assert "exchange unavailable" in error


def test_provenance_failure_after_exchange_creation_terminalizes_and_closes_once(tmp_path):
    settings = _pipeline_settings(tmp_path)

    class Exchange:
        closed = 0

        def close(self):
            self.closed += 1

    market = Exchange()
    with pytest.raises(RuntimeError, match="provenance unavailable"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            exchange=market,
            run_id="provenance-fails",
            git_provenance=lambda _root: (_ for _ in ()).throw(
                RuntimeError("provenance unavailable")
            ),
        )

    status, completed_at, error = _run_status(settings.database_path, "provenance-fails")
    assert status == "failed"
    assert completed_at is not None
    assert "provenance unavailable" in error
    assert market.closed == 1


def test_artifact_failure_leaves_previous_canonical_pointer_and_terminalizes_run(
    tmp_path, monkeypatch
):
    settings = _pipeline_settings(tmp_path)
    run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=object(),
        run_id="previous",
    )
    monkeypatch.setattr(
        run_data_pipeline,
        "save_clean_parquet",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("parquet write failed")),
    )

    with pytest.raises(OSError, match="parquet write failed"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="artifact-fails",
        )

    pointer = json.loads(
        (settings.processed_dir / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert pointer["run_id"] == "previous"
    assert _run_status(settings.database_path, "artifact-fails")[0] == "failed"


def test_pointer_publication_failure_does_not_complete_or_replace_previous_pointer(
    tmp_path, monkeypatch
):
    settings = _pipeline_settings(tmp_path)
    run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=object(),
        run_id="previous",
    )
    original_save = run_data_pipeline.save_json_atomic

    def fail_before_pointer(payload, path, **kwargs):
        if path == settings.processed_dir / "dataset_manifest.json":
            raise OSError("pointer publication interrupted")
        return original_save(payload, path, **kwargs)

    monkeypatch.setattr(run_data_pipeline, "save_json_atomic", fail_before_pointer)
    with pytest.raises(OSError, match="pointer publication interrupted"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="pointer-fails",
        )

    pointer = json.loads(
        (settings.processed_dir / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert pointer["run_id"] == "previous"
    assert _run_status(settings.database_path, "pointer-fails")[0] == "failed"


def test_recovery_finalizes_exact_run_when_pointer_published_before_completion(
    tmp_path, monkeypatch
):
    settings = _pipeline_settings(tmp_path)
    original_complete = run_data_pipeline.complete_published_run
    monkeypatch.setattr(
        run_data_pipeline,
        "complete_published_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("interrupted after pointer publication")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="interrupted after pointer publication"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="interrupted",
        )

    pointer_path = settings.processed_dir / "dataset_manifest.json"
    immutable_path = settings.processed_dir / "interrupted" / "dataset_manifest.json"
    pointer_before_recovery = pointer_path.read_bytes()
    assert json.loads(pointer_before_recovery)["run_id"] == "interrupted"
    assert _run_status(settings.database_path, "interrupted")[0] == "running"

    monkeypatch.setattr(run_data_pipeline, "complete_published_run", original_complete)
    run_data_pipeline.recover_interrupted_publications(settings)

    status, completed_at, error = _run_status(settings.database_path, "interrupted")
    assert (status, error) == ("completed", None)
    assert completed_at is not None
    assert pointer_path.read_bytes() == pointer_before_recovery
    assert json.loads(immutable_path.read_text(encoding="utf-8")) == json.loads(
        pointer_before_recovery
    )
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM dataset_metadata WHERE run_id='interrupted'"
        ).fetchone()[0] == 1


def test_runtime_finalization_error_leaves_published_run_recoverable(tmp_path, monkeypatch):
    settings = _pipeline_settings(tmp_path)
    original_complete = run_data_pipeline.complete_published_run
    monkeypatch.setattr(
        run_data_pipeline,
        "complete_published_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database finalization failed")
        ),
    )

    with pytest.raises(RuntimeError, match="database finalization failed"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="finalization-error",
        )

    pointer_path = settings.processed_dir / "dataset_manifest.json"
    immutable_path = settings.processed_dir / "finalization-error" / "dataset_manifest.json"
    pointer_before_recovery = pointer_path.read_bytes()
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        row = connection.execute(
            "SELECT status, publication_state FROM ingestion_runs WHERE run_id='finalization-error'"
        ).fetchone()
    assert row == ("running", "published")
    assert json.loads(pointer_before_recovery)["run_id"] == "finalization-error"
    assert immutable_path.read_bytes() == pointer_before_recovery

    monkeypatch.setattr(run_data_pipeline, "complete_published_run", original_complete)
    run_data_pipeline.recover_interrupted_publications(settings)
    run_data_pipeline.recover_interrupted_publications(settings)

    status, completed_at, error = _run_status(settings.database_path, "finalization-error")
    assert (status, error) == ("completed", None)
    assert completed_at is not None
    assert pointer_path.read_bytes() == pointer_before_recovery
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM dataset_metadata WHERE run_id='finalization-error'"
        ).fetchone()[0] == 1


def test_artifacts_ready_exact_pointer_recovers_after_crash_before_published_state(
    tmp_path, monkeypatch
):
    settings = _pipeline_settings(tmp_path)
    original_mark = run_data_pipeline.mark_publication_published
    monkeypatch.setattr(
        run_data_pipeline,
        "mark_publication_published",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("crashed before publication state transition")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="crashed before publication state"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="artifacts-ready-pointer",
        )

    pointer_path = settings.processed_dir / "dataset_manifest.json"
    pointer_before_recovery = pointer_path.read_bytes()
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        row = connection.execute(
            "SELECT status, publication_state FROM ingestion_runs WHERE run_id='artifacts-ready-pointer'"
        ).fetchone()
    assert row == ("running", "artifacts_ready")

    monkeypatch.setattr(run_data_pipeline, "mark_publication_published", original_mark)
    run_data_pipeline.recover_interrupted_publications(settings)
    run_data_pipeline.recover_interrupted_publications(settings)

    status, completed_at, error = _run_status(
        settings.database_path, "artifacts-ready-pointer"
    )
    assert (status, error) == ("completed", None)
    assert completed_at is not None
    assert pointer_path.read_bytes() == pointer_before_recovery


def test_mark_published_error_preserves_verified_artifacts_ready_publication(
    tmp_path, monkeypatch
):
    settings = _pipeline_settings(tmp_path)
    original_mark = run_data_pipeline.mark_publication_published
    monkeypatch.setattr(
        run_data_pipeline,
        "mark_publication_published",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish state failed")),
    )
    with pytest.raises(RuntimeError, match="publish state failed"):
        run_pipeline(settings=settings, assets=["BTC/USDT"], downloader=lambda *_a, **_k: _valid_rows(), exchange=object(), run_id="mark-published-error")
    pointer_path = settings.processed_dir / "dataset_manifest.json"
    immutable_path = settings.processed_dir / "mark-published-error" / "dataset_manifest.json"
    pointer_before_recovery = pointer_path.read_bytes()
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        row = connection.execute("SELECT status, publication_state FROM ingestion_runs WHERE run_id='mark-published-error'").fetchone()
    assert row == ("running", "artifacts_ready")
    assert immutable_path.read_bytes() == pointer_before_recovery
    monkeypatch.setattr(run_data_pipeline, "mark_publication_published", original_mark)
    run_data_pipeline.recover_interrupted_publications(settings)
    run_data_pipeline.recover_interrupted_publications(settings)
    status, completed_at, error = _run_status(settings.database_path, "mark-published-error")
    assert (status, error) == ("completed", None)
    assert completed_at is not None
    assert pointer_path.read_bytes() == pointer_before_recovery


def test_active_canonical_writer_lock_refuses_second_pipeline_without_touching_run(
    tmp_path,
):
    settings = _pipeline_settings(tmp_path)
    lock_path = settings.project_root / "runtime" / "canonical_pipeline.lock"
    with InterProcessLock(lock_path, command_name="active-canonical-pipeline"):
        with pytest.raises(AlreadyRunningError):
            run_pipeline(
                settings=settings,
                assets=["BTC/USDT"],
                downloader=lambda *_args, **_kwargs: _valid_rows(),
                exchange=object(),
                run_id="refused-second-run",
            )

    assert not settings.database_path.exists()


def test_canonical_writer_lock_releases_after_pipeline_failure(tmp_path):
    settings = _pipeline_settings(tmp_path)
    with pytest.raises(RuntimeError, match="No finalized OHLCV rows"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: [],
            exchange=object(),
            run_id="first-fails",
        )

    result = run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=object(),
        run_id="second-succeeds",
    )
    assert result["run_id"] == "second-succeeds"


def test_canonical_lock_ignores_stale_owner_sidecar_after_os_lock_release(tmp_path):
    settings = _pipeline_settings(tmp_path)
    lock_path = settings.project_root / "runtime" / "canonical_pipeline.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.with_suffix(lock_path.suffix + ".owner.json").write_text(
        json.dumps({"pid": 999999, "token": "stale"}), encoding="utf-8"
    )

    result = run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=object(),
        run_id="stale-owner-succeeds",
    )
    assert result["run_id"] == "stale-owner-succeeds"


def test_unexpected_publication_state_transition_fails_closed(tmp_path):
    settings = _pipeline_settings(tmp_path)
    run_data_pipeline.initialize_database(settings.database_path)
    start_run(settings.database_path, "unexpected-state")

    with pytest.raises(RuntimeError, match="to published"):
        run_data_pipeline.mark_publication_published(
            settings.database_path, "unexpected-state"
        )


@pytest.mark.parametrize("tamper", ["pointer", "immutable"])
def test_recovery_refuses_published_manifest_identity_mismatch(tmp_path, tamper):
    settings = _pipeline_settings(tmp_path)
    settings.processed_dir.mkdir(parents=True)
    immutable_path = settings.processed_dir / "run-A" / "dataset_manifest.json"
    immutable_path.parent.mkdir()
    immutable_payload = {
        "run_id": "run-A",
        "version_manifest_path": "run-A/dataset_manifest.json",
    }
    immutable_path.write_text(json.dumps(immutable_payload), encoding="utf-8")
    pointer_payload = immutable_payload.copy()
    if tamper == "pointer":
        pointer_payload = {
            "run_id": "run-B",
            "version_manifest_path": "run-B/dataset_manifest.json",
        }
    else:
        immutable_path.write_text(
            json.dumps({**immutable_payload, "tampered": True}), encoding="utf-8"
        )
    (settings.processed_dir / "dataset_manifest.json").write_text(
        json.dumps(pointer_payload), encoding="utf-8"
    )
    run_data_pipeline.initialize_database(settings.database_path)
    with duckdb.connect(str(settings.database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_runs VALUES
            ('run-A', now(), NULL, 'running', NULL, 'published', ?, ?)
            """,
            [
                "run-A/dataset_manifest.json",
                hashlib.sha256(json.dumps(immutable_payload).encode()).hexdigest(),
            ],
        )

    run_data_pipeline.recover_interrupted_publications(settings)

    status, completed_at, _error = _run_status(settings.database_path, "run-A")
    assert status == "failed"
    assert completed_at is not None


def test_successful_publication_closes_exchange_and_completes_after_verified_pointer(tmp_path):
    settings = _pipeline_settings(tmp_path)

    class Exchange:
        closed = 0

        def close(self):
            self.closed += 1

    market = Exchange()
    result = run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=market,
        run_id="successful",
    )

    pointer_path = settings.processed_dir / "dataset_manifest.json"
    immutable_path = settings.processed_dir / "successful" / "dataset_manifest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert result["run_id"] == "successful"
    assert pointer["run_id"] == "successful"
    assert immutable_path.exists()
    assert pointer == json.loads(immutable_path.read_text(encoding="utf-8"))
    assert _run_status(settings.database_path, "successful")[0] == "completed"
    assert market.closed == 1


def test_cleanup_failure_does_not_mask_primary_pipeline_failure(tmp_path):
    settings = _pipeline_settings(tmp_path)

    class Exchange:
        def close(self):
            raise OSError("close failure")

    with pytest.raises(RuntimeError, match="No finalized OHLCV rows"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: [],
            exchange=Exchange(),
            run_id="primary-failure",
        )

    status, completed_at, error = _run_status(settings.database_path, "primary-failure")
    assert status == "failed"
    assert completed_at is not None
    assert "No finalized OHLCV rows" in error


def test_cleanup_failure_after_pointer_publication_does_not_fail_completed_run(tmp_path):
    settings = _pipeline_settings(tmp_path)

    class Exchange:
        closed = 0

        def close(self):
            self.closed += 1
            raise OSError("close failure")

    market = Exchange()
    result = run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=market,
        run_id="cleanup-after-publication",
    )

    pointer = json.loads(
        (settings.processed_dir / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert result["run_id"] == "cleanup-after-publication"
    assert pointer["run_id"] == "cleanup-after-publication"
    assert _run_status(settings.database_path, "cleanup-after-publication")[0] == "completed"
    assert market.closed == 1


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _daily_row(value: str) -> list[float]:
    return [int(_utc(value).timestamp() * 1000), 10.0, 12.0, 9.0, 11.0, 100.0]


def test_latest_finalized_daily_open_uses_utc_previous_midnight():
    assert run_data_pipeline._latest_finalized_daily_open(_utc("2026-09-06T18:00:00Z")) == _utc("2026-09-05T00:00:00Z")
    assert run_data_pipeline._latest_finalized_daily_open(_utc("2026-09-07T00:00:01Z")) == _utc("2026-09-06T00:00:00Z")


@pytest.mark.parametrize("commit", ["", "abc123", "g" * 40])
def test_git_provenance_rejects_malformed_commit(monkeypatch, tmp_path, commit):
    monkeypatch.setattr(run_data_pipeline.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, commit, ""))
    with pytest.raises(run_data_pipeline.CanonicalProvenanceError, match="malformed"):
        run_data_pipeline._git_provenance(tmp_path)


def test_git_provenance_rejects_git_command_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(run_data_pipeline.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git"])))
    with pytest.raises(run_data_pipeline.CanonicalProvenanceError, match="unavailable"):
        run_data_pipeline._git_provenance(tmp_path)


def test_pipeline_requires_exact_finalized_endpoint_and_preserves_previous_pointer(tmp_path):
    settings = _pipeline_settings(tmp_path)
    now = _utc("2026-09-06T18:00:00Z")

    def provenance(_root):
        return "a" * 40, False
    run_pipeline(settings=settings, assets=["BTC/USDT"], downloader=lambda *_a, **_k: [_daily_row("2026-09-04T00:00:00Z"), _daily_row("2026-09-05T00:00:00Z")], exchange=object(), run_id="complete-endpoint", git_provenance=provenance, now_utc=now)
    pointer_path = settings.processed_dir / "dataset_manifest.json"
    pointer_before = pointer_path.read_bytes()
    with pytest.raises(ValueError, match="BTC/USDT.*actual_end.*expected_end"):
        run_pipeline(settings=settings, assets=["BTC/USDT"], downloader=lambda *_a, **_k: [_daily_row("2026-09-04T00:00:00Z")], exchange=object(), run_id="one-day-stale", git_provenance=provenance, now_utc=now)
    assert pointer_path.read_bytes() == pointer_before
    assert _run_status(settings.database_path, "one-day-stale")[0] == "failed"


def test_pipeline_rejects_current_forming_candle_and_stale_asset(tmp_path):
    settings = _pipeline_settings(tmp_path)
    now = _utc("2026-09-06T18:00:00Z")
    with pytest.raises(ValueError, match="BTC/USDT.*actual_end.*expected_end"):
        run_pipeline(settings=settings, assets=["BTC/USDT"], downloader=lambda *_a, **_k: [_daily_row("2026-09-05T00:00:00Z"), _daily_row("2026-09-06T00:00:00Z")], exchange=object(), run_id="forming-candle", git_provenance=lambda _root: ("a" * 40, False), now_utc=now)
    (tmp_path / "config" / "assets.yaml").write_text("assets: [BTC/USDT, ETH/USDT, BNB/USDT]\n", encoding="utf-8")
    def rows_for_symbol(_exchange, symbol, **_kwargs):
        return {"BTC/USDT": [_daily_row("2026-09-05T00:00:00Z")], "ETH/USDT": [_daily_row("2026-09-05T00:00:00Z")], "BNB/USDT": [_daily_row("2026-09-03T00:00:00Z")]}[symbol]
    with pytest.raises(ValueError, match="BNB/USDT.*actual_end.*expected_end"):
        run_pipeline(settings=settings, assets=["BTC/USDT", "ETH/USDT", "BNB/USDT"], downloader=rows_for_symbol, exchange=object(), run_id="one-asset-stale", git_provenance=lambda _root: ("a" * 40, False), now_utc=now)
    assert _run_status(settings.database_path, "one-asset-stale")[0] == "failed"


def test_pipeline_provenance_failure_preserves_previous_pointer(tmp_path):
    settings = _pipeline_settings(tmp_path)
    now = _utc("2026-09-06T18:00:00Z")
    rows = [_daily_row("2026-09-05T00:00:00Z")]
    run_pipeline(settings=settings, assets=["BTC/USDT"], downloader=lambda *_a, **_k: rows, exchange=object(), run_id="provenance-good", git_provenance=lambda _root: ("a" * 40, False), now_utc=now)
    pointer_path = settings.processed_dir / "dataset_manifest.json"
    pointer_before = pointer_path.read_bytes()
    with pytest.raises(run_data_pipeline.CanonicalProvenanceError, match="unavailable"):
        run_pipeline(settings=settings, assets=["BTC/USDT"], exchange=object(), run_id="provenance-bad", git_provenance=lambda _root: (_ for _ in ()).throw(run_data_pipeline.CanonicalProvenanceError("unavailable")), now_utc=now)
    assert pointer_path.read_bytes() == pointer_before
    assert _run_status(settings.database_path, "provenance-bad")[0] == "failed"


def test_pipeline_rejects_multi_day_stale_endpoint_at_midnight_boundary(tmp_path):
    settings = _pipeline_settings(tmp_path)
    now = _utc("2026-09-07T00:00:01Z")

    def provenance(_root):
        return "a" * 40, False

    run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_a, **_k: [_daily_row("2026-09-06T00:00:00Z")],
        exchange=object(),
        run_id="midnight-complete",
        git_provenance=provenance,
        now_utc=now,
    )
    with pytest.raises(ValueError, match="BTC/USDT.*actual_end.*expected_end"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_a, **_k: [_daily_row("2026-09-04T00:00:00Z")],
            exchange=object(),
            run_id="multi-day-stale",
            git_provenance=provenance,
            now_utc=now,
        )
    assert _run_status(settings.database_path, "multi-day-stale")[0] == "failed"


@pytest.mark.parametrize(
    "provenance",
    [
        lambda _root: ("a" * 40, True),
        lambda _root: ("unavailable", None),
    ],
)
def test_pipeline_rejects_noncanonical_injected_provenance(tmp_path, provenance):
    settings = _pipeline_settings(tmp_path)
    with pytest.raises(run_data_pipeline.CanonicalProvenanceError):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            exchange=object(),
            run_id="invalid-provenance",
            git_provenance=provenance,
            now_utc=_utc("2026-09-06T18:00:00Z"),
        )
    assert _run_status(settings.database_path, "invalid-provenance")[0] == "failed"


@pytest.mark.parametrize("configured_since", ["2023-12-31T00:00:00Z", "2024-01-02T00:00:00Z"])
def test_pipeline_rejects_canonical_history_mismatch_and_preserves_pointer(
    tmp_path, configured_since
):
    settings = _pipeline_settings(tmp_path)
    run_pipeline(
        settings=settings,
        assets=["BTC/USDT"],
        downloader=lambda *_args, **_kwargs: _valid_rows(),
        exchange=object(),
        run_id="governed-history",
    )
    pointer_path = settings.processed_dir / "dataset_manifest.json"
    pointer_before = pointer_path.read_bytes()

    with pytest.raises(ValueError, match="Canonical history mismatch"):
        run_pipeline(
            settings=replace(settings, since=configured_since),
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="history-mismatch",
        )

    assert pointer_path.read_bytes() == pointer_before
    assert _run_status(settings.database_path, "history-mismatch")[0] == "failed"


def test_pipeline_rejects_hcl_since_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HCL_SINCE", "2024-01-02T00:00:00Z")
    settings = load_settings(tmp_path)

    with pytest.raises(ValueError, match="Canonical history mismatch"):
        run_pipeline(
            settings=settings,
            assets=["BTC/USDT"],
            downloader=lambda *_args, **_kwargs: _valid_rows(),
            exchange=object(),
            run_id="environment-history-mismatch",
        )

    assert _run_status(settings.database_path, "environment-history-mismatch")[0] == "failed"
