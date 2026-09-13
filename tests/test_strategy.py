import numpy as np
import pandas as pd
import pytest

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
    assert original.momentum_long_ex_skip == after_future_change.momentum_long_ex_skip
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


@pytest.mark.parametrize(("long", "skip"), [(90, 0), (90, 7), (120, 0)])
def test_momentum_skip_contract_and_no_lookahead(long, skip):
    # P[t] = t + 1: the denominator is P[t-long], not P[t-long-skip].
    prices = pd.DataFrame(
        {"BTC/USDT": np.arange(1.0, 162.0)},
        index=pd.date_range("2024-01-01", periods=161, tz="UTC"),
    )
    config = StrategyConfig(
        momentum_long_days=long, momentum_skip_days=skip,
        btc_moving_average_days=150, volatility_days=30,
    )
    signal = generate_signal(prices, as_of=prices.index[149], config=config)
    assert signal.momentum_long_ex_skip["BTC/USDT"] == (150 - skip) / (150 - long) - 1
    assert signal.momentum_short["BTC/USDT"] == 150 / 120 - 1
    assert signal.realized_volatility["BTC/USDT"] > 0
    assert signal.btc_above_trend_ma is True
    prices.iloc[150:] = 1_000_000
    assert generate_signal(prices, as_of=prices.index[149], config=config) == signal


@pytest.mark.parametrize(
    ("overrides", "required"),
    [
        ({"momentum_long_days": 90, "momentum_skip_days": 0}, 91),
        ({"momentum_long_days": 90, "momentum_skip_days": 7}, 91),
        ({"momentum_long_days": 120, "momentum_skip_days": 0,
          "btc_moving_average_days": 150}, 150),
        ({"momentum_short_days": 100}, 101),
        ({"momentum_skip_days": 95}, 96),
        ({"volatility_days": 100}, 101),
    ],
)
def test_signal_and_paper_history_boundaries_agree(tmp_path, overrides, required):
    from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem

    parameters = {"momentum_long_days": 90, "btc_moving_average_days": 30}
    config = StrategyConfig(**(parameters | overrides))
    prices = pd.DataFrame(
        {"BTC/USDT": np.arange(100.0, 100.0 + required)},
        index=pd.date_range("2024-01-01", periods=required, tz="UTC"),
    )
    as_of = prices.index[-1]
    with pytest.raises(ValueError, match=f"At least {required} observations"):
        generate_signal(prices.iloc[1:], as_of=as_of, config=config)
    signal = generate_signal(prices, as_of=as_of, config=config)
    assert signal.momentum_long_ex_skip["BTC/USDT"] == (
        prices.iloc[-(config.momentum_skip_days + 1), 0]
        / prices.iloc[-(config.momentum_long_days + 1), 0] - 1
    )
    paper = PaperConfig(assets=("BTC/USDT",), strategy_config=config, lookback_days=required)
    with pytest.raises(ValueError, match=f"lookback_days must be at least {required}"):
        PaperConfig(assets=("BTC/USDT",), strategy_config=config, lookback_days=required - 1)
    system = PaperTradingSystem(tmp_path / "paper.duckdb", paper)
    now = as_of + pd.Timedelta(days=1)
    snapshot = MarketSnapshot(closes=prices.iloc[1:], quotes={}, fetched_at=now)
    assert system._validate_snapshot(snapshot, now) == f"Missing data: requires at least {required} daily bars"
