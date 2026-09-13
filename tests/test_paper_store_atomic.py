from datetime import datetime, timezone

import pytest

from src.paper_store import PaperStore, ReconciliationResult


def _store(tmp_path) -> PaperStore:
    store = PaperStore(
        tmp_path / "paper.duckdb",
        account_id="locked_strategy",
        initial_cash=2_000.0,
    )
    now = datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs (run_id, started_at_utc, status, mode) "
            "VALUES ('run-1', ?, 'RUNNING', 'PAPER')",
            [now],
        )
    return store


def _finish(store: PaperStore, outcome: str | None = "FULL_EXECUTION") -> None:
    store.finish_run(
        run_id="run-1",
        status="EXECUTED",
        completed_at=datetime(2026, 8, 24, 9, 10, tzinfo=timezone.utc),
        message="terminal",
        reconciliation=ReconciliationResult(True, "valid"),
        execution_outcome=outcome,
    )


def test_finish_run_commits_terminal_state_and_outcome_together(tmp_path):
    store = _store(tmp_path)

    _finish(store)

    with store.connect(read_only=True) as connection:
        run = connection.execute(
            "SELECT status, completed_at_utc, message FROM paper_runs WHERE run_id='run-1'"
        ).fetchone()
        outcome = connection.execute(
            "SELECT execution_outcome FROM paper_execution_outcomes WHERE run_id='run-1'"
        ).fetchone()
    assert run[0] == "EXECUTED"
    assert run[1] is not None
    assert run[2] == "terminal"
    assert outcome == ("FULL_EXECUTION",)


def test_finish_run_rolls_back_terminal_update_when_outcome_write_fails(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def fail_outcome_write(*_args, **_kwargs):
        raise RuntimeError("injected outcome failure")

    monkeypatch.setattr(store, "_write_execution_outcome", fail_outcome_write)
    with pytest.raises(RuntimeError, match="injected outcome failure"):
        _finish(store)

    with store.connect(read_only=True) as connection:
        run = connection.execute(
            "SELECT status, completed_at_utc, message FROM paper_runs WHERE run_id='run-1'"
        ).fetchone()
        outcomes = connection.execute(
            "SELECT COUNT(*) FROM paper_execution_outcomes WHERE run_id='run-1'"
        ).fetchone()
    assert run == ("RUNNING", None, None)
    assert outcomes == (0,)


def test_finish_run_is_retryable_without_duplicate_outcome_after_rollback(tmp_path, monkeypatch):
    store = _store(tmp_path)
    original = store._write_execution_outcome
    monkeypatch.setattr(
        store,
        "_write_execution_outcome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        _finish(store)
    monkeypatch.setattr(store, "_write_execution_outcome", original)

    _finish(store, "NO_REBALANCE_REQUIRED")

    with store.connect(read_only=True) as connection:
        run = connection.execute(
            "SELECT status FROM paper_runs WHERE run_id='run-1'"
        ).fetchone()
        outcomes = connection.execute(
            "SELECT execution_outcome FROM paper_execution_outcomes WHERE run_id='run-1'"
        ).fetchall()
    assert run == ("EXECUTED",)
    assert outcomes == [("NO_REBALANCE_REQUIRED",)]


def test_finish_run_without_outcome_remains_valid_terminal_path(tmp_path):
    store = _store(tmp_path)

    _finish(store, None)

    with store.connect(read_only=True) as connection:
        run = connection.execute(
            "SELECT status FROM paper_runs WHERE run_id='run-1'"
        ).fetchone()
        outcomes = connection.execute(
            "SELECT COUNT(*) FROM paper_execution_outcomes WHERE run_id='run-1'"
        ).fetchone()
    assert run == ("EXECUTED",)
    assert outcomes == (0,)
