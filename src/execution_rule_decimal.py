"""Bounded exact Decimal validation for prospective public execution-rule evidence."""

from __future__ import annotations

from decimal import Decimal

# Binance Spot PRICE_RANGE multipliers and public reference prices are persisted as
# exact raw Decimal strings.  Reject absurd exponents before arithmetic so malformed
# public evidence cannot overflow the Decimal context or create underflowed bounds.
MAX_EXECUTION_RULE_DECIMAL_ABS_ADJUSTED_EXPONENT = 1_000


def is_bounded_execution_rule_decimal(value: Decimal) -> bool:
    """Whether a finite positive raw Decimal fits the prospective evidence envelope."""
    return (
        value.is_finite()
        and value > 0
        and abs(value.adjusted()) <= MAX_EXECUTION_RULE_DECIMAL_ABS_ADJUSTED_EXPONENT
    )
