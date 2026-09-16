"""Tests for prices and cross-commodity valuation."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.prices import Price, PriceDB, value_amount, value_inventory

USD = Commodity("USD")
EUR = Commodity("EUR")
GBP = Commodity("GBP")


def _price(day: int, base: str, rate: str, quote: str) -> Price:
    return Price(date=dt.date(2026, 7, day), base=Commodity(base), rate=Amount.of(rate, quote))


def test_same_commodity_rate_is_one():
    """Converting a commodity to itself is a no-op rate of 1."""
    assert PriceDB().rate(USD, USD, dt.date(2026, 7, 1)) == Decimal(1)


def test_direct_rate():
    """A direct price provides the rate."""
    db = PriceDB([_price(1, "EUR", "1.10", "USD")])
    assert db.rate(EUR, USD, dt.date(2026, 7, 5)) == Decimal("1.10")


def test_inverse_rate():
    """When only the inverse price exists, its reciprocal is used."""
    db = PriceDB([_price(1, "EUR", "1.25", "USD")])
    assert db.rate(USD, EUR, dt.date(2026, 7, 5)) == Decimal(1) / Decimal("1.25")


def test_most_recent_on_or_before():
    """The most recent price on or before the date wins."""
    db = PriceDB([_price(1, "EUR", "1.10", "USD"), _price(5, "EUR", "1.20", "USD")])
    assert db.rate(EUR, USD, dt.date(2026, 7, 4)) == Decimal("1.10")
    assert db.rate(EUR, USD, dt.date(2026, 7, 5)) == Decimal("1.20")


def test_no_price_returns_none():
    """An unknown pair has no rate."""
    assert PriceDB().rate(EUR, GBP, dt.date(2026, 7, 1)) is None


def test_transitive_chaining():
    """Rates chain through intermediate commodities (VWCG -> EUR -> USD)."""
    db = PriceDB([_price(1, "VWCG", "5", "EUR"), _price(1, "EUR", "1.10", "USD")])
    # 1 VWCG = 5 EUR = 5 * 1.10 = 5.50 USD.
    assert db.rate(Commodity("VWCG"), USD, dt.date(2026, 7, 5)) == Decimal("5.50")
    # The chain also works in reverse via inverse edges.
    assert db.rate(USD, Commodity("VWCG"), dt.date(2026, 7, 5)) == Decimal(1) / Decimal("5.50")


def test_future_price_is_ignored():
    """A price dated after the as-of date does not apply."""
    db = PriceDB([_price(5, "EUR", "1.10", "USD")])
    assert db.rate(EUR, USD, dt.date(2026, 7, 1)) is None


def test_value_amount_converts():
    """value_amount converts using the rate."""
    db = PriceDB([_price(1, "EUR", "1.10", "USD")])
    valued = value_amount(Amount.of("100", "EUR"), USD, db, dt.date(2026, 7, 5))
    assert valued == Amount.of("110.00", "USD")


def test_value_amount_without_rate_is_none():
    """value_amount returns None when no rate exists."""
    assert value_amount(Amount.of("100", "GBP"), USD, PriceDB(), dt.date(2026, 7, 1)) is None


def test_value_inventory_keeps_unconvertible():
    """Unconvertible commodities are retained in their original form."""
    db = PriceDB([_price(1, "EUR", "1.10", "USD")])
    inv = Inventory.from_amounts([Amount.of("100", "EUR"), Amount.of("50", "GBP"), Amount.of("10", "USD")])
    valued = value_inventory(inv, USD, db, dt.date(2026, 7, 5))
    assert valued.get(USD) == Decimal("120.00")  # 110 (from EUR) + 10 USD
    assert valued.get(GBP) == Decimal(50)  # unconverted
