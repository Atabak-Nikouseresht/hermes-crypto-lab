from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import re
from types import SimpleNamespace

import duckdb
import pytest

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION, QUOTE_COHERENCE_CONTRACT_VERSION
from src.paper_store import PaperStore, ReconciliationResult


V5_EXECUTION = "versioned ask-bid execution context"
V12_QUOTE = "quote coherence provenance and legacy v5 normalization"
V13_RELEASE = "per-forward-run release provenance"
V14_ATTEMPT = "retryable forward admission attempt schedule identity"
V15_OFFICIAL_SCHEDULE = "official schedule nullability parity"
V16_MARKET_RULES = "prospective Binance market-rule evidence"


def _store(path: Path) -> PaperStore:
    return PaperStore(path, account_id="locked_strategy", initial_cash=2_000.0)


def _schema_versions_from_snapshot(snapshot: str) -> dict[int, str]:
    footer = snapshot.split("-- schema versions\n", maxsplit=1)[1].strip()
    return dict(ast.literal_eval(footer))


def _seed_legacy_paper_runs_without_official_scheduled(path: Path) -> None:
    """Create the actual pre-6e148c4 paper_runs shape with representative rows."""
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE paper_runs (
                run_id VARCHAR PRIMARY KEY,
                started_at_utc TIMESTAMPTZ NOT NULL,
                completed_at_utc TIMESTAMPTZ,
                status VARCHAR NOT NULL,
                mode VARCHAR NOT NULL,
                schedule_key VARCHAR UNIQUE,
                signal_timestamp_utc TIMESTAMPTZ,
                data_timestamp_utc TIMESTAMPTZ,
                message VARCHAR,
                reconciliation JSON
            )
            """
        )
        connection.execute(
            "INSERT INTO paper_runs VALUES (?, ?, ?, 'EXECUTED', 'PAPER', ?, ?, ?, ?, '{}')",
            [
                "historical-paper",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc),
                "2024-01-01T00:05Z",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                "historical representative row",
            ],
        )
        connection.execute(
            "INSERT INTO paper_runs VALUES (?, ?, NULL, 'DATA_HALT', 'DRY_RUN', NULL, NULL, NULL, ?, '{}')",
            ["historical-dry-run", datetime(2024, 1, 8, tzinfo=timezone.utc), "historical halt"],
        )


def _valid_release_provenance(now: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        git_commit="a" * 40,
        git_dirty=False,
        hardening_manifest_sha256="b" * 64,
        execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
        captured_at_utc=now,
    )


def _record_current_no_rebalance_evidence(store: PaperStore, *, run_id: str, now: datetime) -> None:
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_execution_outcomes VALUES (?, 'NO_REBALANCE_REQUIRED', ?)",
            [run_id, now],
        )
        connection.execute(
            "INSERT INTO paper_quote_coherence_context VALUES (?, ?, 30, ?, ?, ?)",
            [run_id, QUOTE_COHERENCE_CONTRACT_VERSION, now, now, now],
        )


def _seed_valid_official_run(store: PaperStore, *, run_id: str = "current") -> datetime:
    now = datetime.now(timezone.utc)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES (?, ?, 'locked', 'strategy', ?, '{}', 'ACTIVE')",
            ["forward-1", now, f"governance-{run_id}"],
        )
    store.insert_run(
        run_id=run_id,
        started_at=now,
        mode="PAPER",
        schedule_key=f"2026-09-07T00:05Z-{run_id}",
        signal_timestamp=now,
        data_timestamp=now,
        official_scheduled=True,
        release_provenance=_valid_release_provenance(now),
    )
    store.finish_run(
        run_id=run_id,
        status="EXECUTED",
        completed_at=now,
        message="test official run",
        reconciliation=ReconciliationResult(True, "test"),
    )
    _record_current_no_rebalance_evidence(store, run_id=run_id, now=now)
    assert store.reconcile().valid
    return now


def _assert_official_scheduled_default_false(connection, *, run_id: str) -> None:
    now = datetime.now(timezone.utc)
    connection.execute(
        "INSERT INTO paper_runs (run_id, started_at_utc, status, mode) VALUES (?, ?, 'RUNNING', 'DRY_RUN')",
        [run_id, now],
    )
    assert connection.execute(
        "SELECT official_scheduled FROM paper_runs WHERE run_id=?", [run_id]
    ).fetchone() == (False,)


def test_fresh_schema_has_unambiguous_v5_and_release_provenance_snapshot(tmp_path):
    store = _store(tmp_path / "fresh.duckdb")
    snapshot = (Path(__file__).resolve().parents[1] / "forward_experiment" / "paper_schema.sql").read_text(
        encoding="utf-8"
    )
    with store.connect(read_only=True) as connection:
        versions = dict(connection.execute("SELECT version, description FROM paper_schema_versions").fetchall())
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('paper_run_release_provenance')").fetchall()
        }

    assert set(versions) == set(range(2, 17))
    assert versions[5] == V5_EXECUTION
    assert versions[12] == V12_QUOTE
    assert versions[13] == V13_RELEASE
    assert versions[14] == V14_ATTEMPT
    assert versions[15] == V15_OFFICIAL_SCHEDULE
    assert versions[16] == V16_MARKET_RULES
    assert {
        "run_id",
        "git_commit",
        "git_dirty",
        "hardening_manifest_sha256",
        "execution_protocol_version",
        "captured_at_utc",
    } <= columns
    assert "-- schema versions" in snapshot
    assert V5_EXECUTION in snapshot
    assert V12_QUOTE in snapshot
    assert V13_RELEASE in snapshot
    assert "-- paper_run_release_provenance" in snapshot
    assert _schema_versions_from_snapshot(snapshot) == versions


def _seed_legacy_v5(path: Path, description: str) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE paper_schema_versions ("
            "version INTEGER PRIMARY KEY, applied_at_utc TIMESTAMPTZ NOT NULL, description VARCHAR NOT NULL)"
        )
        connection.execute(
            "INSERT INTO paper_schema_versions VALUES (5, ?, ?)",
            [datetime(2024, 1, 1, tzinfo=timezone.utc), description],
        )


@pytest.mark.parametrize("description", ["quote coherence contract provenance", V5_EXECUTION])
def test_legacy_v5_meaning_is_preserved_by_actual_initializer_migration(tmp_path, description):
    path = tmp_path / f"legacy-{description[:5]}.duckdb"
    _seed_legacy_v5(path, description)

    migrated = _store(path)
    with migrated.connect(read_only=True) as connection:
        versions = dict(connection.execute("SELECT version, description FROM paper_schema_versions").fetchall())

    assert versions[5] == description
    assert versions[12] == V12_QUOTE
    assert versions[13] == V13_RELEASE
    assert versions[14] == V14_ATTEMPT


def test_release_provenance_is_immutable_and_required_after_adoption(tmp_path):
    store = _store(tmp_path / "adoption.duckdb")
    now = datetime.now(timezone.utc)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('forward-1', ?, 'locked', 'strategy', 'governance', '{}', 'ACTIVE')",
            [now],
        )
        connection.execute(
            "INSERT INTO paper_runs "
            "(run_id, started_at_utc, completed_at_utc, status, mode, official_scheduled, schedule_key, reconciliation) "
            "VALUES ('current', ?, ?, 'EXECUTED', 'PAPER', TRUE, '2026-09-07T00:05Z', '{}')",
            [now, now],
        )
        connection.execute(
            "INSERT INTO paper_runs "
            "(run_id, started_at_utc, completed_at_utc, status, mode, official_scheduled, schedule_key, reconciliation) "
            "VALUES ('historical', '2000-01-01T00:00:00Z', '2000-01-01T00:01:00Z', "
            "'EXECUTED', 'PAPER', FALSE, '2000-01-03T00:05Z', '{}')"
        )

    assert not store.reconcile().valid
    store.record_run_release_provenance(
        run_id="current",
        git_commit="a" * 40,
        git_dirty=False,
        hardening_manifest_sha256="b" * 64,
        execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
        captured_at_utc=now,
    )
    _record_current_no_rebalance_evidence(store, run_id="current", now=now)
    assert store.reconcile().valid
    with pytest.raises(FileExistsError):
        store.record_run_release_provenance(
            run_id="current",
            git_commit="c" * 40,
            git_dirty=False,
            hardening_manifest_sha256="b" * 64,
            execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
            captured_at_utc=now,
        )


def _schema_structure(connection, table: str) -> tuple[dict, set]:
    return (
        {row[1]: (row[2].upper(), bool(row[3]), bool(row[5])) for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()},
        {(row[0], tuple(row[1])) for row in connection.execute("SELECT constraint_type, constraint_column_names FROM duckdb_constraints() WHERE table_name=? AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')", [table]).fetchall()},
    )


def test_fresh_runtime_schema_structurally_matches_checked_in_snapshot(tmp_path):
    snapshot = (Path(__file__).resolve().parents[1] / "forward_experiment" / "paper_schema.sql").read_text(encoding="utf-8")
    canonical = duckdb.connect(str(tmp_path / "canonical.duckdb"))
    try:
        for statement in re.findall(r"CREATE TABLE [^;]+", snapshot):
            canonical.execute(statement)
        runtime_store = _store(tmp_path / "runtime.duckdb")
        with runtime_store.connect(read_only=True) as runtime:
            tables = {row[0] for row in canonical.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
            assert tables == {row[0] for row in runtime.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
            for table in tables:
                assert _schema_structure(runtime, table) == _schema_structure(canonical, table)
            runtime_versions = dict(runtime.execute("SELECT version, description FROM paper_schema_versions").fetchall())
            assert set(runtime_versions) == set(range(2, 17))
            assert runtime_versions[15] == V15_OFFICIAL_SCHEDULE
            assert runtime_versions[16] == V16_MARKET_RULES
            assert _schema_versions_from_snapshot(snapshot) == runtime_versions
    finally:
        canonical.close()


def test_adoption_boundary_requires_official_provenance_at_and_after_v13(tmp_path):
    store = _store(tmp_path / "boundary.duckdb")
    boundary = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    with store.connect() as connection:
        connection.execute("UPDATE paper_schema_versions SET applied_at_utc=? WHERE version=13", [boundary])
        connection.execute("INSERT INTO forward_experiments VALUES ('forward', ?, 'locked', 'strategy', 'governance', '{}', 'ACTIVE')", [boundary])
        for run_id, timestamp in (("before", datetime(2026, 9, 5, 11, 59, 59, tzinfo=timezone.utc)), ("at", boundary), ("after", datetime(2026, 9, 5, 12, 0, 1, tzinfo=timezone.utc))):
            connection.execute("INSERT INTO paper_runs (run_id, started_at_utc, completed_at_utc, status, mode, official_scheduled) VALUES (?, ?, ?, 'DATA_HALT', 'PAPER', TRUE)", [run_id, timestamp, timestamp])
    assert not store.reconcile().valid
    for run_id, timestamp in (("at", boundary), ("after", datetime(2026, 9, 5, 12, 0, 1, tzinfo=timezone.utc))):
        store.record_run_release_provenance(run_id=run_id, git_commit="a" * 40, git_dirty=False, hardening_manifest_sha256="b" * 64, execution_protocol_version=EXECUTION_PROTOCOL_VERSION, captured_at_utc=timestamp)
    assert store.reconcile().valid
    with store.connect() as connection:
        connection.execute("DELETE FROM paper_run_release_provenance WHERE run_id='after'")
    assert not store.reconcile().valid


def test_legacy_paper_runs_without_official_scheduled_migrates_to_false_not_null_default(tmp_path):
    path = tmp_path / "legacy-no-official-scheduled.duckdb"
    _seed_legacy_paper_runs_without_official_scheduled(path)

    store = _store(path)

    with store.connect() as connection:
        rows = connection.execute(
            "SELECT run_id, official_scheduled FROM paper_runs ORDER BY run_id"
        ).fetchall()
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info('paper_runs')").fetchall()}
        versions = dict(connection.execute("SELECT version, description FROM paper_schema_versions").fetchall())
        _assert_official_scheduled_default_false(connection, run_id="default-check")

    # Absence of the column proves these rows predate official scheduling.
    assert rows == [("historical-dry-run", False), ("historical-paper", False)]
    official = columns["official_scheduled"]
    assert official[2].upper() == "BOOLEAN"
    assert official[3] is True
    assert versions[15] == V15_OFFICIAL_SCHEDULE


def test_legacy_null_official_scheduled_fails_closed_without_reclassification(tmp_path):
    path = tmp_path / "legacy-null-official-scheduled.duckdb"
    _seed_legacy_paper_runs_without_official_scheduled(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute("ALTER TABLE paper_runs ADD COLUMN official_scheduled BOOLEAN")
        connection.execute("UPDATE paper_runs SET official_scheduled=NULL WHERE run_id='historical-paper'")

    with pytest.raises(duckdb.ConstraintException):
        _store(path)

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute(
            "SELECT official_scheduled FROM paper_runs WHERE run_id='historical-paper'"
        ).fetchone() == (None,)


def test_official_scheduled_migration_is_idempotent(tmp_path):
    path = tmp_path / "migration-idempotency.duckdb"
    _seed_legacy_paper_runs_without_official_scheduled(path)
    _store(path)
    store = _store(path)
    _store(path)

    with store.connect() as connection:
        rows = connection.execute(
            "SELECT run_id, official_scheduled FROM paper_runs ORDER BY run_id"
        ).fetchall()
        official = next(
            row for row in connection.execute("PRAGMA table_info('paper_runs')").fetchall() if row[1] == "official_scheduled"
        )
        version_count = connection.execute(
            "SELECT COUNT(*) FROM paper_schema_versions WHERE version=15"
        ).fetchone()[0]
        _assert_official_scheduled_default_false(connection, run_id="default-check")

    assert rows == [("historical-dry-run", False), ("historical-paper", False)]
    assert official[3] is True
    assert version_count == 1


def test_reconcile_rejects_controlled_null_official_scheduled_corruption(tmp_path):
    store = _store(tmp_path / "null-reconciliation.duckdb")
    _seed_valid_official_run(store)
    with store.connect() as connection:
        connection.execute("ALTER TABLE paper_runs ALTER COLUMN official_scheduled DROP NOT NULL")
        connection.execute("UPDATE paper_runs SET official_scheduled=NULL WHERE run_id='current'")

    result = store.reconcile()
    assert not result.valid
    assert result.message == "NULL official_scheduled state detected"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("git_commit", "a" * 39),
        ("git_commit", "a" * 41),
        ("git_commit", "A" * 40),
        ("git_commit", "g" * 40),
        ("git_commit", ""),
        ("git_dirty", True),
        ("hardening_manifest_sha256", "b" * 63),
        ("hardening_manifest_sha256", "b" * 65),
        ("hardening_manifest_sha256", "B" * 64),
        ("hardening_manifest_sha256", "g" * 64),
        ("execution_protocol_version", "unknown-protocol"),
    ],
)
def test_reconcile_rejects_persisted_release_provenance_corruption(tmp_path, column, value):
    store = _store(tmp_path / f"provenance-{column}-{len(str(value))}.duckdb")
    _seed_valid_official_run(store)
    with store.connect() as connection:
        connection.execute(f"UPDATE paper_run_release_provenance SET {column}=? WHERE run_id='current'", [value])

    assert not store.reconcile().valid


def test_reconcile_rejects_missing_release_provenance_for_post_adoption_run(tmp_path):
    store = _store(tmp_path / "missing-provenance.duckdb")
    _seed_valid_official_run(store)
    with store.connect() as connection:
        connection.execute("DELETE FROM paper_run_release_provenance WHERE run_id='current'")

    assert not store.reconcile().valid


def test_release_provenance_captured_timestamp_is_persisted_not_null_and_normalized(tmp_path):
    store = _store(tmp_path / "captured-timestamp.duckdb")
    captured = _seed_valid_official_run(store)
    with store.connect() as connection:
        with pytest.raises(duckdb.ConstraintException):
            connection.execute(
                "UPDATE paper_run_release_provenance SET captured_at_utc=NULL WHERE run_id='current'"
            )
        persisted = connection.execute(
            "SELECT captured_at_utc FROM paper_run_release_provenance WHERE run_id='current'"
        ).fetchone()[0]

    assert persisted.tzinfo is not None
    assert persisted == captured
    assert store.reconcile().valid


def test_null_official_scheduled_with_valid_provenance_cannot_bypass_applicability(tmp_path):
    store = _store(tmp_path / "null-bypass.duckdb")
    _seed_valid_official_run(store)
    with store.connect() as connection:
        connection.execute("ALTER TABLE paper_runs ALTER COLUMN official_scheduled DROP NOT NULL")
        connection.execute("UPDATE paper_runs SET official_scheduled=NULL WHERE run_id='current'")
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_runs WHERE mode='PAPER' AND official_scheduled IS NULL"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_run_release_provenance WHERE run_id='current'"
        ).fetchone()[0] == 1

    result = store.reconcile()
    assert not result.valid
    assert result.message == "NULL official_scheduled state detected"
