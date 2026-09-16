"""Tests for the money value types (Commodity and Amount)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wealthbraid.engine.errors import (
    CommodityMismatchError,
    InvalidAmountError,
    InvalidCommodityError,
)
from wealthbraid.engine.money import Amount, Commodity


@pytest.mark.parametrize("code", ["USD", "EUR", "BTC", "AAPL", "VANGUARD-500", "X", "USTBillOct", "IBOND-04239"])
def test_valid_commodity_codes(code):
    """Well-formed commodity codes are accepted (uppercase first, then mixed)."""
    assert Commodity(code).code == code


@pytest.mark.parametrize("code", ["", "usd", "1USD", "US D", "US:D", "$", "04239"])
def test_invalid_commodity_codes(code):
    """Malformed commodity codes are rejected."""
    with pytest.raises(InvalidCommodityError):
        Commodity(code)


def test_amount_rejects_float():
    """Floats are rejected at construction to avoid rounding error."""
    with pytest.raises(InvalidAmountError):
        Amount(1.5, Commodity("USD"))


def test_amount_rejects_bool():
    """Booleans are not valid quantities."""
    with pytest.raises(InvalidAmountError):
        Amount(True, Commodity("USD"))


def test_amount_rejects_non_finite():
    """NaN and infinities are rejected."""
    with pytest.raises(InvalidAmountError):
        Amount(Decimal("NaN"), Commodity("USD"))
    with pytest.raises(InvalidAmountError):
        Amount("Infinity", Commodity("USD"))


def test_amount_coerces_int_and_str():
    """Integers and decimal strings coerce to Decimal quantities."""
    assert Amount(5, Commodity("USD")).quantity == Decimal(5)
    assert Amount("5.50", Commodity("USD")).quantity == Decimal("5.50")


def test_amount_of_helper():
    """Amount.of pairs a quantity with a commodity code."""
    amount = Amount.of("10.00", "USD")
    assert amount.quantity == Decimal("10.00")
    assert amount.commodity == Commodity("USD")


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [("10", 0), ("10.0", 1), ("10.00", 2), ("0.005", 3), ("100", 0)],
)
def test_fractional_digits(quantity, expected):
    """Fractional digit count reflects the Decimal exponent."""
    assert Amount.of(quantity, "USD").fractional_digits == expected


def test_addition_same_commodity():
    """Amounts of the same commodity add."""
    total = Amount.of("3.00", "USD") + Amount.of("4.50", "USD")
    assert total == Amount.of("7.50", "USD")


def test_addition_different_commodity_raises():
    """Adding across commodities is an error, not a coercion."""
    with pytest.raises(CommodityMismatchError):
        Amount.of("3.00", "USD") + Amount.of("4.50", "EUR")


def test_subtraction_and_negation():
    """Subtraction and negation behave as exact Decimal operations."""
    assert Amount.of("5", "USD") - Amount.of("2", "USD") == Amount.of("3", "USD")
    assert -Amount.of("5", "USD") == Amount.of("-5", "USD")


def test_multiplication_by_scalar():
    """Scaling multiplies the quantity exactly."""
    assert Amount.of("1.50", "USD") * 3 == Amount.of("4.50", "USD")
    assert Amount.of("2", "USD") * Decimal("2.5") == Amount.of("5.0", "USD")


def test_multiplication_by_float_rejected():
    """Scaling by a float is rejected."""
    with pytest.raises(InvalidAmountError):
        Amount.of("1.50", "USD") * 1.5


def test_is_zero_with_tolerance():
    """Zero detection honours an optional tolerance."""
    assert Amount.of("0", "USD").is_zero()
    assert not Amount.of("0.01", "USD").is_zero()
    assert Amount.of("0.001", "USD").is_zero(Decimal("0.005"))


def test_str_format():
    """Amounts render as ``<quantity> <commodity>``."""
    assert str(Amount.of("10.00", "USD")) == "10.00 USD"
