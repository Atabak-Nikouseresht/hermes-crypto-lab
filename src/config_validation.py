"""Strict raw-YAML validators for governed configuration loaders."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a mapping")
    return value


def require_boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def require_integer(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an exact integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def require_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
    maximum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if minimum is not None and (number <= minimum if minimum_exclusive else number < minimum):
        operator = "greater than" if minimum_exclusive else "at least"
        raise ValueError(f"{name} must be {operator} {minimum}")
    if maximum is not None and (number >= maximum if maximum_exclusive else number > maximum):
        operator = "less than" if maximum_exclusive else "at most"
        raise ValueError(f"{name} must be {operator} {maximum}")
    return number


def require_string(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def require_string_list(value: Any, name: str, *, unique: bool = False) -> list[str]:
    if type(value) is not list or not value:
        raise ValueError(f"{name} must be a nonempty list")
    if any(type(item) is not str or not item.strip() for item in value):
        raise ValueError(f"{name} must contain only nonempty strings")
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicates")
    return value
