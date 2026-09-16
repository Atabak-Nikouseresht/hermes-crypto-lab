from decimal import Decimal
import json

import pandas as pd
import pytest

from src.paper_broker import (
    PriceRangeRuleEvidence,
    RuleReferencePrice,
    evaluate_price_range_rule,
    validate_price_range_evidence,
)
from src.paper_store import ReconciliationResult
from src.paper_market import parse_binance_price_range_execution_rule
from src.release_provenance import ReleaseProvenance
from tests.test_market_rule_integrity import NOW as RUN_NOW, SYMBOLS, _rules
from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem, Quote


NOW = pd.Timestamp("2026-09-14T00:10:00Z")
REFERENCE = RuleReferencePrice(Decimal("100"), "REFERENCE_PRICE", NOW, NOW)


def _rule(**overrides):
    values = {
        "symbol": "BTC/USDT",
        "native_symbol": "BTCUSDT",
        "status": "PRICE_RANGE_PRESENT",
        "price_range_present": True,
        "bid_limit_mult_up": Decimal("1.05"),
        "bid_limit_mult_down": Decimal("0.95"),
        "ask_limit_mult_up": Decimal("1.04"),
        "ask_limit_mult_down": Decimal("0.96"),
        "source_timestamp": NOW,
        "acquired_at": NOW,
    }
    values.update(overrides)
    return PriceRangeRuleEvidence(**values)


@pytest.mark.parametrize(
    "side,price,expected",
    [
        ("BUY", "95", "ACCEPTED"),
        ("BUY", "105", "ACCEPTED"),
        ("SELL", "96", "ACCEPTED"),
        ("SELL", "104", "ACCEPTED"),
        ("BUY", "94.9999999999999999999999999999", "REJECTED"),
        ("BUY", "105.0000000000000000000000000001", "REJECTED"),
        ("SELL", "95.9999999999999999999999999999", "REJECTED"),
        ("SELL", "104.0000000000000000000000000001", "REJECTED"),
    ],
)
def test_price_range_boundaries_are_side_specific_and_inclusive(side, price, expected):
    decision = evaluate_price_range_rule(
        _rule(), reference=REFERENCE, side=side, simulated_execution_price=Decimal(price)
    )

    assert decision.outcome == expected
    assert decision.reason == (
        None if expected == "ACCEPTED" else "EXECUTION_RULE_PRICE_RANGE_EXCEEDED"
    )


@pytest.mark.parametrize(
    "field,side",
    [
        ("bid_limit_mult_up", "BUY"),
        ("bid_limit_mult_down", "BUY"),
        ("ask_limit_mult_up", "SELL"),
        ("ask_limit_mult_down", "SELL"),
    ],
)
def test_missing_individual_multiplier_disables_only_its_corresponding_bound(field, side):
    decision = evaluate_price_range_rule(
        _rule(**{field: None}),
        reference=REFERENCE,
        side=side,
        simulated_execution_price=Decimal("1000" if field.endswith("up") else "0.001"),
    )

    assert decision.outcome == "ACCEPTED"


@pytest.mark.parametrize(
    "rule,reference,expected",
    [
        (_rule(status="PRICE_RANGE_ABSENT", price_range_present=False), REFERENCE, "NOT_ENFORCED"),
        (_rule(), RuleReferencePrice(None, "LAST_FALLBACK"), "NOT_ENFORCED"),
        (_rule(), RuleReferencePrice(None, "UNVERIFIABLE_AVERAGE"), "NOT_ENFORCED"),
        (_rule(status="TRANSPORT_FAILURE", price_range_present=None), REFERENCE, "EVIDENCE_UNAVAILABLE"),
        (_rule(status="MALFORMED_RESPONSE", price_range_present=None), REFERENCE, "EVIDENCE_UNAVAILABLE"),
        (_rule(status="RULE_NOT_APPLICABLE", price_range_present=None), REFERENCE, "NOT_ENFORCED"),
    ],
)
def test_price_range_preserves_documented_absence_and_endpoint_failure_semantics(
    rule, reference, expected
):
    decision = evaluate_price_range_rule(
        rule, reference=reference, side="BUY", simulated_execution_price=Decimal("100")
    )

    assert decision.outcome == expected


def test_price_range_rejects_invalid_side_and_nonfinite_simulated_price():
    with pytest.raises(ValueError):
        evaluate_price_range_rule(
            _rule(), reference=REFERENCE, side="HOLD", simulated_execution_price=Decimal("100")
        )
    with pytest.raises(ValueError):
        evaluate_price_range_rule(
            _rule(), reference=REFERENCE, side="BUY", simulated_execution_price=Decimal("NaN")
        )


def test_price_range_rejects_unbounded_decimal_evidence_before_arithmetic():
    payload = {
        "timestamp": int(NOW.timestamp() * 1000),
        "symbolRules": [{
            "symbol": "BTCUSDT",
            "rules": [{"ruleType": "PRICE_RANGE", "bidLimitMultUp": "1e999999"}],
        }],
    }
    with pytest.raises(ValueError, match="PRICE_RANGE multiplier"):
        parse_binance_price_range_execution_rule(
            symbol="BTC/USDT", native_symbol="BTCUSDT", payload=payload, acquired_at=NOW
        )
    with pytest.raises(ValueError, match="PRICE_RANGE"):
        evaluate_price_range_rule(
            _rule(bid_limit_mult_up=Decimal("1e999999")),
            reference=REFERENCE,
            side="BUY",
            simulated_execution_price=Decimal("100"),
        )
    with pytest.raises(ValueError, match="PRICE_RANGE"):
        evaluate_price_range_rule(
            _rule(),
            reference=RuleReferencePrice(Decimal("1e999999"), "REFERENCE_PRICE", NOW, NOW),
            side="BUY",
            simulated_execution_price=Decimal("100"),
        )


@pytest.mark.parametrize(
    "rule,now,valid",
    [
        (_rule(source_timestamp=NOW, acquired_at=NOW), NOW, True),
        (_rule(source_timestamp=NOW - pd.Timedelta(minutes=5), acquired_at=NOW), NOW, True),
        (_rule(source_timestamp=NOW - pd.Timedelta(minutes=5, milliseconds=1), acquired_at=NOW), NOW, False),
        (_rule(source_timestamp=NOW + pd.Timedelta(milliseconds=1), acquired_at=NOW), NOW, False),
        (_rule(source_timestamp=NOW, acquired_at=NOW + pd.Timedelta(milliseconds=1)), NOW, False),
    ],
)
def test_price_range_evidence_validates_persisted_source_and_acquisition_clocks(rule, now, valid):
    assert (validate_price_range_evidence(rule, now=now, max_age_minutes=5) is None) is valid


@pytest.mark.parametrize("status", ["TRANSPORT_FAILURE", "MALFORMED_RESPONSE"])
def test_price_range_failure_evidence_retains_receipt_without_becoming_documented_absence(status):
    evidence = _rule(
        status=status,
        price_range_present=None,
        bid_limit_mult_up=None,
        bid_limit_mult_down=None,
        ask_limit_mult_up=None,
        ask_limit_mult_down=None,
        source_timestamp=None,
        acquired_at=NOW,
    )
    assert validate_price_range_evidence(evidence, now=NOW, max_age_minutes=5) is None
    assert evaluate_price_range_rule(
        evidence, reference=REFERENCE, side="BUY", simulated_execution_price=Decimal("100")
    ).outcome == "EVIDENCE_UNAVAILABLE"


def _current_price_range_run(tmp_path, *, upper: str = "1.05"):
    system = PaperTradingSystem(
        tmp_path / "price-range.duckdb",
        PaperConfig(
            assets=SYMBOLS, initial_cash=1_000.0, fee_rate=0.0,
            minimum_spread_rate=0.0, slippage_rate=0.0, require_exchange_rules=True,
            require_execution_rule_evidence=True,
        ),
    )
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_schema_versions SET applied_at_utc='2024-01-01T00:00:00Z' WHERE version IN (13, 16)")
    timestamp = pd.Timestamp(RUN_NOW)
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(),
        quotes={symbol: Quote(100.0, 100.0, 100.0, timestamp) for symbol in SYMBOLS},
        fetched_at=timestamp,
        symbol_rules={symbol: _rules() for symbol in SYMBOLS},
        rule_reference_prices={symbol: RuleReferencePrice(Decimal("100"), "REFERENCE_PRICE", timestamp, timestamp) for symbol in SYMBOLS},
        price_range_rules={
            symbol: _rule(
                symbol=symbol, native_symbol=symbol.replace("/", ""),
                bid_limit_mult_up=Decimal(upper), source_timestamp=timestamp, acquired_at=timestamp,
            )
            for symbol in SYMBOLS
        },
    )
    provenance = ReleaseProvenance("a" * 40, False, "b" * 64, "paper-exec-v3-ask-bid-minspread-utc0010", RUN_NOW)
    system.store.insert_run(
        run_id="run", started_at=RUN_NOW, mode="PAPER", schedule_key="2026-09-08T09:05Z",
        signal_timestamp=RUN_NOW, data_timestamp=RUN_NOW, official_scheduled=True,
        release_provenance=provenance,
    )
    system._execute(
        run_id="run", signal_timestamp=pd.Timestamp("2026-09-07T00:00:00Z"),
        proposals=[{"idempotency_key": "order", "symbol": "BTC/USDT", "side": "BUY", "requested_quantity": 1.0, "target_weight": 1.0}],
        snapshot=snapshot, now=timestamp,
    )
    system.store.finish_run(
        run_id="run", status="EXECUTED", completed_at=RUN_NOW, message="fixture",
        reconciliation=ReconciliationResult(True, "fixture"),
    )
    return system


def test_price_range_evidence_and_decision_reconcile_offline_and_tamper_fails_closed(tmp_path, monkeypatch):
    system = _current_price_range_run(tmp_path)
    assert system.store.reconcile().valid
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT acquisition_status, bid_limit_mult_up FROM paper_execution_rules_evidence WHERE symbol='BTC/USDT'").fetchone() == ("PRICE_RANGE_PRESENT", "1.05")
        assert connection.execute("SELECT outcome, reason FROM paper_price_range_decisions").fetchone() == ("ACCEPTED", None)

    monkeypatch.setattr("src.paper_market.fetch_public_market_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")))
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_execution_rules_evidence SET bid_limit_mult_up='1.01' WHERE symbol='BTC/USDT'")
    assert not system.store.reconcile().valid


def test_required_price_range_evidence_cannot_be_bypassed_with_an_empty_snapshot_mapping(tmp_path):
    system = _current_price_range_run(tmp_path)
    with system.store.connect(read_only=True) as connection:
        snapshot_time = connection.execute("SELECT started_at_utc FROM paper_runs WHERE run_id='run'").fetchone()[0]
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(),
        quotes={symbol: Quote(100.0, 100.0, 100.0, snapshot_time) for symbol in SYMBOLS},
        fetched_at=snapshot_time,
        symbol_rules={symbol: _rules() for symbol in SYMBOLS},
        rule_reference_prices={symbol: RuleReferencePrice(Decimal("100"), "REFERENCE_PRICE", snapshot_time, snapshot_time) for symbol in SYMBOLS},
    )
    with pytest.raises(ValueError, match="executionRules evidence"):
        system._execute(
            run_id="run", signal_timestamp=pd.Timestamp("2026-09-07T00:00:00Z"),
            proposals=[], snapshot=snapshot, now=snapshot_time,
        )


def test_price_range_zero_order_run_reconciles_with_empty_decision_scope(tmp_path):
    system = _current_price_range_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute("DELETE FROM paper_orders WHERE run_id='run'")
        connection.execute("DELETE FROM paper_fills WHERE run_id='run'")
        connection.execute("DELETE FROM paper_price_range_decisions WHERE run_id='run'")
        connection.execute("DELETE FROM cash_ledger WHERE run_id='run'")
        connection.execute("DELETE FROM position_ledger WHERE run_id='run'")
        connection.execute("UPDATE paper_positions SET quantity=0, average_cost=0")
        connection.execute("UPDATE paper_accounts SET cash=initial_cash")
    assert system.store.reconcile().valid


def _refresh_execution_rules_digest(system):
    import json

    with system.store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM paper_execution_rules_evidence WHERE run_id='run' ORDER BY symbol"
        ).fetchall()
        diagnostics = json.loads(
            connection.execute(
                "SELECT diagnostics FROM paper_forward_execution_evidence WHERE run_id='run'"
            ).fetchone()[0]
        )
        diagnostics["execution_rules_evidence_sha256"] = (
            system.store.execution_rules_evidence_digest(rows)
        )
        connection.execute(
            "UPDATE paper_forward_execution_evidence SET diagnostics=? WHERE run_id='run'",
            [json.dumps(diagnostics, sort_keys=True)],
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE paper_execution_rules_evidence SET native_symbol='ETHUSDT' WHERE symbol='BTC/USDT'",
        "UPDATE paper_execution_rules_evidence SET price_range_present=FALSE WHERE symbol='BTC/USDT'",
        "UPDATE paper_execution_rules_evidence SET source_timestamp_utc=acquired_at_utc+INTERVAL 1 SECOND WHERE symbol='BTC/USDT'",
        "UPDATE paper_execution_rules_evidence SET source_timestamp_utc=acquired_at_utc-INTERVAL 301 SECOND WHERE symbol='BTC/USDT'",
        "UPDATE paper_price_range_decisions SET side='SELL' WHERE symbol='BTC/USDT'",
        "UPDATE paper_price_range_decisions SET outcome='REJECTED', reason='EXECUTION_RULE_PRICE_RANGE_EXCEEDED'",
        "DELETE FROM paper_execution_rules_evidence WHERE symbol='BTC/USDT'",
    ],
)
def test_price_range_semantic_tampering_fails_after_digest_refresh(tmp_path, statement):
    system = _current_price_range_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(statement)
    _refresh_execution_rules_digest(system)
    assert not system.store.reconcile().valid


def test_price_range_unbounded_decimal_tampering_fails_closed_without_reconcile_crash(tmp_path):
    system = _current_price_range_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_execution_rules_evidence SET bid_limit_mult_up='1E999999' "
            "WHERE symbol='BTC/USDT'"
        )
    _refresh_execution_rules_digest(system)
    assert not system.store.reconcile().valid


def test_price_range_unbounded_reference_tampering_fails_closed_without_reconcile_crash(tmp_path):
    system = _current_price_range_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute("DELETE FROM paper_orders WHERE run_id='run'")
        connection.execute("DELETE FROM paper_fills WHERE run_id='run'")
        connection.execute("DELETE FROM paper_price_range_decisions WHERE run_id='run'")
        connection.execute("DELETE FROM cash_ledger WHERE run_id='run'")
        connection.execute("DELETE FROM position_ledger WHERE run_id='run'")
        connection.execute("UPDATE paper_positions SET quantity=0, average_cost=0")
        connection.execute("UPDATE paper_accounts SET cash=initial_cash")
        connection.execute(
            "UPDATE paper_market_rule_evidence SET reference_price_decimal='1E999999' "
            "WHERE symbol='BTC/USDT'"
        )
        rows = connection.execute(
            "SELECT * FROM paper_market_rule_evidence WHERE run_id='run' ORDER BY symbol"
        ).fetchall()
        diagnostics = json.loads(connection.execute(
            "SELECT diagnostics FROM paper_forward_execution_evidence WHERE run_id='run'"
        ).fetchone()[0])
        diagnostics["market_rule_evidence_sha256"] = system.store.market_rule_evidence_digest(rows)
        connection.execute(
            "UPDATE paper_forward_execution_evidence SET diagnostics=? WHERE run_id='run'",
            [json.dumps(diagnostics, sort_keys=True)],
        )
    assert not system.store.reconcile().valid


def test_price_range_backup_and_temporary_restore_use_offline_runtime_reconciliation(tmp_path):
    import hashlib
    import json

    import duckdb

    from src.backup_restore import create_verified_backup, verify_backup, verify_restore_to_temporary

    system = _current_price_range_run(tmp_path)
    project = tmp_path / "project"
    (project / "forward_experiment").mkdir(parents=True)
    (project / "forward_experiment" / "governance.json").write_text("{}", encoding="utf-8")
    backup = create_verified_backup(
        project_root=project, database_path=system.store.path,
        output_root=tmp_path / "backups", lock_path=project / "runtime" / "forward_writer.lock",
        timestamp="batch-h", commit_hash="a" * 40,
        reconciliation_settings={
            "account_id": system.config.account_id,
            "quantity_tolerance": system.config.quantity_tolerance,
            "fee_rate": system.config.fee_rate,
            "minimum_spread_rate": system.config.minimum_spread_rate,
            "slippage_rate": system.config.slippage_rate,
            "max_quote_timestamp_skew_seconds": system.config.max_quote_timestamp_skew_seconds,
        },
    )
    assert verify_backup(backup)["valid"]
    assert verify_restore_to_temporary(backup, tmp_path / "restore")["valid"]
    database = backup / "paper_trading.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("UPDATE paper_execution_rules_evidence SET bid_limit_mult_up='1.01'")
        rows = connection.execute("SELECT * FROM paper_execution_rules_evidence ORDER BY symbol").fetchall()
        diagnostics = json.loads(
            connection.execute("SELECT diagnostics FROM paper_forward_execution_evidence WHERE run_id='run'").fetchone()[0]
        )
        diagnostics["execution_rules_evidence_sha256"] = system.store.execution_rules_evidence_digest(rows)
        connection.execute("UPDATE paper_forward_execution_evidence SET diagnostics=?", [json.dumps(diagnostics)])
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checksums"]["paper_trading.duckdb"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (backup / "backup_manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  backup_manifest.json\n",
        encoding="ascii",
    )
    with pytest.raises(ValueError, match="runtime reconciliation"):
        verify_backup(backup)
