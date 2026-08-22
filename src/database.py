from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import duckdb


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_runs (
                run_id VARCHAR PRIMARY KEY,
                started_at_utc TIMESTAMPTZ NOT NULL,
                completed_at_utc TIMESTAMPTZ,
                status VARCHAR NOT NULL,
                error_message VARCHAR
            );
            CREATE TABLE IF NOT EXISTS dataset_metadata (
                run_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                timeframe VARCHAR NOT NULL,
                raw_path VARCHAR NOT NULL,
                parquet_path VARCHAR NOT NULL,
                raw_rows BIGINT NOT NULL,
                clean_rows BIGINT NOT NULL,
                start_utc TIMESTAMPTZ,
                end_utc TIMESTAMPTZ,
                quality_summary JSON NOT NULL,
                recorded_at_utc TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (run_id, symbol)
            );
            """
        )


def start_run(path: Path, run_id: str) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "INSERT INTO ingestion_runs VALUES (?, ?, NULL, 'running', NULL)",
            [run_id, datetime.now(timezone.utc)],
        )


def finish_run(path: Path, run_id: str, status: str, error_message: str | None = None) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "UPDATE ingestion_runs SET completed_at_utc=?, status=?, error_message=? WHERE run_id=?",
            [datetime.now(timezone.utc), status, error_message, run_id],
        )


def record_dataset_metadata(
    path: Path,
    *,
    run_id: str,
    symbol: str,
    timeframe: str,
    raw_path: str,
    parquet_path: str,
    raw_rows: int,
    clean_rows: int,
    start_utc: str | None,
    end_utc: str | None,
    quality_summary: dict[str, Any],
) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            INSERT INTO dataset_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                symbol,
                timeframe,
                raw_path,
                parquet_path,
                raw_rows,
                clean_rows,
                start_utc,
                end_utc,
                json.dumps(quality_summary, sort_keys=True),
                datetime.now(timezone.utc),
            ],
        )
