import json
from concurrent.futures import ThreadPoolExecutor

import duckdb
import pandas as pd

from src.database import initialize_database, record_dataset_metadata
from src.storage import save_clean_parquet, save_json_atomic, save_raw_json


def test_raw_rows_are_preserved_and_clean_data_is_parquet(tmp_path):
    rows = [[1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0]]
    raw_path = tmp_path / "raw.json"
    parquet_path = tmp_path / "clean.parquet"
    frame = pd.DataFrame(
        [[pd.Timestamp("2024-01-01", tz="UTC"), 10.0, 12.0, 9.0, 11.0, 100.0]],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    save_raw_json(rows, raw_path)
    save_clean_parquet(frame, parquet_path)

    assert json.loads(raw_path.read_text(encoding="utf-8")) == rows
    loaded = pd.read_parquet(parquet_path)
    assert loaded.equals(frame)


def test_concurrent_immutable_publication_allows_exactly_one_writer(tmp_path):
    target = tmp_path / "immutable.json"

    def publish(value):
        try:
            save_json_atomic({"writer": value}, target, immutable=True)
            return "committed"
        except FileExistsError:
            return "exists"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, ("first", "second")))

    assert sorted(outcomes) == ["committed", "exists"]
    assert json.loads(target.read_text(encoding="utf-8"))["writer"] in {"first", "second"}
    assert not list(tmp_path.glob(".*.tmp"))


def test_raw_and_processed_concurrent_immutable_publication_allow_one_writer(tmp_path):
    rows = [[1704067200000, 10.0, 12.0, 9.0, 11.0, 100.0]]
    frame = pd.DataFrame(
        [[pd.Timestamp("2024-01-01", tz="UTC"), 10.0, 12.0, 9.0, 11.0, 100.0]],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    for writer, target in (
        (lambda path: save_raw_json(rows, path), tmp_path / "raw.json"),
        (lambda path: save_clean_parquet(frame, path), tmp_path / "processed.parquet"),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer, target) for _ in range(2)]
        outcomes = [future.exception() for future in futures]
        assert outcomes.count(None) == 1
        assert sum(isinstance(error, FileExistsError) for error in outcomes) == 1
        assert target.is_file()
        assert not list(tmp_path.glob(".*.tmp"))


def test_dataset_metadata_is_stored_in_duckdb(tmp_path):
    database_path = tmp_path / "trading.duckdb"
    initialize_database(database_path)
    record_dataset_metadata(
        database_path,
        run_id="run-1",
        symbol="BTC/USDT",
        timeframe="1d",
        raw_path="raw/BTC_USDT.json",
        parquet_path="processed/BTC_USDT.parquet",
        raw_rows=10,
        clean_rows=9,
        start_utc="2024-01-01T00:00:00+00:00",
        end_utc="2024-01-10T00:00:00+00:00",
        quality_summary={"missing_dates": 1},
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            "SELECT symbol, raw_rows, clean_rows, quality_summary FROM dataset_metadata"
        ).fetchone()
    assert row[:3] == ("BTC/USDT", 10, 9)
    assert json.loads(row[3])["missing_dates"] == 1
