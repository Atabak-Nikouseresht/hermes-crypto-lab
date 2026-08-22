import pytest

from src.portfolio import build_target_weights


def test_inverse_volatility_weights_respect_asset_and_altcoin_caps():
    weights = build_target_weights(
        ranked_assets=["BNB/USDT", "XRP/USDT"],
        realized_volatility={"BNB/USDT": 0.20, "XRP/USDT": 0.20},
        max_assets=2,
        asset_caps={
            "BTC/USDT": 0.70,
            "ETH/USDT": 0.60,
            "BNB/USDT": 0.40,
            "XRP/USDT": 0.40,
            "TRX/USDT": 0.40,
        },
        altcoins={"BNB/USDT", "XRP/USDT", "TRX/USDT"},
        max_altcoin_weight=0.60,
    )

    assert weights["BNB/USDT"] == pytest.approx(0.30)
    assert weights["XRP/USDT"] == pytest.approx(0.30)
    assert weights["CASH"] == pytest.approx(0.40)
    assert sum(weights.values()) == pytest.approx(1.0)
