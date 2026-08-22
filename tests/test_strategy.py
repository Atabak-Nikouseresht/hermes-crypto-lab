import numpy as np
import pandas as pd

from src.strategy import StrategyConfig, generate_signal


def _rising_prices() -> pd.DataFrame:
    dates = pd.date_range("2023-01-01", periods=240, freq="D", tz="UTC")
    day = np.arange(len(dates), dtype=float)
    return pd.DataFrame(
        {
            "BTC/USDT": 100.0 * np.exp(0.0010 * day),
            "ETH/USDT": 100.0 * np.exp(0.0015 * day),
            "BNB/USDT": 100.0 * np.exp(0.0020 * day),
            "XRP/USDT": 100.0 * np.exp(-0.0010 * day),
            "TRX/USDT": 100.0 * np.exp(0.0005 * day),
        },
        index=dates,
    )


def test_signal_uses_only_information_available_at_as_of_and_selects_top_two():
    prices = _rising_prices()
    as_of = prices.index[220]
    config = StrategyConfig()

    original = generate_signal(prices, as_of=as_of, config=config)
    changed_future = prices.copy()
    changed_future.loc[changed_future.index > as_of, "XRP/USDT"] *= 1_000_000
    after_future_change = generate_signal(changed_future, as_of=as_of, config=config)

    assert original.ranked_assets == ("BNB/USDT", "ETH/USDT")
    assert original.target_weights == after_future_change.target_weights
    assert original.momentum_90_ex_7 == after_future_change.momentum_90_ex_7
    assert original.target_weights["BNB/USDT"] <= 0.40
    assert original.target_weights["ETH/USDT"] <= 0.60
    assert original.target_weights["CASH"] >= 0.0


def test_btc_below_200_day_average_forces_all_cash():
    prices = _rising_prices()
    as_of = prices.index[220]
    prices.loc[as_of, "BTC/USDT"] = 1.0

    signal = generate_signal(prices, as_of=as_of, config=StrategyConfig())

    assert signal.ranked_assets == ()
    assert signal.target_weights == {"CASH": 1.0}
