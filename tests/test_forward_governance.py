
import pytest

from src.forward_governance import (
    bootstrap_forward_experiment,
    create_immutable_governance,
    locked_strategy_hash,
    locked_strategy_spec,
    verify_governance,
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
    config = PaperConfig(assets=ASSETS)

    verified = verify_trust_anchors(root, config)

    assert verified["locked_strategy"] == EXPECTED_HASH
    assert "governance_amendment" in verified


def test_repository_governance_amendment_rejects_schedule_drift():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = PaperConfig(assets=ASSETS, schedule_hour=9, schedule_minute=5)

    with pytest.raises(ValueError, match="schedule"):
        verify_trust_anchors(root, config)


def test_forward_experiment_bootstrap_is_idempotent(tmp_path):
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = PaperConfig(assets=ASSETS)
    from src.paper_store import PaperStore

    store = PaperStore(tmp_path / "paper.duckdb", account_id="locked_strategy", initial_cash=2000)
    first = bootstrap_forward_experiment(store, root, config)
    second = bootstrap_forward_experiment(store, root, config)

    with store.connect(read_only=True) as connection:
        count = connection.execute("SELECT COUNT(*) FROM forward_experiments").fetchone()[0]
    assert first == second == "paper-forward-20260822"
    assert count == 1
