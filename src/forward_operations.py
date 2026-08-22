"""Operational safety primitives for the forward paper experiment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo

import pandas as pd

from src.paper_broker import PaperConfig, PaperRunResult
from src.paper_store import PaperStore


class AlreadyRunningError(RuntimeError):
    pass


class InterProcessLock:
    """Non-blocking one-byte file lock with same-process protection."""

    _held: set[str] = set()
    _guard = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._handle = None

    def __enter__(self) -> "InterProcessLock":
        key = str(self.path).casefold()
        with self._guard:
            if key in self._held:
                raise AlreadyRunningError(f"Paper process already holds {self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as error:
                handle.close()
                raise AlreadyRunningError(f"Another paper process is active: {self.path}") from error
            self._held.add(key)
            self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        key = str(self.path).casefold()
        with self._guard:
            if self._handle is not None:
                self._handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
                self._handle = None
            self._held.discard(key)


def utc_and_rome_labels(value: datetime | pd.Timestamp) -> dict[str, str]:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    utc_value = timestamp.tz_convert("UTC")
    rome_value = utc_value.tz_convert(ZoneInfo("Europe/Rome"))
    return {"utc": utc_value.isoformat(), "rome": rome_value.isoformat()}


def verify_immutable_manifest(manifest: Path, sidecar: Path) -> str:
    expected_line = sidecar.read_text(encoding="ascii").strip()
    expected = expected_line.split()[0]
    actual = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"Immutable manifest hash mismatch: expected {expected}, got {actual}")
    return actual


def _scheduled_mondays(start: pd.Timestamp, now: pd.Timestamp, config: PaperConfig):
    cursor = start.normalize()
    while cursor.weekday() != config.schedule_weekday:
        cursor += pd.Timedelta(days=1)
    while cursor <= now.normalize():
        window_start = cursor + pd.Timedelta(
            hours=config.schedule_hour, minutes=config.schedule_minute
        )
        window_end = window_start + pd.Timedelta(minutes=config.schedule_window_minutes)
        target = cursor + pd.Timedelta(
            hours=config.schedule_hour,
            minutes=getattr(config, "execution_target_minute", 10),
        )
        if now > window_end:
            yield window_start, target
        cursor += pd.Timedelta(days=7)


def record_missed_windows(
    store: PaperStore,
    *,
    start: datetime | pd.Timestamp,
    now: datetime | pd.Timestamp,
    config: PaperConfig,
) -> int:
    """Record missing weekly windows; never executes or backdates a trade."""
    start_utc = pd.Timestamp(start).tz_convert("UTC")
    now_utc = pd.Timestamp(now).tz_convert("UTC")
    created = 0
    with store.connect() as connection:
        experiment_id = store._active_experiment_id(connection)
        for window_start, target in _scheduled_mondays(start_utc, now_utc, config):
            schedule_key = window_start.strftime("%Y-%m-%dT%H:%MZ")
            seen = connection.execute(
                "SELECT COUNT(*) FROM forward_schedule_windows WHERE schedule_key=?",
                [schedule_key],
            ).fetchone()[0]
            if seen:
                continue
            committed = connection.execute(
                """
                SELECT run_id, status FROM paper_runs
                WHERE schedule_key=? AND status <> 'RUNNING'
                ORDER BY completed_at_utc DESC LIMIT 1
                """,
                [schedule_key],
            ).fetchone()
            if committed is not None:
                run_id, status = committed
                fill_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM paper_fills WHERE run_id=?", [run_id]
                    ).fetchone()[0]
                )
                if status in {"EXECUTED", "RECOVERED_COMMITTED"}:
                    outcome = "PAPER_TRADE_COMPLETED" if fill_count else "NO_REBALANCE"
                elif status in {
                    "DATA_QUALITY_FAILURE",
                    "RECONCILIATION_FAILURE",
                    "KILL_SWITCH_ACTIVATED",
                    "EXECUTION_ERROR",
                }:
                    outcome = status
                else:
                    outcome = "EXECUTION_ERROR"
                connection.execute("BEGIN TRANSACTION")
                connection.execute(
                    "INSERT INTO forward_schedule_windows VALUES (?, ?, ?, ?, ?)",
                    [schedule_key, target, run_id, outcome, now_utc],
                )
                connection.execute(
                    "INSERT OR IGNORE INTO forward_experiment_windows VALUES (?, ?)",
                    [experiment_id, schedule_key],
                )
                connection.execute("COMMIT")
                continue
            incident_id = str(uuid.uuid4())
            try:
                connection.execute("BEGIN TRANSACTION")
                connection.execute(
                    "INSERT INTO forward_schedule_windows VALUES (?, ?, NULL, 'MISSED_SCHEDULE', ?)",
                    [schedule_key, target, now_utc],
                )
                connection.execute(
                    "INSERT INTO forward_incidents VALUES (?, NULL, 'MISSED_SCHEDULE', ?, ?, ?, NULL)",
                    [
                        incident_id,
                        target,
                        "Hermes or host did not complete a run inside the permitted UTC window; no backdated trade was created",
                        now_utc,
                    ],
                )
                connection.execute(
                    "INSERT INTO forward_experiment_windows VALUES (?, ?)",
                    [experiment_id, schedule_key],
                )
                connection.execute(
                    "INSERT INTO forward_experiment_incidents VALUES (?, ?)",
                    [experiment_id, incident_id],
                )
                connection.execute("COMMIT")
                created += 1
            except Exception:
                connection.execute("ROLLBACK")
                raise
    return created


def audit_missed_schedule(
    store: PaperStore,
    *,
    start: datetime | pd.Timestamp,
    now: datetime | pd.Timestamp,
    config: PaperConfig,
) -> PaperRunResult | None:
    """Commit an audit-only MISSED_SCHEDULE run; never fetch or execute."""
    now_utc = pd.Timestamp(now).tz_convert("UTC")
    created = record_missed_windows(store, start=start, now=now_utc, config=config)
    if not created:
        return None
    run_id = "missed_audit_" + now_utc.strftime("%Y%m%dT%H%M%S%fZ")
    reconciliation = store.reconcile()
    store.insert_run(
        run_id=run_id,
        started_at=now_utc.to_pydatetime(),
        mode="AUDIT_ONLY",
        schedule_key=None,
        signal_timestamp=None,
        data_timestamp=None,
    )
    store.finish_run(
        run_id=run_id,
        status="MISSED_SCHEDULE",
        completed_at=now_utc.to_pydatetime(),
        message="Missed UTC window recorded; no market fetch and no backdated trade",
        reconciliation=reconciliation,
    )
    window_start = now_utc.normalize() + pd.Timedelta(
        hours=config.schedule_hour, minutes=config.schedule_minute
    )
    target = now_utc.normalize() + pd.Timedelta(
        hours=config.schedule_hour, minutes=config.execution_target_minute
    )
    store.record_forward_window(
        schedule_key=window_start.strftime("%Y-%m-%dT%H:%MZ"),
        scheduled_for=target.to_pydatetime(),
        run_id=run_id,
        outcome="MISSED_SCHEDULE",
        now=now_utc.to_pydatetime(),
    )
    return PaperRunResult(
        run_id,
        "MISSED_SCHEDULE",
        "Missed UTC window recorded; no market fetch and no backdated trade",
        outcome="MISSED_SCHEDULE",
    )
