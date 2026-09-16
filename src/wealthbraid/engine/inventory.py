"""Multi-commodity balances: the :class:`Inventory` value type.

An inventory is a set of amounts keyed by commodity — the natural result of
summing postings that may span several currencies. It is immutable: combining
inventories yields new inventories. Zero balances are dropped so that a fully
offset commodity does not clutter reports, and :meth:`amounts` returns a
deterministic, commodity-sorted view.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal

from wealthbraid.engine.money import Amount, Commodity

_ZERO = Decimal(0)


class Inventory:
    """An immutable collection of amounts, at most one per commodity.

    Commodities whose net quantity is zero are not stored, so an empty inventory
    represents a fully balanced position.
    """

    __slots__ = ("_balances",)

    def __init__(self, balances: Mapping[Commodity, Decimal] | None = None) -> None:
        """Build an inventory from a commodity-to-quantity mapping.

        Args:
            balances: Optional mapping of commodity to net quantity. Zero
                quantities are dropped.

        """
        stored: dict[Commodity, Decimal] = {}
        if balances:
            for commodity, quantity in balances.items():
                if quantity != _ZERO:
                    stored[commodity] = quantity
        self._balances = stored

    @classmethod
    def from_amounts(cls, amounts: Iterable[Amount]) -> Inventory:
        """Build an inventory by summing amounts per commodity.

        Args:
            amounts: The amounts to accumulate.

        Returns:
            The resulting :class:`Inventory`.

        """
        totals: dict[Commodity, Decimal] = {}
        for amount in amounts:
            totals[amount.commodity] = totals.get(amount.commodity, _ZERO) + amount.quantity
        return cls(totals)

    def get(self, commodity: Commodity) -> Decimal:
        """Return the net quantity held in ``commodity``.

        Args:
            commodity: The commodity to look up.

        Returns:
            The net quantity, or zero if the commodity is not held.

        """
        return self._balances.get(commodity, _ZERO)

    def add_amount(self, amount: Amount) -> Inventory:
        """Return a new inventory with ``amount`` added.

        Args:
            amount: The amount to add.

        Returns:
            A new :class:`Inventory`.

        """
        totals = dict(self._balances)
        totals[amount.commodity] = totals.get(amount.commodity, _ZERO) + amount.quantity
        return Inventory(totals)

    def merge(self, other: Inventory) -> Inventory:
        """Return a new inventory combining this one with ``other``.

        Args:
            other: The inventory to merge in.

        Returns:
            A new :class:`Inventory` holding the summed balances.

        """
        totals = dict(self._balances)
        for commodity, quantity in other._balances.items():
            totals[commodity] = totals.get(commodity, _ZERO) + quantity
        return Inventory(totals)

    def amounts(self) -> list[Amount]:
        """Return the held amounts, sorted by commodity code.

        Returns:
            A deterministic list of non-zero :class:`Amount` values.

        """
        return [
            Amount(self._balances[commodity], commodity) for commodity in sorted(self._balances, key=lambda c: c.code)
        ]

    def is_empty(self) -> bool:
        """Report whether the inventory holds no non-zero balances.

        Returns:
            ``True`` if empty (fully balanced).

        """
        return not self._balances

    def __eq__(self, other: object) -> bool:
        """Compare inventories by their stored balances.

        Args:
            other: The object to compare against.

        Returns:
            ``True`` if both hold identical non-zero balances.

        """
        if not isinstance(other, Inventory):
            return NotImplemented
        return self._balances == other._balances

    def __hash__(self) -> int:
        """Return a hash consistent with equality.

        Returns:
            A hash over the stored (commodity, quantity) balances.

        """
        return hash(frozenset(self._balances.items()))

    def __repr__(self) -> str:
        """Return a debug representation listing the held amounts.

        Returns:
            A string such as ``Inventory([10 USD, -5 EUR])``.

        """
        return f"Inventory([{', '.join(str(a) for a in self.amounts())}])"
