from datetime import datetime, timezone
import json
from pathlib import Path
import threading

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


def _forward_store(tmp_path):
    system = PaperTradingSystem(tmp_path / "paper.duckdb", _config())
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('test-forward','2026-01-01T00:00:00Z','locked','hash','gov','{}','ACTIVE')"
        )
    return system.store


def test_forward_incident_is_idempotent_and_preserves_existing_evidence(tmp_path):
    store = _forward_store(tmp_path)
    scheduled_for = datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc)
    first_seen = datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc)

    store.record_forward_incident(
        incident_type="MISSED_SCHEDULE",
        reason="original reason",
        now=first_seen,
        run_id="original-run",
        scheduled_for=scheduled_for,
    )
    store.record_forward_incident(
        incident_type="MISSED_SCHEDULE",
        reason="replacement must not overwrite evidence",
        now=datetime(2026, 1, 5, 9, 40, tzinfo=timezone.utc),
        run_id="replacement-run",
        scheduled_for=scheduled_for,
    )

    with store.connect(read_only=True) as connection:
        incidents = connection.execute(
            "SELECT incident_id, run_id, reason, created_at_utc FROM forward_incidents"
        ).fetchall()
        mappings = connection.execute(
            "SELECT experiment_id, incident_id FROM forward_experiment_incidents"
        ).fetchall()
        orphan_count = connection.execute(
            """
            SELECT COUNT(*) FROM forward_experiment_incidents mapping
            LEFT JOIN forward_incidents incident
              ON incident.incident_id=mapping.incident_id
            WHERE incident.incident_id IS NULL
            """
        ).fetchone()[0]

    assert len(incidents) == 1
    incident_id, run_id, reason, created_at = incidents[0]
    assert (run_id, reason, created_at) == ("original-run", "original reason", first_seen)
    assert mappings == [("test-forward", incident_id)]
    assert orphan_count == 0


def test_distinct_unscheduled_incidents_are_preserved(tmp_path):
    store = _forward_store(tmp_path)

    store.record_forward_incident(
        incident_type="MARKET_DATA_FAILURE",
        reason="first outage",
        now=datetime(2026, 1, 5, 9, 1, tzinfo=timezone.utc),
    )
    store.record_forward_incident(
        incident_type="MARKET_DATA_FAILURE",
        reason="second outage",
        now=datetime(2026, 1, 12, 9, 1, tzinfo=timezone.utc),
    )

    with store.connect(read_only=True) as connection:
        incidents = connection.execute(
            "SELECT reason FROM forward_incidents ORDER BY created_at_utc"
        ).fetchall()
        mappings = connection.execute(
            "SELECT COUNT(*) FROM forward_experiment_incidents"
        ).fetchone()[0]

    assert incidents == [("first outage",), ("second outage",)]
    assert mappings == 2


def test_forward_incident_association_failure_rolls_back_incident(tmp_path):
    store = _forward_store(tmp_path)
    with store.connect() as connection:
        connection.execute("DROP TABLE forward_experiment_incidents")
        connection.execute(
            """
            CREATE TABLE forward_experiment_incidents (
                experiment_id VARCHAR NOT NULL,
                incident_id VARCHAR NOT NULL CHECK (incident_id='forced-failure'),
                PRIMARY KEY (experiment_id, incident_id)
            )
            """
        )

    with pytest.raises(duckdb.ConstraintException):
        store.record_forward_incident(
            incident_type="MISSED_SCHEDULE",
            reason="must rollback",
            now=datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc),
        )

    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM forward_incidents").fetchone()[0] == 0


def test_concurrent_forward_incident_calls_are_idempotent(tmp_path):
    store = _forward_store(tmp_path)
    scheduled_for = datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc)
    barrier = threading.Barrier(2)
    errors = []

    def record(reason):
        try:
            barrier.wait()
            store.record_forward_incident(
                incident_type="MISSED_SCHEDULE",
                reason=reason,
                now=datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc),
                scheduled_for=scheduled_for,
            )
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [
        threading.Thread(target=record, args=("first",)),
        threading.Thread(target=record, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with store.connect(read_only=True) as connection:
        incidents = connection.execute("SELECT COUNT(*) FROM forward_incidents").fetchone()[0]
        mappings = connection.execute(
            "SELECT COUNT(*) FROM forward_experiment_incidents"
        ).fetchone()[0]

    assert errors == []
    assert (incidents, mappings) == (1, 1)


def test_forward_window_association_failure_rolls_back_window(tmp_path):
    store = _forward_store(tmp_path)
    with store.connect() as connection:
        connection.execute("DROP TABLE forward_experiment_windows")
        connection.execute(
            """
            CREATE TABLE forward_experiment_windows (
                experiment_id VARCHAR NOT NULL,
                schedule_key VARCHAR NOT NULL CHECK (schedule_key='forced-failure'),
                PRIMARY KEY (experiment_id, schedule_key)
            )
            """
        )

    with pytest.raises(duckdb.ConstraintException):
        store.record_forward_window(
            schedule_key="2026-01-05T09:05Z",
            scheduled_for=datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc),
            run_id=None,
            outcome="MISSED_SCHEDULE",
            now=datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc),
        )

    with store.connect(read_only=True) as connection:
        count = connection.execute("SELECT COUNT(*) FROM forward_schedule_windows").fetchone()[0]
    assert count == 0


def test_concurrent_forward_window_calls_are_idempotent(tmp_path):
    store = _forward_store(tmp_path)
    barrier = threading.Barrier(2)
    errors = []

    def record():
        try:
            barrier.wait()
            store.record_forward_window(
                schedule_key="2026-01-05T09:05Z",
                scheduled_for=datetime(2026, 1, 5, 9, 10, tzinfo=timezone.utc),
                run_id=None,
                outcome="MISSED_SCHEDULE",
                now=datetime(2026, 1, 5, 9, 36, tzinfo=timezone.utc),
            )
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=record), threading.Thread(target=record)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with store.connect(read_only=True) as connection:
        windows = connection.execute("SELECT COUNT(*) FROM forward_schedule_windows").fetchone()[0]
        mappings = connection.execute(
            "SELECT COUNT(*) FROM forward_experiment_windows"
        ).fetchone()[0]

    assert errors == []
    assert (windows, mappings) == (1, 1)


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
