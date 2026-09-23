"""Prospective report integrity and conservative delivery recovery."""
import hashlib
import subprocess

import pytest

from src.paper_notifications import (
    HermesTelegramSender,
    NotificationError,
    NotificationService,
)
from src.paper_store import PaperStore


@pytest.fixture
def notification(tmp_path):
    store = PaperStore(tmp_path / 'paper.duckdb', account_id='test', initial_cash=100)
    with store.connect() as connection:
        connection.execute("INSERT INTO paper_runs(run_id, started_at_utc, completed_at_utc, status, mode) VALUES ('run', now(), now(), 'EXECUTED', 'PAPER')")
    report = tmp_path / 'report.md'
    report.write_bytes(b'report\r\nexact bytes\n')
    calls = []
    service = NotificationService(store, target='telegram:original', sender=lambda *args: calls.append(args) or {'ok': True})
    return store, report, service, calls


def test_registration_seals_exact_report_bytes_and_initial_send_checks_seal(notification):
    store, report, service, calls = notification
    original = report.read_bytes()
    service.register_pending('run', report)
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT report_sha256, notification_kind FROM paper_notifications").fetchone() == (hashlib.sha256(original).hexdigest(), 'PAPER')
    report.write_bytes(original.replace(b'\r\n', b'\n'))
    with pytest.raises(NotificationError, match='hash|integrity|SHA'):
        service.send_committed_run('run', report)
    assert calls == []
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT status, attempt_count FROM paper_notifications").fetchone() == ('PENDING', 0)
        assert connection.execute("SELECT COUNT(*) FROM notification_attempts").fetchone() == (0,)
    report.write_bytes(original)
    assert service.send_committed_run('run', report) == {'ok': True}
    assert calls == [('telegram:original', report.resolve())]


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('hermes', 60), TimeoutError('delivery timeout'), KeyboardInterrupt(), SystemExit(2)])
def test_interrupted_sender_is_durably_unknown_and_never_auto_retried(notification, failure):
    store, report, service, calls = notification
    def interrupted(*args):
        calls.append(args)
        raise failure
    service.sender = interrupted
    expected = NotificationError if isinstance(failure, Exception) else type(failure)
    with pytest.raises(expected):
        service.send_committed_run('run', report)
    with store.connect(read_only=True) as connection:
        assert connection.execute('SELECT status, attempt_count FROM paper_notifications').fetchone() == ('DELIVERY_UNKNOWN', 1)
        assert connection.execute('SELECT status FROM notification_attempts').fetchone() == ('DELIVERY_UNKNOWN',)
    with pytest.raises(NotificationError, match='manual recovery'):
        service.resend('run')
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("returncode", "expected_status"),
    [(1, "DELIVERY_UNKNOWN"), (2, "FAILED")],
)
def test_hermes_send_exit_codes_preserve_delivery_safety(
    notification, monkeypatch, returncode, expected_status
):
    store, report, service, _calls = notification
    sender_calls = []

    def failed_backend(args, **kwargs):
        sender_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, returncode, stdout="", stderr="backend error"
        )

    monkeypatch.setattr(subprocess, "run", failed_backend)
    service.sender = HermesTelegramSender()

    with pytest.raises(NotificationError):
        service.send_committed_run("run", report)

    with store.connect(read_only=True) as connection:
        assert connection.execute(
            "SELECT status, attempt_count FROM paper_notifications WHERE run_id='run'"
        ).fetchone() == (expected_status, 1)
    if expected_status == "DELIVERY_UNKNOWN":
        with store.connect(read_only=True) as connection:
            assert connection.execute(
                "SELECT status FROM notification_attempts WHERE run_id='run'"
            ).fetchone() == ("DELIVERY_UNKNOWN",)
        with pytest.raises(NotificationError, match="manual recovery"):
            service.resend("run")
        assert len(sender_calls) == 1
    else:
        with pytest.raises(NotificationError):
            service.resend("run")
        with store.connect(read_only=True) as connection:
            assert connection.execute(
                "SELECT status, attempt_count FROM paper_notifications WHERE run_id='run'"
            ).fetchone() == ("FAILED", 2)
        assert len(sender_calls) == 2


@pytest.mark.parametrize('resolution,expected', [('unknown', 'DELIVERY_UNKNOWN'), ('delivered', 'DELIVERED'), ('not-delivered', 'FAILED')])
def test_manual_resolution_preserves_attempt_history_and_audits_operator(notification, resolution, expected):
    store, report, service, calls = notification
    service.register_pending('run', report)
    attempt_id = service._claim_attempt('run')
    with store.connect(read_only=True) as connection:
        original = connection.execute(
            'SELECT attempt_id, run_id, attempted_at_utc FROM notification_attempts'
        ).fetchall()
    service.recover('run', resolution=resolution, operator='alice', reason='provider log reviewed')
    with store.connect(read_only=True) as connection:
        attempt = connection.execute(
            'SELECT attempt_id, run_id, attempted_at_utc, status FROM notification_attempts'
        ).fetchone()
        assert attempt[:3] == original[0]
        assert attempt[3] == expected
        assert connection.execute('SELECT status, attempt_count FROM paper_notifications').fetchone() == (expected, 1)
        assert connection.execute('SELECT run_id, attempt_id, resolution, operator, reason, previous_status, new_status FROM notification_audit_events').fetchone() == ('run', attempt_id, resolution, 'alice', 'provider log reviewed', 'SENDING', expected)
    assert calls == []
    if resolution == 'not-delivered':
        service.resend('run')
        with store.connect(read_only=True) as connection:
            assert connection.execute(
                'SELECT attempt_id, run_id, attempted_at_utc FROM notification_attempts WHERE attempt_id=?',
                [attempt_id],
            ).fetchall() == original
            assert connection.execute('SELECT status, attempt_count FROM paper_notifications').fetchone() == ('DELIVERED', 2)
    else:
        with pytest.raises(NotificationError):
            service.resend('run')


@pytest.mark.parametrize('operator,reason', [('', 'investigated'), ('alice', ''), ('  ', 'investigated'), ('alice', ' \n ')])
def test_manual_resolution_requires_operator_and_reason(notification, operator, reason):
    store, report, service, calls = notification
    service.register_pending('run', report)
    service._claim_attempt('run')
    before = store.path.read_bytes()
    with pytest.raises(NotificationError, match='operator|reason'):
        service.recover('run', resolution='unknown', operator=operator, reason=reason)
    assert store.path.read_bytes() == before


def test_manual_recovery_after_interruption_retains_attempt_identity(notification):
    store, report, service, _calls = notification
    service.sender = lambda *_args: (_ for _ in ()).throw(TimeoutError('delivery outcome unknown'))
    with pytest.raises(NotificationError):
        service.send_committed_run('run', report)
    with store.connect(read_only=True) as connection:
        attempt_id = connection.execute(
            'SELECT attempt_id FROM notification_attempts WHERE run_id=?', ['run']
        ).fetchone()[0]

    service.recover(
        'run', resolution='not-delivered', operator='alice', reason='provider logs confirm no delivery'
    )

    with store.connect(read_only=True) as connection:
        assert connection.execute(
            'SELECT status FROM notification_attempts WHERE attempt_id=?', [attempt_id]
        ).fetchone() == ('FAILED',)
        assert connection.execute(
            'SELECT attempt_id, previous_status, new_status FROM notification_audit_events'
        ).fetchone() == (attempt_id, 'DELIVERY_UNKNOWN', 'FAILED')


def test_legacy_notification_without_hash_is_not_backfilled_or_sent(notification):
    store, report, service, calls = notification
    service.sender = lambda *args: calls.append(args)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_notifications "
            "(run_id, target, report_path, status, attempt_count, created_at_utc, updated_at_utc) "
            "VALUES ('run', 'telegram:legacy', ?, 'FAILED', 1, now(), now())",
            [str(report.resolve())],
        )

    with pytest.raises(NotificationError, match='historical report hash'):
        service.resend('run')

    with store.connect(read_only=True) as connection:
        assert connection.execute(
            'SELECT status, report_sha256 FROM paper_notifications WHERE run_id=?', ['run']
        ).fetchone() == ('FAILED', None)
        assert connection.execute(
            'SELECT COUNT(*) FROM notification_attempts WHERE run_id=?', ['run']
        ).fetchone() == (0,)
    assert calls == []
