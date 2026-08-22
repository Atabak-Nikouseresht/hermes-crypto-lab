from datetime import datetime, timezone
import json
from pathlib import Path

import duckdb
import pytest

from src.forward_operations import (
    AlreadyRunningError,
    InterProcessLock,
    audit_missed_schedule,
    record_missed_windows,
    utc_and_rome_labels,
    verify_immutable_manifest,
)
from src.paper_broker import PaperConfig, PaperTradingSystem


ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")


def _config():
    return PaperConfig(
        assets=ASSETS,
        schedule_weekday=0,
        schedule_hour=9,
        schedule_minute=5,
        schedule_window_minutes=30,
        max_data_staleness_minutes=720,
    )


def test_new_utc_window_and_rome_dst_labels():
    system = PaperTradingSystem.__new__(PaperTradingSystem)
    system.config = _config()

    assert system._scheduled_key(
        system._utc(datetime(2026, 1, 5, 9, 5, tzinfo=timezone.utc))
    ) == "2026-01-05T09:05Z"
    assert system._scheduled_key(
        system._utc(datetime(2026, 1, 5, 9, 35, tzinfo=timezone.utc))
    ) == "2026-01-05T09:05Z"
    assert system._scheduled_key(
        system._utc(datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc))
    ) is None

    winter = utc_and_rome_labels(datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc))
    summer = utc_and_rome_labels(datetime(2026, 7, 6, 9, 10, tzinfo=timezone.utc))
    assert winter["rome"].endswith("+01:00")
    assert summer["rome"].endswith("+02:00")
    assert winter["utc"].endswith("+00:00")


def test_process_lock_prevents_overlap(tmp_path):
    lock_path = tmp_path / "paper.lock"
    with InterProcessLock(lock_path):
        with pytest.raises(AlreadyRunningError):
            with InterProcessLock(lock_path):
                pass


def test_missed_window_records_incident_without_backdated_trade(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2026-01-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    after_window = datetime(2026, 1, 5, 9, 40, tzinfo=timezone.utc)

    created = record_missed_windows(system.store, start=start, now=after_window, config=_config())

    with duckdb.connect(str(database), read_only=True) as connection:
        orders = connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        fills = connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        incidents = connection.execute(
            "SELECT incident_type, scheduled_for_utc FROM forward_incidents"
        ).fetchall()
    assert created == 1
    assert (orders, fills) == (0, 0)
    assert incidents[0][0] == "MISSED_SCHEDULE"


def test_committed_run_is_recovered_before_missed_window_audit(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2026-01-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
        connection.execute(
            """
            INSERT INTO paper_runs
            (run_id, started_at_utc, completed_at_utc, status, mode, schedule_key)
            VALUES ('committed','2026-01-05T09:10:00Z','2026-01-05T09:11:00Z',
                    'EXECUTED','PAPER','2026-01-05T09:05Z')
            """
        )

    created = record_missed_windows(
        system.store,
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        now=datetime(2026, 1, 5, 9, 40, tzinfo=timezone.utc),
        config=_config(),
    )

    with system.store.connect(read_only=True) as connection:
        window = connection.execute(
            "SELECT run_id, outcome FROM forward_schedule_windows"
        ).fetchone()
        incidents = connection.execute("SELECT COUNT(*) FROM forward_incidents").fetchone()[0]
    assert created == 0
    assert window == ("committed", "NO_REBALANCE")
    assert incidents == 0


def test_0936_audit_records_miss_without_market_or_portfolio_state(tmp_path):
    database = tmp_path / "paper.duckdb"
    system = PaperTradingSystem(database, _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2026-01-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )

    result = audit_missed_schedule(
        system.store,
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        now=datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc),
        config=_config(),
    )

    with system.store.connect(read_only=True) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills), "
            "(SELECT COUNT(*) FROM equity_snapshots), (SELECT COUNT(*) FROM forward_market_observations)"
        ).fetchone()
    assert result is not None and result.outcome == "MISSED_SCHEDULE"
    assert counts == (0, 0, 0, 0)


def test_checkpoint_manifest_is_hash_verified_and_cannot_be_replaced(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"locked": "abc"}, sort_keys=True), encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    sidecar = tmp_path / "manifest.sha256"
    sidecar.write_text(f"{digest}  manifest.json\n", encoding="ascii")
    assert verify_immutable_manifest(manifest, sidecar) == digest

    manifest.write_text(json.dumps({"locked": "changed"}), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_immutable_manifest(manifest, sidecar)
