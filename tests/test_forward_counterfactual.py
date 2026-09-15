import pytest

from src.forward_counterfactual import replay_no_cost_counterfactual


def test_no_cost_replay_resizes_later_buy_from_same_persisted_order_intent():
    steps = [
        {
            "timestamp": "2026-08-03T00:10:00Z",
            "prices": {"BTC/USDT": 100.0},
            "orders": [{
                "symbol": "BTC/USDT", "side": "BUY", "target_weight": 1.0,
                "execution_price": 110.0, "mid_price": 100.0, "quantity": 9.0, "fee": 10.0,
            }],
        },
        {
            "timestamp": "2026-08-10T00:10:00Z",
            "prices": {"BTC/USDT": 200.0, "ETH/USDT": 100.0},
            "orders": [
                {
                    "symbol": "BTC/USDT", "side": "SELL", "target_weight": 0.0,
                    "execution_price": 190.0, "mid_price": 200.0, "quantity": 9.0, "fee": 10.0,
                },
                {
                    "symbol": "ETH/USDT", "side": "BUY", "target_weight": 1.0,
                    "execution_price": 110.0, "mid_price": 100.0, "quantity": 17.0, "fee": 10.0,
                },
            ],
        },
    ]

    result = replay_no_cost_counterfactual(
        starting_cash=1_000.0, starting_positions={}, steps=steps
    )

    assert result["no_fee"]["equity"][-1] > result["net_replay"]["equity"][-1]
    assert result["frictionless"]["equity"][-1] > result["no_fee"]["equity"][-1]
    assert result["frictionless"]["equity"][-1] != pytest.approx(
        result["net_replay"]["equity"][-1] + 30.0
    )


def test_zero_cost_and_no_trade_counterfactuals_equal_net_path():
    no_trade = replay_no_cost_counterfactual(
        starting_cash=100.0,
        starting_positions={"BTC/USDT": 1.0},
        steps=[{"timestamp": "2026-08-03T00:10:00Z", "prices": {"BTC/USDT": 120.0}, "orders": []}],
    )
    zero_cost = replay_no_cost_counterfactual(
        starting_cash=100.0,
        starting_positions={},
        steps=[{
            "timestamp": "2026-08-03T00:10:00Z",
            "prices": {"BTC/USDT": 100.0},
            "orders": [{
                "symbol": "BTC/USDT", "side": "BUY", "target_weight": 1.0,
                "execution_price": 100.0, "mid_price": 100.0, "quantity": 1.0, "fee": 0.0,
            }],
        }],
    )

    assert no_trade["net_replay"]["equity"] == no_trade["no_fee"]["equity"] == no_trade["frictionless"]["equity"]
    assert zero_cost["net_replay"]["equity"] == zero_cost["no_fee"]["equity"] == zero_cost["frictionless"]["equity"]


def test_fully_invested_factual_anchor_with_zero_cash_is_replayable():
    result = replay_no_cost_counterfactual(
        starting_cash=0.0,
        starting_positions={"BTC/USDT": 1.0},
        steps=[{"timestamp": "2026-08-03T00:10:00Z", "prices": {"BTC/USDT": 120.0}, "orders": []}],
    )

    assert result["frictionless"]["equity"] == [120.0]
