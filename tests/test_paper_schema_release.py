from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.paper_store import PaperStore


V5_EXECUTION = "versioned ask-bid execution context"
V12_QUOTE = "quote coherence provenance and legacy v5 normalization"
V13_RELEASE = "per-forward-run release provenance"


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

    assert set(versions) == set(range(2, 14))
    assert versions[5] == V5_EXECUTION
    assert versions[12] == V12_QUOTE
    assert versions[13] == V13_RELEASE
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


def test_legacy_v5_meanings_are_preserved_while_v12_and_v13_are_added(tmp_path):
    for description in ("quote coherence contract provenance", V5_EXECUTION):
        path = tmp_path / f"legacy-{description[:5]}.duckdb"
        store = _store(path)
        with store.connect() as connection:
            connection.execute(
                "UPDATE paper_schema_versions SET description=? WHERE version=5", [description]
            )
            connection.execute("DELETE FROM paper_schema_versions WHERE version IN (12, 13)")

        migrated = _store(path)
        with migrated.connect(read_only=True) as connection:
            versions = dict(connection.execute("SELECT version, description FROM paper_schema_versions").fetchall())

        assert versions[5] == description
        assert versions[12] == V12_QUOTE
        assert versions[13] == V13_RELEASE


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
            "INSERT INTO paper_runs VALUES "
            "('current', ?, ?, 'EXECUTED', 'PAPER', '2026-09-07T00:05Z', NULL, NULL, NULL, '{}')",
            [now, now],
        )
        connection.execute(
            "INSERT INTO paper_runs VALUES "
            "('historical', '2000-01-01T00:00:00Z', '2000-01-01T00:01:00Z', "
            "'EXECUTED', 'PAPER', '2000-01-03T00:05Z', NULL, NULL, NULL, '{}')"
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
