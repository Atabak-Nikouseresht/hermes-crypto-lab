from hypothesis import given, strategies as st

from src.portfolio import build_target_weights


@given(
    btc_vol=st.floats(min_value=0.01, max_value=5, allow_nan=False, allow_infinity=False),
    eth_vol=st.floats(min_value=0.01, max_value=5, allow_nan=False, allow_infinity=False),
    alt_vol=st.floats(min_value=0.01, max_value=5, allow_nan=False, allow_infinity=False),
    max_assets=st.integers(min_value=0, max_value=3),
)
def test_portfolio_constraints_hold_for_positive_volatility(
    btc_vol, eth_vol, alt_vol, max_assets
):
    weights = build_target_weights(
        ranked_assets=["BTC/USDT", "ETH/USDT", "XRP/USDT"],
        realized_volatility={
            "BTC/USDT": btc_vol,
            "ETH/USDT": eth_vol,
            "XRP/USDT": alt_vol,
        },
        max_assets=max_assets,
        asset_caps={"BTC/USDT": 0.70, "ETH/USDT": 0.60, "XRP/USDT": 0.40},
        altcoins={"XRP/USDT"},
        max_altcoin_weight=0.60,
    )

    assert all(weight >= 0 for weight in weights.values())
    assert abs(sum(weights.values()) - 1.0) <= 1e-12
    assert weights.get("BTC/USDT", 0.0) <= 0.70 + 1e-12
    assert weights.get("ETH/USDT", 0.0) <= 0.60 + 1e-12
    assert weights.get("XRP/USDT", 0.0) <= 0.40 + 1e-12
    assert sum(asset != "CASH" and weight > 0 for asset, weight in weights.items()) <= max_assets
