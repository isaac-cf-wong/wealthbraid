"""Tests for the Inventory multi-commodity balance type."""

from __future__ import annotations

from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.money import Amount, Commodity


def test_from_amounts_sums_per_commodity():
    """Amounts accumulate per commodity."""
    inv = Inventory.from_amounts([Amount.of("3", "USD"), Amount.of("4", "USD"), Amount.of("2", "EUR")])
    assert inv.get(Commodity("USD")) == Amount.of("7", "USD").quantity
    assert inv.get(Commodity("EUR")) == Amount.of("2", "EUR").quantity


def test_zero_balances_are_dropped():
    """A commodity that nets to zero is not retained."""
    inv = Inventory.from_amounts([Amount.of("5", "USD"), Amount.of("-5", "USD")])
    assert inv.is_empty()
    assert inv.get(Commodity("USD")) == 0


def test_get_absent_commodity_is_zero():
    """Absent commodities report a zero balance."""
    assert Inventory().get(Commodity("USD")) == 0


def test_amounts_sorted_by_commodity():
    """The amounts view is sorted by commodity code for determinism."""
    inv = Inventory.from_amounts([Amount.of("1", "USD"), Amount.of("1", "EUR"), Amount.of("1", "BTC")])
    assert [a.commodity.code for a in inv.amounts()] == ["BTC", "EUR", "USD"]


def test_add_amount_is_immutable():
    """add_amount returns a new inventory and leaves the original unchanged."""
    original = Inventory.from_amounts([Amount.of("1", "USD")])
    extended = original.add_amount(Amount.of("2", "USD"))
    assert original.get(Commodity("USD")) == Amount.of("1", "USD").quantity
    assert extended.get(Commodity("USD")) == Amount.of("3", "USD").quantity


def test_merge_combines_two_inventories():
    """Merging sums balances across both inventories."""
    a = Inventory.from_amounts([Amount.of("1", "USD"), Amount.of("2", "EUR")])
    b = Inventory.from_amounts([Amount.of("3", "USD")])
    merged = a.merge(b)
    assert merged.get(Commodity("USD")) == Amount.of("4", "USD").quantity
    assert merged.get(Commodity("EUR")) == Amount.of("2", "EUR").quantity


def test_equality_and_hash():
    """Inventories compare and hash by their non-zero balances."""
    a = Inventory.from_amounts([Amount.of("1", "USD")])
    b = Inventory.from_amounts([Amount.of("1", "USD")])
    assert a == b
    assert hash(a) == hash(b)
