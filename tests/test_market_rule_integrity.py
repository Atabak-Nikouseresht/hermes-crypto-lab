from dataclasses import replace
from datetime import datetime, timezone
import json
from decimal import Decimal

import pandas as pd
import pytest

from src.paper_broker import (
    MarketSnapshot,
    PaperConfig,
    PaperTradingSystem,
    Quote,
    RuleReferencePrice,
    SymbolRules,
)
from src.paper_store import ReconciliationResult
from src.release_provenance import ReleaseProvenance


NOW = datetime(2026, 9, 8, 9, 10, tzinfo=timezone.utc)
SYMBOLS = ("BTC/USDT", "ETH/USDT")


def _provenance() -> ReleaseProvenance:
    return ReleaseProvenance(
        git_commit="a" * 40,
        git_dirty=False,
        hardening_manifest_sha256="b" * 64,
        execution_protocol_version="paper-exec-v3-ask-bid-minspread-utc0010",
        captured_at_utc=NOW,
    )


def _rules(*, minimum: str = "0.1", market_minimum: str = "0.1", average: int = 0) -> SymbolRules:
    return SymbolRules(
        active=True,
        min_quantity=float(Decimal(minimum)),
        max_quantity=Decimal("10"),
        step_size=0.1,
        min_notional=1.0,
        price_tick=0.01,
        market_min_quantity=float(Decimal(market_minimum)),
        market_max_quantity=Decimal("10"),
        market_step_size=0.1,
        min_notional_applies_to_market=True,
        min_notional_avg_price_mins=average,
        notional_min=1.0,
        notional_max=1_000.0,
        notional_min_applies_to_market=True,
        notional_max_applies_to_market=True,
        notional_avg_price_mins=average,
        raw_min_quantity=Decimal(minimum),
        raw_max_quantity=Decimal("10"),
        raw_step_size=Decimal("0.1"),
        raw_market_min_quantity=Decimal(market_minimum),
        raw_market_max_quantity=Decimal("10"),
        raw_market_step_size=Decimal("0.1"),
        raw_min_notional=Decimal("1"),
        raw_notional_min=Decimal("1"),
        raw_notional_max=Decimal("1000"),
    )


def _system(tmp_path, *, rules: SymbolRules | None = None, reference: Decimal | None = Decimal("100")):
    system = PaperTradingSystem(
        tmp_path / "integrity.duckdb",
        PaperConfig(
            assets=SYMBOLS,
            initial_cash=1_000.0,
            fee_rate=0.0,
            minimum_spread_rate=0.0,
            slippage_rate=0.0,
            require_exchange_rules=True,
        ),
    )
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_schema_versions SET applied_at_utc='2024-01-01T00:00:00Z' WHERE version IN (13, 16)")
    timestamp = pd.Timestamp(NOW)
    symbol_rules = {symbol: rules or _rules() for symbol in SYMBOLS}
    references = {
        symbol: RuleReferencePrice(reference, "REFERENCE_PRICE", timestamp)
        if reference is not None
        else RuleReferencePrice(None, "UNVERIFIABLE_AVERAGE")
        for symbol in SYMBOLS
    }
    snapshot = MarketSnapshot(
        closes=pd.DataFrame(),
        quotes={symbol: Quote(100.0, 100.0, 100.0, timestamp) for symbol in SYMBOLS},
        fetched_at=timestamp,
        symbol_rules=symbol_rules,
        rule_reference_prices=references,
    )
    return system, snapshot, timestamp


def _current_run(tmp_path, *, rules: SymbolRules | None = None, reference: Decimal | None = Decimal("100"), quantity: float | None = 1.0, stage: str = "FINAL"):
    system, snapshot, timestamp = _system(tmp_path, rules=rules, reference=reference)
    system.store.insert_run(
        run_id="run",
        started_at=NOW,
        mode="PAPER",
        schedule_key="2026-09-08T09:05Z",
        signal_timestamp=NOW,
        data_timestamp=NOW,
        official_scheduled=True,
        release_provenance=_provenance(),
    )
    proposals = [] if quantity is None else [{
        "idempotency_key": "order", "symbol": "BTC/USDT", "side": "BUY",
        "requested_quantity": quantity, "target_weight": 1.0,
    }]
    if stage == "PROPOSAL":
        _, reason = system._normalize_exchange_quantity(
            symbol="BTC/USDT", quantity=quantity,
            rule_reference_price=reference, snapshot=snapshot,
        )
        assert reason is not None
        system._reject_quantity(proposal=proposals[0], reason=reason,
                                notional=quantity * 100, stage=stage)
        proposals = []
    system._execute(
        run_id="run",
        signal_timestamp=pd.Timestamp("2026-09-07T00:00:00Z"),
        proposals=proposals,
        snapshot=snapshot,
        now=timestamp,
    )
    system.store.finish_run(
        run_id="run", status="EXECUTED", completed_at=NOW, message="fixture",
        reconciliation=ReconciliationResult(True, "fixture"),
    )
    assert system.store.reconcile().valid
    return system


@pytest.mark.parametrize(
    "tables",
    [
        ("paper_market_rule_evidence",),
        ("paper_market_rule_evidence", "paper_execution_context"),
        ("paper_market_rule_evidence", "paper_execution_context", "paper_orders", "paper_order_rejections"),
    ],
)
def test_authoritative_scope_rejects_coordinated_symbol_stripping(tmp_path, tables):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        for table in tables:
            connection.execute(f"DELETE FROM {table} WHERE run_id='run' AND symbol='ETH/USDT'")
    assert not system.store.reconcile().valid


def test_authoritative_scope_rejects_replaced_or_extra_symbol_evidence(tmp_path):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_market_rule_evidence SET symbol='XRP/USDT' WHERE run_id='run' AND symbol='ETH/USDT'")
    assert not system.store.reconcile().valid


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE paper_market_rule_evidence SET contract_version='other' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET reference_price_decimal='99' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET reference_price_source='LAST_FALLBACK' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET reference_price_timestamp_utc=NULL WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET lot_min_qty='2' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET lot_max_qty='0.5' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET lot_step_size='0.3' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET market_lot_min_qty='2' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET market_lot_max_qty='0.5' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET market_lot_step_size='0.3' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET min_notional='101' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET min_notional_applies_to_market=FALSE WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET notional_min='101' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET notional_max='99' WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET notional_min_applies_to_market=FALSE WHERE run_id='run' AND symbol='BTC/USDT'",
        "UPDATE paper_market_rule_evidence SET notional_max_applies_to_market=FALSE WHERE run_id='run' AND symbol='BTC/USDT'",
        "DELETE FROM paper_market_rule_evidence WHERE run_id='run' AND symbol='BTC/USDT'",
    ],
)
def test_market_rule_semantic_tampering_fails_closed(tmp_path, statement):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(statement)
    assert not system.store.reconcile().valid


def test_rejection_reason_is_reconstructed_from_persisted_rules(tmp_path):
    system = _current_run(tmp_path, rules=_rules(minimum="2"), quantity=1.0)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT reason FROM paper_order_rejections").fetchone() == ("below_min_quantity",)
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_market_rule_evidence SET lot_min_qty='0.1' WHERE run_id='run' AND symbol='BTC/USDT'")
    assert not system.store.reconcile().valid


def test_final_rejection_reconstructs_scaled_quantity_not_proposal(tmp_path):
    rules = replace(_rules(minimum="10.1"), raw_max_quantity=Decimal("100"),
                    raw_market_max_quantity=Decimal("100"), raw_notional_max=Decimal("100000"))
    system = _current_run(tmp_path, rules=rules, quantity=20.0)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute(
            "SELECT stage, reason, requested_quantity, notional FROM paper_order_rejections"
        ).fetchone() == ("FINAL", "below_min_quantity", 20.0, 1000.0)


def _refresh_rule_digest(system):
    """Bypass the checksum deliberately to exercise semantic validation."""
    with system.store.connect() as connection:
        rows = connection.execute("SELECT * FROM paper_market_rule_evidence WHERE run_id='run' ORDER BY symbol").fetchall()
        diagnostics = json.loads(connection.execute("SELECT diagnostics FROM paper_forward_execution_evidence WHERE run_id='run'").fetchone()[0])
        diagnostics["market_rule_evidence_sha256"] = system.store.market_rule_evidence_digest(rows)
        connection.execute("UPDATE paper_forward_execution_evidence SET diagnostics=? WHERE run_id='run'", [json.dumps(diagnostics)])


@pytest.mark.parametrize("mutation", [
    "UPDATE paper_order_rejections SET notional=99",
    "UPDATE paper_order_rejections SET stage='PROPOSAL'",
    "UPDATE paper_order_rejections SET reason='unrelated_reason'",
    "DELETE FROM paper_order_rejections",
])
def test_rejection_audit_matches_independent_run_diagnostics(tmp_path, mutation):
    system = _current_run(tmp_path, rules=_rules(minimum="2"))
    with system.store.connect() as connection:
        connection.execute(mutation)
    result = system.store.reconcile()
    assert not result.valid, result.message


def test_symbol_scope_rejects_extra_execution_context(tmp_path):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute("INSERT INTO paper_execution_context SELECT * REPLACE ('XRP/USDT' AS symbol) FROM paper_execution_context WHERE symbol='BTC/USDT'")
    result = system.store.reconcile()
    assert not result.valid, result.message
    assert "symbol" in result.message.lower()


def test_exact_reference_product_beyond_decimal_default_precision(tmp_path):
    system = _current_run(tmp_path, reference=Decimal("100.0000000000000000000000000001"))
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_market_rule_evidence SET notional_max='100' WHERE symbol='BTC/USDT'")
    _refresh_rule_digest(system)
    result = system.store.reconcile()
    assert not result.valid, result.message
    assert "NOTIONAL maximum" in result.message


def test_non_step_proposal_persists_normalized_fill_as_requested_quantity(tmp_path):
    system = _current_run(tmp_path, quantity=0.99)
    with system.store.connect(read_only=True) as connection:
        # This repository's requested_quantity is the post-normalization
        # executable amount, so it must equal the authoritative filled amount.
        assert connection.execute(
            "SELECT o.requested_quantity, f.filled_quantity FROM paper_orders o "
            "JOIN paper_fills f USING (order_id)"
        ).fetchone() == (0.9, 0.9)
    assert system.store.reconcile().valid


# These cases deliberately refresh the digest: success cannot be explained by
# a checksum mismatch instead of offline reconstruction of the exact rule.
SEMANTIC_TAMPERS = [
    ("contract_version='other'", None),
    ("reference_price_decimal='NaN'", None),
    ("reference_price_source='unknown'", None),
    ("reference_price_timestamp_utc=NULL", None),
    ("lot_min_qty='2'", None),
    ("lot_max_qty='0.5'", None),
    ("lot_step_size='0.3'", None),
    ("market_lot_min_qty='2'", None),
    ("market_lot_max_qty='0.5'", None),
    ("market_lot_step_size='0.3'", None),
    ("min_notional='101'", None),
    ("min_notional_applies_to_market=TRUE", replace(_rules(), min_notional_applies_to_market=False, raw_min_notional=Decimal("101"))),
    ("notional_min='101'", None),
    ("notional_max='99'", None),
    ("notional_min_applies_to_market=TRUE", replace(_rules(), notional_min_applies_to_market=False, raw_notional_min=Decimal("101"))),
    ("notional_max_applies_to_market=TRUE", replace(_rules(), notional_max_applies_to_market=False, raw_notional_max=Decimal("99"))),
    ("min_notional_avg_price_mins=-1", None),
    ("notional_avg_price_mins=-1", None),
    ("min_notional=NULL", None),
    ("reference_price_source='LAST_FALLBACK', notional_avg_price_mins=5", None),
]


@pytest.mark.parametrize("mutation,rules", SEMANTIC_TAMPERS, ids=[f"T{i}" for i in range(1, 21)])
def test_semantic_tamper_with_recomputed_digest(tmp_path, mutation, rules):
    system = _current_run(tmp_path, rules=rules)
    with system.store.connect() as connection:
        connection.execute(f"UPDATE paper_market_rule_evidence SET {mutation} WHERE symbol='BTC/USDT'")
    _refresh_rule_digest(system)
    result = system.store.reconcile()
    assert not result.valid, result.message
    assert "digest" not in result.message.lower()


@pytest.mark.parametrize("column", ["min_notional", "notional_min", "notional_max"])
def test_applicable_notional_rule_cannot_lose_its_limit(tmp_path, column):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(f"UPDATE paper_market_rule_evidence SET {column}=NULL WHERE symbol='BTC/USDT'")
    _refresh_rule_digest(system)
    assert not system.store.reconcile().valid


@pytest.mark.parametrize("column", ["lot_step_size", "market_lot_step_size"])
def test_exact_step_beyond_decimal_default_precision(tmp_path, column):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(f"UPDATE paper_market_rule_evidence SET {column}='0.10000000000000000000000000001' WHERE symbol='BTC/USDT'")
    _refresh_rule_digest(system)
    result = system.store.reconcile()
    assert not result.valid, result.message
    assert "step" in result.message


REJECTION_CASES = [
    ("below_min_quantity", _rules(minimum="2"), Decimal("100"), "lot_min_qty='0.1'"),
    ("above_max_quantity", replace(_rules(), raw_max_quantity=Decimal("0.5")), Decimal("100"), "lot_max_qty='10'"),
    ("below_market_min_quantity", _rules(market_minimum="2"), Decimal("100"), "market_lot_min_qty='0.1'"),
    ("above_market_max_quantity", replace(_rules(), raw_market_max_quantity=Decimal("0.5")), Decimal("100"), "market_lot_max_qty='10'"),
    ("below_min_notional", replace(_rules(), raw_min_notional=Decimal("101")), Decimal("100"), "min_notional='1'"),
    ("below_market_notional", replace(_rules(), raw_notional_min=Decimal("101")), Decimal("100"), "notional_min='1'"),
    ("above_market_notional", replace(_rules(), raw_notional_max=Decimal("99")), Decimal("100"), "notional_max='1000'"),
    ("market_notional_reference_unverifiable", _rules(average=5), None, "reference_price_source='REFERENCE_PRICE', reference_price_decimal='100', reference_price_timestamp_utc='2026-09-08T09:10:00Z'"),
]


@pytest.mark.parametrize("stage", ["PROPOSAL", "FINAL"])
@pytest.mark.parametrize("reason,rules,reference,mutation", REJECTION_CASES, ids=[case[0] for case in REJECTION_CASES])
def test_each_rejection_semantically_reconstructs_exact_rule(tmp_path, stage, reason, rules, reference, mutation):
    system = _current_run(tmp_path, rules=rules, reference=reference, stage=stage)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT stage, reason, requested_quantity, notional FROM paper_order_rejections").fetchone() == (stage, reason, 1.0, 100.0)
    with system.store.connect() as connection:
        connection.execute(f"UPDATE paper_market_rule_evidence SET {mutation} WHERE symbol='BTC/USDT'")
    _refresh_rule_digest(system)
    result = system.store.reconcile()
    assert not result.valid, result.message
    assert result.message == "Inconsistent market-rule rejection reason"


def test_unverifiable_average_requires_matching_source_and_average_rule(tmp_path):
    system = _current_run(tmp_path, rules=_rules(average=5), reference=None, quantity=1.0)
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT reason FROM paper_order_rejections").fetchone() == ("market_notional_reference_unverifiable",)
    with system.store.connect() as connection:
        connection.execute("UPDATE paper_market_rule_evidence SET reference_price_source='LAST_FALLBACK', reference_price_decimal='100' WHERE run_id='run' AND symbol='BTC/USDT'")
    assert not system.store.reconcile().valid


def test_unverifiable_average_requires_an_applicable_positive_average_window(tmp_path):
    system = _current_run(tmp_path, rules=_rules(average=5), reference=None, quantity=1.0)
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_market_rule_evidence SET min_notional_avg_price_mins=0, "
            "notional_avg_price_mins=0 WHERE run_id='run' AND symbol='BTC/USDT'"
        )
    _refresh_rule_digest(system)
    result = system.store.reconcile()
    assert not result.valid
    assert result.message == "Invalid unverifiable-average evidence"


def test_authoritative_scope_rejects_correct_evidence_attached_to_wrong_run(tmp_path):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(
            "UPDATE paper_market_rule_evidence SET run_id='other' "
            "WHERE run_id='run' AND symbol='ETH/USDT'"
        )
    assert not system.store.reconcile().valid


def test_authoritative_scope_rejects_extra_unrelated_evidence(tmp_path):
    system = _current_run(tmp_path)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_market_rule_evidence "
            "SELECT * REPLACE ('XRP/USDT' AS symbol) FROM paper_market_rule_evidence "
            "WHERE run_id='run' AND symbol='BTC/USDT'"
        )
    _refresh_rule_digest(system)
    assert not system.store.reconcile().valid


def test_fully_rejected_run_cannot_lose_evidence_and_rejections(tmp_path):
    system = _current_run(tmp_path, rules=_rules(minimum="2"), quantity=1.0, stage="PROPOSAL")
    with system.store.connect() as connection:
        connection.execute("DELETE FROM paper_market_rule_evidence WHERE run_id='run'")
        connection.execute("DELETE FROM paper_order_rejections WHERE run_id='run'")
    assert not system.store.reconcile().valid


def test_reconcile_has_no_market_or_network_dependency(tmp_path, monkeypatch):
    system = _current_run(tmp_path)
    import src.paper_market as paper_market

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reconciliation attempted external market access")

    monkeypatch.setattr(paper_market, "fetch_public_market_snapshot", forbidden)
    assert system.store.reconcile().valid
