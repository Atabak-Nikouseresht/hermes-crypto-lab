"""Prospective v2 clock and offline semantic evidence contract."""
from dataclasses import replace
import hashlib
import json

import pandas as pd
import pytest

from src.paper_broker import validate_reference_price_evidence
from tests.test_market_rule_integrity import _current_run, _refresh_rule_digest, _system, _rules


MUTATIONS = [
    "reference_price_acquired_at_utc=NULL",
    "reference_price_acquired_at_utc=reference_price_timestamp_utc-INTERVAL 1 SECOND",
    "reference_price_acquired_at_utc=captured_at_utc+INTERVAL 1 SECOND",
    "reference_price_timestamp_utc=captured_at_utc-INTERVAL 301 SECOND",
    "reference_price_timestamp_utc=captured_at_utc-INTERVAL 301 SECOND, reference_price_acquired_at_utc=captured_at_utc-INTERVAL 300 SECOND",
    "reference_price_source='LAST_FALLBACK'",
    "captured_at_utc=captured_at_utc-INTERVAL 1 SECOND",
    "contract_version='binance-market-rule-evidence-v1', reference_price_acquired_at_utc=NULL",
    "reference_price_timestamp_utc=NULL",
    "reference_price_timestamp_utc=reference_price_acquired_at_utc+INTERVAL 1 SECOND",
    "contract_version='binance-market-rule-evidence-v1'",
]


@pytest.mark.parametrize('mutation', MUTATIONS)
@pytest.mark.parametrize('quantity', [1.0, None])
def test_v2_semantic_tamper_after_refreshing_digest(tmp_path, mutation, quantity):
    system = _current_run(tmp_path, quantity=quantity)
    with system.store.connect() as connection:
        connection.execute('UPDATE paper_market_rule_evidence SET ' + mutation)
    _refresh_rule_digest(system)
    before = hashlib.sha256(system.store.path.read_bytes()).hexdigest()
    result = system.store.reconcile()
    assert not result.valid, result.message
    assert 'digest' not in result.message.lower()
    assert hashlib.sha256(system.store.path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('remove_marker', [False, True])
def test_v2_boundary_blocks_coordinated_downgrade(tmp_path, remove_marker):
    system = _current_run(tmp_path, quantity=None)
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_schema_versions SET applied_at_utc='2024-01-01' WHERE version=17")
        connection.execute("UPDATE paper_market_rule_evidence SET contract_version='binance-market-rule-evidence-v1', reference_price_acquired_at_utc=NULL")
        if remove_marker:
            connection.execute('UPDATE paper_runs SET market_rule_evidence_version=1, market_rule_evidence_required=FALSE')
    _refresh_rule_digest(system)
    assert not system.store.reconcile().valid


@pytest.mark.parametrize('average', [0, 5])
def test_v2_nonreference_sources_have_no_reference_timing(tmp_path, average):
    system = _current_run(tmp_path, rules=_rules(average=average), reference=None, quantity=None)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute('SELECT reference_price_timestamp_utc, reference_price_acquired_at_utc FROM paper_market_rule_evidence').fetchall() == [(None, None), (None, None)]
        expected = 'LAST_FALLBACK' if average == 0 else 'UNVERIFIABLE_AVERAGE'
        assert connection.execute('SELECT DISTINCT reference_price_source FROM paper_market_rule_evidence').fetchall() == [(expected,)]
    with system.store.connect() as connection:
        connection.execute('UPDATE paper_market_rule_evidence SET reference_price_acquired_at_utc=captured_at_utc')
    _refresh_rule_digest(system)
    assert not system.store.reconcile().valid


@pytest.mark.parametrize('quantity', [1.0, None])
def test_v1_actual_pre17_schema_migrates_without_rewriting_digest(tmp_path, quantity):
    from src.paper_store import PaperStore
    system = _current_run(tmp_path, quantity=quantity)
    with system.store.connect() as connection:
        # V1's historical contract did not reconstruct timestamp freshness.
        connection.execute("UPDATE paper_market_rule_evidence SET contract_version='binance-market-rule-evidence-v1', reference_price_acquired_at_utc=NULL, reference_price_timestamp_utc=captured_at_utc-INTERVAL 1 DAY")
        old_orders = connection.execute('SELECT * FROM paper_orders ORDER BY order_id').fetchall()
        old_fills = connection.execute('SELECT * FROM paper_fills ORDER BY fill_id').fetchall()
        connection.execute('ALTER TABLE paper_market_rule_evidence DROP COLUMN reference_price_acquired_at_utc')
        connection.execute('ALTER TABLE paper_runs DROP COLUMN market_rule_evidence_version')
        connection.execute('DELETE FROM paper_schema_versions WHERE version=17')
        old_rows = connection.execute('SELECT * FROM paper_market_rule_evidence ORDER BY symbol').fetchall()
        # Calculate the original serializer independently, not the v2 helper.
        old_digest = hashlib.sha256(json.dumps([[None if v is None else str(v) for v in row] for row in old_rows], separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        diagnostics = json.loads(connection.execute('SELECT diagnostics FROM paper_forward_execution_evidence').fetchone()[0])
        diagnostics['market_rule_evidence_sha256'] = old_digest
        connection.execute('UPDATE paper_forward_execution_evidence SET diagnostics=?', [json.dumps(diagnostics)])
    for _ in range(2):
        store = PaperStore(system.store.path, account_id=system.config.account_id, initial_cash=1000, fee_rate=0, minimum_spread_rate=0, slippage_rate=0)
        assert store.reconcile().valid
        with store.connect(read_only=True) as connection:
            rows = connection.execute('SELECT * FROM paper_market_rule_evidence ORDER BY symbol').fetchall()
            assert [r[:21] for r in rows] == old_rows
            assert all(r[21] is None for r in rows)
            assert store.market_rule_evidence_digest(rows) == old_digest
            assert connection.execute('SELECT * FROM paper_orders ORDER BY order_id').fetchall() == old_orders
            assert connection.execute('SELECT * FROM paper_fills ORDER BY fill_id').fetchall() == old_fills
            assert connection.execute('SELECT market_rule_evidence_version FROM paper_runs').fetchone() == (1,)
            info = {r[1]: r for r in connection.execute("PRAGMA table_info('paper_market_rule_evidence')").fetchall()}
            assert info['reference_price_acquired_at_utc'][2:5] == ('TIMESTAMP WITH TIME ZONE', False, None)
            assert connection.execute('SELECT count(*) FROM paper_schema_versions WHERE version=17').fetchone() == (1,)


def test_v2_persists_distinct_acquisition_and_admission(tmp_path):
    system = _current_run(tmp_path, quantity=None, source_age=300, acquisition_age=150)
    with system.store.connect(read_only=True) as connection:
        row = connection.execute("SELECT reference_price_acquired_at_utc, captured_at_utc FROM paper_market_rule_evidence LIMIT 1").fetchone()
        assert row[1] - row[0] == pd.Timedelta(seconds=150)
        assert connection.execute("SELECT market_rule_evidence_version FROM paper_runs").fetchone() == (2,)


@pytest.mark.parametrize('source_age,acquisition_age,valid', [(300, 0, True), (300.001, 1, False), (299, 1, True), (301, 300, False), (0, -1, False), (0, 1, False), (0, 0, True)])
def test_final_admission_zero_orders_enforces_v2_clocks(tmp_path, source_age, acquisition_age, valid):
    if valid:
        assert _current_run(tmp_path, quantity=None, source_age=source_age, acquisition_age=acquisition_age).store.reconcile().valid
    else:
        with pytest.raises(ValueError, match='reference price'):
            _current_run(tmp_path, quantity=None, source_age=source_age, acquisition_age=acquisition_age)


@pytest.mark.parametrize('age,valid', [(300, True), (301, False), (-1, False)])
def test_clock6_final_reference_age(tmp_path, age, valid):
    system, snapshot, now = _system(tmp_path)
    reference = replace(snapshot.rule_reference_prices['BTC/USDT'], timestamp=now-pd.Timedelta(seconds=age), acquired_at=now)
    assert (validate_reference_price_evidence(reference, now=now, max_age_minutes=5) is None) == valid


def test_clock7_acquisition_cannot_be_fabricated(tmp_path):
    _, snapshot, now = _system(tmp_path)
    reference = replace(snapshot.rule_reference_prices['BTC/USDT'], acquired_at=None)
    assert validate_reference_price_evidence(reference, now=now, max_age_minutes=5) is not None


def test_REC7_acquisition_change_without_digest_refresh_is_detected(tmp_path):
    system = _current_run(tmp_path, source_age=2, acquisition_age=1)
    with system.store.connect() as connection:
        connection.execute('UPDATE paper_market_rule_evidence SET reference_price_acquired_at_utc=reference_price_acquired_at_utc+INTERVAL 100 MILLISECOND')
    result = system.store.reconcile()
    assert not result.valid
    assert 'digest mismatch' in result.message
