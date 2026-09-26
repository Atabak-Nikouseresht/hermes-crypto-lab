import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

_SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if os.environ.get("HCL_MUTATION_PACKAGE") == "mutation_targets":
    from mutation_targets.config_validation import (
        require_boolean,
        require_integer,
        require_mapping,
        require_number,
        require_string,
        require_string_list,
    )
    from mutation_targets.costs import ExecutionCostModel
    from mutation_targets.schedule import (
        schedule_target_from_start,
        schedule_timestamps,
        schedule_window_end,
        target_offset_minutes,
    )
else:
    if str(_SOURCE_DIRECTORY) not in sys.path:
        sys.path.insert(0, str(_SOURCE_DIRECTORY))
    from config_validation import (
        require_boolean,
        require_integer,
        require_mapping,
        require_number,
        require_string,
        require_string_list,
    )
    from costs import ExecutionCostModel
    from schedule import (
        schedule_target_from_start,
        schedule_timestamps,
        schedule_window_end,
        target_offset_minutes,
    )


def test_mutation_assurance_imports_target_without_src_prefix():
    mutation_targets = (require_mapping, schedule_timestamps, ExecutionCostModel)
    assert all(not target.__module__.startswith("src.") for target in mutation_targets)
    if os.environ.get("HCL_MUTATION_PACKAGE") == "mutation_targets":
        assert all(
            target.__module__.startswith("mutation_targets.")
            for target in mutation_targets
        )


def test_require_mapping_accepts_only_plain_dicts():
    expected = {"strategy": "paper"}
    assert require_mapping(expected, "config") is expected

    class CustomDict(dict):
        pass

    for value in (CustomDict(expected), None, [], (), "value", 1):
        with pytest.raises(ValueError, match="must be a mapping"):
            require_mapping(value, "config")


def test_require_boolean_rejects_coercions():
    assert require_boolean(True, "enabled") is True
    assert require_boolean(False, "enabled") is False
    for value in (0, 1, "true", None, []):
        with pytest.raises(ValueError, match="must be a boolean"):
            require_boolean(value, "enabled")


def test_require_integer_enforces_exact_type_and_bounds():
    assert require_integer(1, "count", minimum=1, maximum=3) == 1
    assert require_integer(3, "count", minimum=1, maximum=3) == 3
    for value in (True, False, 1.0, "1", None):
        with pytest.raises(ValueError, match="exact integer"):
            require_integer(value, "count")
    with pytest.raises(ValueError, match="at least"):
        require_integer(0, "count", minimum=1)
    with pytest.raises(ValueError, match="at most"):
        require_integer(4, "count", maximum=3)


def test_require_number_rejects_invalid_values_and_applies_limits():
    assert require_number(1, "rate", minimum=1, maximum=2) == 1
    assert require_number(2, "rate", minimum=1, maximum=2) == 2
    for value in (True, False, math.nan, math.inf, -math.inf, "1", None):
        with pytest.raises(ValueError, match="finite number"):
            require_number(value, "rate")
    with pytest.raises(ValueError, match="greater than"):
        require_number(1, "rate", minimum=1, minimum_exclusive=True)
    with pytest.raises(ValueError, match="less than"):
        require_number(2, "rate", maximum=2, maximum_exclusive=True)
    with pytest.raises(ValueError, match="at least"):
        require_number(0, "rate", minimum=1)
    with pytest.raises(ValueError, match="at most"):
        require_number(3, "rate", maximum=2)


def test_require_string_and_string_list_reject_invalid_values():
    assert require_string(" paper ", "mode") == " paper "
    for value in (None, 1, True, "", " \t\n"):
        with pytest.raises(ValueError, match="nonempty string"):
            require_string(value, "mode")
    values = ["BTC/USDT", "ETH/USDT"]
    assert require_string_list(values, "assets") is values
    assert require_string_list(["BTC", "BTC"], "assets") == ["BTC", "BTC"]
    with pytest.raises(ValueError, match="nonempty list"):
        require_string_list([], "assets")
    for value in (None, "BTC", ("BTC",), ["BTC", 1], [" "]):
        with pytest.raises(ValueError):
            require_string_list(value, "assets")
    with pytest.raises(ValueError, match="duplicates"):
        require_string_list(["BTC", "BTC"], "assets", unique=True)


def test_schedule_offsets_are_exact_and_wrap_within_an_hour():
    assert target_offset_minutes(5, 10) == 5
    assert target_offset_minutes(10, 10) == 0
    assert target_offset_minutes(55, 5) == 10
    for value in (True, False, -1, 60, 1.0, "5", None):
        with pytest.raises(ValueError, match="exact integer"):
            target_offset_minutes(value, 10)
        with pytest.raises(ValueError, match="exact integer"):
            target_offset_minutes(5, value)


def test_schedule_timestamps_convert_to_utc_and_roll_over_midnight():
    config = SimpleNamespace(
        schedule_hour=23,
        schedule_minute=55,
        execution_target_minute=5,
        schedule_window_minutes=20,
    )
    start, target, end = schedule_timestamps(
        config, pd.Timestamp("2026-01-05", tz="UTC")
    )
    assert start == pd.Timestamp("2026-01-05T23:55:00Z")
    assert target == pd.Timestamp("2026-01-06T00:05:00Z")
    assert end == pd.Timestamp("2026-01-06T00:15:00Z")
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_timestamps(config, pd.Timestamp("2026-01-05"))


def test_schedule_deadline_helpers_validate_persisted_identity():
    config = SimpleNamespace(
        schedule_hour=0,
        schedule_minute=5,
        execution_target_minute=10,
        schedule_window_minutes=15,
    )
    start = pd.Timestamp("2026-01-05T00:05:00Z")
    assert schedule_target_from_start(config, start) == pd.Timestamp(
        "2026-01-05T00:10:00Z"
    )
    assert schedule_window_end(config, start) == pd.Timestamp(
        "2026-01-05T00:20:00Z"
    )
    for helper in (schedule_target_from_start, schedule_window_end):
        with pytest.raises(ValueError, match="timezone-aware"):
            helper(config, pd.Timestamp("2026-01-05T00:05:00"))
        with pytest.raises(ValueError, match="inconsistent"):
            helper(config, pd.Timestamp("2026-01-05T00:06:00Z"))


def test_execution_cost_model_applies_fee_and_symmetric_slippage():
    model = ExecutionCostModel()
    assert model.execution_price(100.0, "BUY") == pytest.approx(100.05)
    assert model.execution_price(100.0, "SELL") == pytest.approx(99.95)
    assert model.execution_price(100.0, "buy") == pytest.approx(100.05)
    assert model.fee(2.0, 100.0) == pytest.approx(0.2)
    assert model.fee(-2.0, 100.0) == pytest.approx(0.2)
    assert model.fee(0.0, 100.0) == 0.0
    zero_costs = ExecutionCostModel(fee_rate=0.0, slippage_rate=0.0)
    assert zero_costs.execution_price(100.0, "BUY") == 100.0
    assert zero_costs.fee(1.0, 100.0) == 0.0


def test_execution_cost_model_rejects_invalid_rates_prices_sides_and_quantities():
    for field in ("fee_rate", "slippage_rate"):
        for value in (True, False, "0.01", math.nan, math.inf, -math.inf, -0.01, 1.0):
            with pytest.raises(ValueError, match=field):
                ExecutionCostModel(**{field: value})
    for price in (True, False, 0, -1, math.nan, math.inf, -math.inf, "100"):
        with pytest.raises(ValueError, match="market_price"):
            ExecutionCostModel().execution_price(price, "BUY")
    for side in ("HOLD", "buy-now", "", None, 1):
        with pytest.raises(ValueError, match="side|Unsupported"):
            ExecutionCostModel().execution_price(100.0, side)
    for quantity in (True, False, "1", math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="quantity"):
            ExecutionCostModel().fee(quantity, 100.0)
    for price in (True, False, "100", math.nan, math.inf, -math.inf, 0, -1):
        with pytest.raises(ValueError, match="execution_price"):
            ExecutionCostModel().fee(1.0, price)
