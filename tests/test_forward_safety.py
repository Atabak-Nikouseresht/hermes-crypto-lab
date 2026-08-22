from pathlib import Path

import yaml

from run_paper import load_paper_configuration
from src.forward_governance import locked_strategy_hash


EXPECTED_LOCKED_HASH = "29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6"


def test_locked_strategy_and_real_trading_prohibitions_remain_enforced():
    root = Path(__file__).resolve().parents[1]
    config, _values = load_paper_configuration(root)
    payload = yaml.safe_load((root / "config" / "strategy.yaml").read_text(encoding="utf-8"))

    assert locked_strategy_hash(config) == EXPECTED_LOCKED_HASH
    assert payload["live_trading_enabled"] is False
    assert payload["optimization_enabled"] is False

    operational_sources = [
        root / "run_paper.py",
        root / "src" / "paper_broker.py",
        root / "src" / "paper_market.py",
        root / "src" / "paper_store.py",
        root / "src" / "paper_forward.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in operational_sources)
    forbidden = [
        ".create_order(",
        ".createOrder(",
        ".fetch_balance(",
        "apiKey=",
        "secret=",
        "withdraw(",
        "set_leverage(",
    ]
    assert all(token not in combined for token in forbidden)
