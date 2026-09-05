
from dataclasses import replace

import pytest

from src.forward_governance import (
    bootstrap_forward_experiment,
    create_immutable_governance,
    economic_spec_hash_v2,
    economic_spec_v2,
    locked_strategy_hash,
    locked_strategy_spec,
    verify_economic_spec_v2,
    verify_governance,
    verify_quote_coherence_runtime_contract,
    verify_trust_anchors,
)
from src.paper_broker import PaperConfig

ASSETS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "TRX/USDT")
EXPECTED_HASH = "29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6"


def test_locked_strategy_hash_is_unchanged_by_operational_schedule():
    config = PaperConfig(
        assets=ASSETS,
        schedule_hour=9,
        schedule_minute=5,
        execution_target_minute=10,
    )

    assert locked_strategy_spec(config)["candidate_id"] == "mw120_sw00_ma150_n2_r07_v30"
    assert locked_strategy_hash(config) == EXPECTED_HASH


def _mutate_strategy(config, **changes):
    return replace(config, strategy_config=replace(config.strategy_config, **changes))


def _economic_config():
    return PaperConfig(assets=ASSETS, require_exchange_rules=True)


ECONOMIC_MUTATIONS = [
    ("assets", lambda c: replace(c, assets=c.assets + ("SOL/USDT",))),
    ("initial_cash", lambda c: replace(c, initial_cash=c.initial_cash + 1.0)),
    ("accounting_currency", lambda c: replace(c, accounting_currency="EUR")),
    ("fee_rate", lambda c: replace(c, fee_rate=c.fee_rate + 0.0001)),
    (
        "minimum_spread_rate",
        lambda c: replace(c, minimum_spread_rate=c.minimum_spread_rate + 0.0001),
    ),
    ("slippage_rate", lambda c: replace(c, slippage_rate=c.slippage_rate + 0.0001)),
    ("schedule_weekday", lambda c: replace(c, schedule_weekday=1)),
    ("schedule_hour", lambda c: replace(c, schedule_hour=1)),
    ("schedule_minute", lambda c: replace(c, schedule_minute=6)),
    (
        "execution_target_minute",
        lambda c: replace(c, execution_target_minute=11),
    ),
    (
        "schedule_window_minutes",
        lambda c: replace(c, schedule_window_minutes=c.schedule_window_minutes + 1),
    ),
    (
        "max_data_staleness_minutes",
        lambda c: replace(c, max_data_staleness_minutes=c.max_data_staleness_minutes + 1),
    ),
    (
        "max_quote_staleness_minutes",
        lambda c: replace(c, max_quote_staleness_minutes=c.max_quote_staleness_minutes + 1),
    ),
    (
        "quantity_tolerance",
        lambda c: replace(c, quantity_tolerance=c.quantity_tolerance * 10),
    ),
    ("rebalance_days", lambda c: replace(c, rebalance_days=14)),
    ("locked_candidate_id", lambda c: replace(c, locked_candidate_id="changed")),
    (
        "require_exchange_rules",
        lambda c: replace(c, require_exchange_rules=not c.require_exchange_rules),
    ),
    (
        "max_abs_daily_return",
        lambda c: replace(c, max_abs_daily_return=c.max_abs_daily_return - 0.01),
    ),
    (
        "max_volume_ratio",
        lambda c: replace(c, max_volume_ratio=c.max_volume_ratio - 1.0),
    ),
    ("exchange_id", lambda c: replace(c, exchange_id="other-public-exchange")),
    ("lookback_days", lambda c: replace(c, lookback_days=c.lookback_days + 1)),
    (
        "momentum_short_days",
        lambda c: _mutate_strategy(c, momentum_short_days=31),
    ),
    (
        "momentum_long_days",
        lambda c: _mutate_strategy(c, momentum_long_days=121),
    ),
    (
        "momentum_skip_days",
        lambda c: _mutate_strategy(c, momentum_skip_days=1),
    ),
    (
        "btc_moving_average_days",
        lambda c: _mutate_strategy(c, btc_moving_average_days=151),
    ),
    (
        "volatility_days",
        lambda c: _mutate_strategy(c, volatility_days=31),
    ),
    (
        "annualization_days",
        lambda c: _mutate_strategy(c, annualization_days=366),
    ),
    ("max_assets", lambda c: _mutate_strategy(c, max_assets=3)),
    (
        "asset_caps",
        lambda c: _mutate_strategy(
            c, asset_caps={**c.strategy_config.asset_caps, "BTC/USDT": 0.69}
        ),
    ),
    (
        "altcoins",
        lambda c: _mutate_strategy(
            c, altcoins=c.strategy_config.altcoins | {"ETH/USDT"}
        ),
    ),
    (
        "max_altcoin_weight",
        lambda c: _mutate_strategy(c, max_altcoin_weight=0.59),
    ),
]


def test_economic_spec_v2_canonicalizes_unordered_collections():
    config = _economic_config()
    reordered = replace(
        config,
        assets=tuple(reversed(config.assets)),
        strategy_config=replace(
            config.strategy_config,
            asset_caps=dict(reversed(list(config.strategy_config.asset_caps.items()))),
            altcoins=set(reversed(sorted(config.strategy_config.altcoins))),
        ),
    )

    assert economic_spec_v2(reordered) == economic_spec_v2(config)
    assert economic_spec_hash_v2(reordered) == economic_spec_hash_v2(config)


@pytest.mark.parametrize(
    ("field", "mutate"), ECONOMIC_MUTATIONS, ids=[item[0] for item in ECONOMIC_MUTATIONS]
)
def test_economic_spec_v2_rejects_every_relevant_field_mutation(field, mutate):
    config = _economic_config()

    with pytest.raises(ValueError, match="economic specification"):
        verify_economic_spec_v2(mutate(config))


def test_economic_spec_v2_rejects_execution_protocol_mutation(monkeypatch):
    import src.forward_governance as governance

    monkeypatch.setattr(governance, "EXECUTION_PROTOCOL_VERSION", "changed-protocol")

    with pytest.raises(ValueError, match="economic specification"):
        governance.verify_economic_spec_v2(_economic_config())


def test_forward_governance_record_is_create_once_and_hash_verified(tmp_path):
    path = tmp_path / "governance.json"
    payload = {
        "experiment_id": "forward-1",
        "locked_strategy_hash": EXPECTED_HASH,
        "minimum_observation_weeks": 12,
        "live_promotion": False,
    }

    digest, sidecar = create_immutable_governance(path, payload)

    assert verify_governance(path, sidecar) == digest
    with pytest.raises(FileExistsError):
        create_immutable_governance(path, payload)


def test_repository_governance_uses_code_anchored_release_hashes():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = _economic_config()

    verified = verify_trust_anchors(root, config)

    assert verified["locked_strategy"] == EXPECTED_HASH
    assert "governance_amendment" in verified
    assert verified["economic_spec_v2"] == economic_spec_hash_v2(config)
    assert "economic_governance_amendment" in verified
    assert "quote_coherence_governance_amendment" in verified
    assert "quote_coherence_contract" in verified
    assert economic_spec_hash_v2(
        replace(config, max_quote_timestamp_skew_seconds=31)
    ) == economic_spec_hash_v2(config)


def test_quote_coherence_runtime_contract_requires_exact_governed_skew():
    payload = {
        "version": "quote-coherence-v1-cross-asset-utc",
        "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010",
        "rule": {"max_quote_timestamp_skew_seconds": 30},
    }

    verify_quote_coherence_runtime_contract(payload, _economic_config())
    with pytest.raises(ValueError, match="skew"):
        verify_quote_coherence_runtime_contract(
            payload, replace(_economic_config(), max_quote_timestamp_skew_seconds=60)
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"version": "wrong", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": 30}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "wrong", "rule": {"max_quote_timestamp_skew_seconds": 30}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": 30.0}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": "30"}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": True}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": False}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": None}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": 0}},
        {"version": "quote-coherence-v1-cross-asset-utc", "execution_protocol_version": "paper-exec-v3-ask-bid-minspread-utc0010", "rule": {"max_quote_timestamp_skew_seconds": -1}},
    ],
)
def test_quote_coherence_runtime_contract_rejects_invalid_governed_values(payload):
    with pytest.raises(ValueError):
        verify_quote_coherence_runtime_contract(payload, _economic_config())


def test_repository_governance_amendment_rejects_schedule_drift():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = replace(_economic_config(), schedule_hour=9, schedule_minute=5)

    with pytest.raises(ValueError, match="economic specification|schedule"):
        verify_trust_anchors(root, config)


def test_trust_anchors_reject_runtime_quote_skew_mismatch():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]

    with pytest.raises(ValueError, match="quote timestamp skew"):
        verify_trust_anchors(root, replace(_economic_config(), max_quote_timestamp_skew_seconds=60))


def test_forward_experiment_bootstrap_is_idempotent(tmp_path):
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = _economic_config()
    from src.paper_store import PaperStore

    store = PaperStore(tmp_path / "paper.duckdb", account_id="locked_strategy", initial_cash=2000)
    first = bootstrap_forward_experiment(store, root, config)
    second = bootstrap_forward_experiment(store, root, config)

    with store.connect(read_only=True) as connection:
        count = connection.execute("SELECT COUNT(*) FROM forward_experiments").fetchone()[0]
    assert first == second == "paper-forward-20260822"
    assert count == 1
