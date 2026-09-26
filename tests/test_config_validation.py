from __future__ import annotations

import math

import pytest

from src.config_validation import (
    require_boolean,
    require_integer,
    require_mapping,
    require_number,
    require_string,
    require_string_list,
)


def test_require_mapping_accepts_only_a_plain_mapping():
    expected = {"strategy": "paper"}
    assert require_mapping(expected, "config") is expected
    class CustomDict(dict):
        pass

    with pytest.raises(ValueError, match="must be a mapping"):
        require_mapping(CustomDict(expected), "config")


def test_require_mapping_rejects_non_dict_values():
    for value in (None, [], (), "value", 1):
        with pytest.raises(ValueError, match="must be a mapping"):
            require_mapping(value, "config")


def test_require_boolean_accepts_both_boolean_values_and_rejects_coercions():
    assert require_boolean(True, "enabled") is True
    assert require_boolean(False, "enabled") is False
    for value in (0, 1, "true", None, []):
        with pytest.raises(ValueError, match="must be a boolean"):
            require_boolean(value, "enabled")


def test_require_integer_enforces_exact_type_and_inclusive_bounds():
    assert require_integer(3, "count", minimum=1, maximum=3) == 3
    assert require_integer(1, "count", minimum=1, maximum=3) == 1
    for value in (True, False, 1.0, "1", None):
        with pytest.raises(ValueError, match="exact integer"):
            require_integer(value, "count")
    with pytest.raises(ValueError, match="at least"):
        require_integer(0, "count", minimum=1)
    with pytest.raises(ValueError, match="at most"):
        require_integer(4, "count", maximum=3)


@pytest.mark.parametrize("value", [0, 1, -1, 0.5])
def test_require_number_accepts_finite_real_values(value):
    assert require_number(value, "rate") == float(value)


@pytest.mark.parametrize("value", [True, False, math.nan, math.inf, -math.inf, "1", None])
def test_require_number_rejects_booleans_nonfinite_and_non_numeric_values(value):
    with pytest.raises(ValueError, match="finite number"):
        require_number(value, "rate")


def test_require_number_applies_inclusive_and_exclusive_limits():
    assert require_number(1, "rate", minimum=1, maximum=2) == 1
    assert require_number(2, "rate", minimum=1, maximum=2) == 2
    with pytest.raises(ValueError, match="greater than"):
        require_number(1, "rate", minimum=1, minimum_exclusive=True)
    with pytest.raises(ValueError, match="less than"):
        require_number(2, "rate", maximum=2, maximum_exclusive=True)
    with pytest.raises(ValueError, match="at least"):
        require_number(0, "rate", minimum=1)
    with pytest.raises(ValueError, match="at most"):
        require_number(3, "rate", maximum=2)


def test_require_string_rejects_non_strings_and_empty_text():
    assert require_string(" paper ", "mode") == " paper "
    for value in (None, 1, True, "", " \t\n"):
        with pytest.raises(ValueError, match="nonempty string"):
            require_string(value, "mode")


def test_require_string_list_preserves_values_and_checks_uniqueness():
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
