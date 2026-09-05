from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

import duckdb
import pytest

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.paper_store import PaperStore


V5_EXECUTION = "versioned ask-bid execution context"
V12_QUOTE = "quote coherence provenance and legacy v5 normalization"
V13_RELEASE = "per-forward-run release provenance"
V14_ATTEMPT = "retryable forward admission attempt schedule identity"


def _store(path: Path) -> PaperStore:
    return PaperStore(path, account_id="locked_strategy", initial_cash=2_000.0)


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

    assert set(versions) == set(range(2, 15))
    assert versions[5] == V5_EXECUTION
    assert versions[12] == V12_QUOTE
    assert versions[13] == V13_RELEASE
    assert versions[14] == V14_ATTEMPT
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
            assert set(dict(runtime.execute("SELECT version, description FROM paper_schema_versions").fetchall())) == set(range(2, 15))
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
