from pathlib import Path

import yaml

from run_paper import load_paper_configuration
from src.forward_governance import locked_strategy_hash
from scripts.verify_safety import scan


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


def test_safety_scanner_detects_private_exchange_capability_families(tmp_path):
    source = tmp_path / "src" / "probe.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "exchange.create_market_buy_order('BTC/USDT', 1)",
                "exchange.cancel_all_orders('BTC/USDT')",
                "exchange.fetch_positions()",
                "exchange.transfer('USDT', 1, 'spot', 'margin')",
                "exchange.set_margin_mode('isolated', 'BTC/USDT')",
            ]
        ),
        encoding="utf-8",
    )

    categories = {category for category, _path, _line in scan(tmp_path)}

    assert categories == {
        "order creation method",
        "private order-management method",
        "private account or position method",
        "withdrawal or transfer method",
        "leverage or margin method",
    }


def test_safety_scanner_detects_extended_order_method_families(tmp_path):
    source = tmp_path / "src" / "probe.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "exchange.create_market_order('BTC/USDT', 'buy', 1)",
                "exchange.create_limit_order('BTC/USDT', 'buy', 1, 100)",
                "exchange.cancel_all_orders_after(1000)",
                "exchange.fetch_order_trades(order_id)",
            ]
        ),
        encoding="utf-8",
    )

    findings = scan(tmp_path)

    assert len(findings) == 4
    assert {category for category, _path, _line in findings} == {
        "order creation method",
        "private order-management method",
    }


def test_safety_scanner_allows_narrow_public_market_capabilities(tmp_path):
    source = tmp_path / "src" / "probe.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "exchange.load_markets()",
                "exchange.fetch_ohlcv('BTC/USDT', '1d')",
                "exchange.fetch_ticker('BTC/USDT')",
                "exchange.market('BTC/USDT')",
                "exchange.close()",
            ]
        ),
        encoding="utf-8",
    )

    assert scan(tmp_path) == []


def test_safety_scanner_detects_dynamic_private_capability_lookup(tmp_path):
    source = tmp_path / "src" / "probe.py"
    source.parent.mkdir()
    source.write_text(
        "method = getattr(exchange, 'create_order')\n",
        encoding="utf-8",
    )

    assert scan(tmp_path) == [("order creation method", "src/probe.py", 1)]


def test_safety_scanner_detects_constant_built_private_capability_lookup(tmp_path):
    source = tmp_path / "src" / "probe.py"
    source.parent.mkdir()
    source.write_text(
        "method = getattr(exchange, 'create_' + 'order')\n",
        encoding="utf-8",
    )

    assert scan(tmp_path) == [("order creation method", "src/probe.py", 1)]


def test_safety_scanner_detects_private_capability_alias(tmp_path):
    source = tmp_path / "src" / "probe.py"
    source.parent.mkdir()
    source.write_text(
        "submit = exchange.create_order\nsubmit('BTC/USDT', 'market', 'buy', 1)\n",
        encoding="utf-8",
    )

    assert scan(tmp_path) == [("order creation method", "src/probe.py", 1)]


def test_safety_scanner_covers_operational_scripts(tmp_path):
    source = tmp_path / "scripts" / "paper_probe.py"
    source.parent.mkdir()
    source.write_text("exchange.cancel_order(order_id)\n", encoding="utf-8")

    assert scan(tmp_path) == [
        ("private order-management method", "scripts/paper_probe.py", 1)
    ]
