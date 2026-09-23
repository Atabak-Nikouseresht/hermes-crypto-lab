"""Batch1 schema admission must precede every mutable initialization step."""
import hashlib

import duckdb
import pytest

from src.paper_store import PaperStore


def test_future_schema_rejected_before_any_writable_connect(tmp_path, monkeypatch):
    path = tmp_path / "future.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE paper_schema_versions(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO paper_schema_versions VALUES (18), (999)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    real_connect = duckdb.connect
    def guarded_connect(*args, **kwargs):
        assert kwargs.get("read_only") is True, "future schema reached writable connect"
        return real_connect(*args, **kwargs)
    monkeypatch.setattr(duckdb, "connect", guarded_connect)
    with pytest.raises(ValueError, match="[Ff]uture|[Nn]ewer|[Uu]nsupported"):
        PaperStore(path, account_id="test", initial_cash=100)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("entry", ["diagnostic", "read_only", "reconcile", "existing_reconcile"])
def test_future_schema_inspection_rejected_without_mutation(tmp_path, entry):
    path = tmp_path / "future.duckdb"
    store = PaperStore(path, account_id="test", initial_cash=100)
    with store.connect() as connection:
        connection.execute("INSERT INTO paper_schema_versions VALUES (999, now(), 'future')")
    before = path.read_bytes()
    settings = dict(account_id="test", quantity_tolerance=1e-7, fee_rate=.001,
                    minimum_spread_rate=.0005, slippage_rate=.0005,
                    max_quote_timestamp_skew_seconds=30)
    with pytest.raises(ValueError, match="Unsupported future"):
        if entry == "diagnostic":
            PaperStore.open_diagnostic_read_only(path)
        elif entry == "read_only":
            PaperStore.open_existing_read_only(path, **settings)
        elif entry == "reconcile":
            PaperStore.reconcile_database(path, **settings)
        else:
            store.reconcile()
    assert path.read_bytes() == before


def test_schema19_migrates_actual_notification_v18_shape_without_backfill(tmp_path):
    from src.paper_store import CURRENT_SCHEMA_VERSION
    path = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("""CREATE TABLE paper_notifications (
            run_id VARCHAR PRIMARY KEY, target VARCHAR NOT NULL, report_path VARCHAR NOT NULL,
            status VARCHAR NOT NULL, attempt_count INTEGER NOT NULL, last_error VARCHAR,
            created_at_utc TIMESTAMPTZ NOT NULL, updated_at_utc TIMESTAMPTZ NOT NULL,
            delivered_at_utc TIMESTAMPTZ);
            CREATE TABLE notification_attempts (attempt_id VARCHAR PRIMARY KEY,
            run_id VARCHAR NOT NULL, attempted_at_utc TIMESTAMPTZ NOT NULL,
            status VARCHAR NOT NULL, error VARCHAR);
            CREATE TABLE paper_schema_versions(version INTEGER PRIMARY KEY,
            applied_at_utc TIMESTAMPTZ NOT NULL, description VARCHAR NOT NULL);
            INSERT INTO paper_schema_versions VALUES (18, now(), 'prospective Binance executionRules PRICE_RANGE evidence');
            INSERT INTO paper_notifications VALUES ('old', 'tg:old', 'old.md', 'FAILED', 1,
            'preserved', now(), now(), NULL);
            INSERT INTO notification_attempts VALUES ('original', 'old', now(), 'FAILED', 'preserved');
        """)
        original = connection.execute("SELECT * FROM paper_notifications").fetchone()
        original_attempts = connection.execute("SELECT * FROM notification_attempts").fetchall()
    assert CURRENT_SCHEMA_VERSION == 19
    for _ in range(2):
        store = PaperStore(path, account_id="test", initial_cash=100)
        with store.connect(read_only=True) as connection:
            migrated = connection.execute("SELECT * FROM paper_notifications").fetchone()
            assert migrated[:9] == original
            assert migrated[9:] == (None, 'PAPER')
            assert connection.execute("SELECT * FROM notification_attempts").fetchall() == original_attempts
            assert connection.execute("SELECT COUNT(*) FROM notification_audit_events").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM paper_schema_versions WHERE version=19").fetchone() == (1,)
            columns = {row[1]: row for row in connection.execute("PRAGMA table_info('paper_notifications')").fetchall()}
            assert columns['report_sha256'][2:5] == ('VARCHAR', False, None)
            assert columns['notification_kind'][2:5] == ('VARCHAR', True, "'PAPER'")


def test_notification_opener_never_initializes_or_recovers(tmp_path, monkeypatch):
    path = tmp_path / 'paper.duckdb'
    store = PaperStore(path, account_id='test', initial_cash=100)
    with store.connect() as connection:
        connection.execute("INSERT INTO paper_runs(run_id, started_at_utc, status, mode) VALUES ('stranded', now(), 'RUNNING', 'PAPER')")
        connection.execute("UPDATE paper_accounts SET cash=77")
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('notification-only opener invoked initialization/recovery')
    monkeypatch.setattr(PaperStore, '__init__', forbidden)
    monkeypatch.setattr(PaperStore, '_initialize', forbidden)
    monkeypatch.setattr(PaperStore, 'recover_abandoned_runs', forbidden)
    notifications = PaperStore.open_notification_store(path)
    assert notifications.path == path
    assert path.read_bytes() == before
    with notifications.connect() as connection:
        connection.execute("INSERT INTO notification_attempts VALUES ('a', 'n', now(), 'FAILED', 'known')")
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT status FROM paper_runs").fetchone() == ('RUNNING',)
        assert connection.execute("SELECT cash FROM paper_accounts").fetchone() == (77,)


@pytest.mark.parametrize('shape', ['missing', 'legacy', 'future', 'missing_hash', 'missing_audit', 'wrong_hash_type'])
def test_notification_opener_fails_closed_without_mutation(tmp_path, shape):
    path = tmp_path / 'nested' / 'paper.duckdb'
    if shape != 'missing':
        store = PaperStore(path, account_id='test', initial_cash=100)
        with store.connect() as connection:
            if shape == 'legacy':
                connection.execute('DELETE FROM paper_schema_versions WHERE version=19')
            elif shape == 'future':
                connection.execute("INSERT INTO paper_schema_versions VALUES (999, now(), 'future')")
            elif shape == 'missing_hash':
                connection.execute('ALTER TABLE paper_notifications DROP report_sha256')
            elif shape == 'wrong_hash_type':
                connection.execute('ALTER TABLE paper_notifications ALTER report_sha256 TYPE INTEGER')
            else:
                connection.execute('DROP TABLE notification_audit_events')
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((ValueError, FileNotFoundError)):
        PaperStore.open_notification_store(path)
    assert (path.read_bytes() if path.exists() else None) == before
    if shape == 'missing':
        assert not path.parent.exists()
