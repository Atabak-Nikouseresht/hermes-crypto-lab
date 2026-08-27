from pathlib import Path

import duckdb

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
    assert (tmp_path / "data" / "processed" / "BTC_USDT_1d.parquet").exists()
    assert Path(result["markdown_report"]).exists()
    with duckdb.connect(str(settings.database_path), read_only=True) as connection:
        assert connection.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM dataset_metadata").fetchone()[0] == 1
