"""Prices and cross-commodity valuation.

A :class:`Price` records an exchange rate for a commodity as of a date, for
example ``1 EUR = 1.10 USD``. A :class:`PriceDB` answers rate queries using the
most recent price on or before a date, considering both the direct rate and its
inverse. Valuation converts amounts and inventories into a target commodity;
commodities without a usable rate are left unconverted rather than silently
dropped, keeping conversions explicit.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.money import Amount, Commodity

_ONE = Decimal(1)


@dataclass(frozen=True)
class Price:
    """An exchange rate for a commodity as of a date.

    Attributes:
        date: The date the rate applies from.
        base: The commodity being priced.
        rate: The value of one unit of ``base``, denominated in the quote
            commodity (``rate.commodity``).

    """

    date: dt.date
    base: Commodity
    rate: Amount

    @property
    def quote(self) -> Commodity:
        """Return the quote commodity of the rate.

        Returns:
            The commodity ``rate`` is denominated in.

        """
        return self.rate.commodity


class PriceDB:
    """A queryable collection of price records."""

    def __init__(self, prices: list[Price] | None = None) -> None:
        """Create a price database.

        Args:
            prices: Optional initial prices.

        """
        self._prices: list[Price] = list(prices) if prices else []

    def add(self, price: Price) -> None:
        """Add a price record.

        Args:
            price: The price to add.

        """
        self._prices.append(price)

    def prices(self) -> list[Price]:
        """Return all prices, sorted by date then base then quote.

        Returns:
            A deterministic, sorted list of prices.

        """
        return sorted(self._prices, key=lambda p: (p.date, p.base.code, p.quote.code))

    def rate(self, base: Commodity, quote: Commodity, on: dt.date) -> Decimal | None:
        """Return the rate to convert ``base`` into ``quote`` as of a date.

        Rates chain transitively: if there is no direct (or inverse) price, the
        shortest path through intermediate commodities is used (for example
        ``VWCG → EUR → USD``). Each edge uses the most recent price on or before
        ``on``. When several equal-length paths exist, neighbours are visited in
        commodity-code order so the result is deterministic.

        Args:
            base: The commodity to convert from.
            quote: The commodity to convert to.
            on: The as-of date.

        Returns:
            The conversion rate, or None if no path of prices connects them.

        """
        if base == quote:
            return _ONE
        adjacency = self._adjacency(on)
        queue: deque[tuple[Commodity, Decimal]] = deque([(base, _ONE)])
        visited = {base}
        while queue:
            node, accumulated = queue.popleft()
            if node == quote:
                return accumulated
            for neighbour in sorted(adjacency[node], key=lambda c: c.code):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append((neighbour, accumulated * adjacency[node][neighbour]))
        return None

    def _adjacency(self, on: dt.date) -> dict[Commodity, dict[Commodity, Decimal]]:
        """Build the rate graph from the most recent prices as of a date.

        Direct edges use the latest ``base → quote`` price; inverse edges are
        added only where no direct edge already exists.

        Args:
            on: The as-of date.

        Returns:
            A mapping of commodity to its neighbours and the rate to each.

        """
        best: dict[tuple[Commodity, Commodity], tuple[dt.date, Decimal]] = {}
        for price in self._prices:
            if price.date > on:
                continue
            key = (price.base, price.quote)
            if key not in best or price.date > best[key][0]:
                best[key] = (price.date, price.rate.quantity)

        adjacency: dict[Commodity, dict[Commodity, Decimal]] = defaultdict(dict)
        for (edge_base, edge_quote), (_, edge_rate) in best.items():
            adjacency[edge_base][edge_quote] = edge_rate
        for (edge_base, edge_quote), (_, edge_rate) in best.items():
            if edge_rate != 0 and edge_base not in adjacency[edge_quote]:
                adjacency[edge_quote][edge_base] = _ONE / edge_rate
        return adjacency


def value_amount(amount: Amount, target: Commodity, prices: PriceDB, on: dt.date) -> Amount | None:
    """Convert an amount into the target commodity as of a date.

    Args:
        amount: The amount to convert.
        target: The target commodity.
        prices: The price database.
        on: The as-of date.

    Returns:
        The converted amount, or None if no usable rate exists.

    """
    rate = prices.rate(amount.commodity, target, on)
    if rate is None:
        return None
    return Amount(amount.quantity * rate, target)


def value_inventory(inventory: Inventory, target: Commodity, prices: PriceDB, on: dt.date) -> Inventory:
    """Convert an inventory into the target commodity where possible.

    Amounts that cannot be converted (no usable rate) are retained in their
    original commodity, so the result is explicit about what was valued.

    Args:
        inventory: The inventory to value.
        target: The target commodity.
        prices: The price database.
        on: The as-of date.

    Returns:
        A new :class:`Inventory` with convertible holdings expressed in
        ``target`` and the rest unchanged.

    """
    converted: list[Amount] = []
    for amount in inventory.amounts():
        valued = value_amount(amount, target, prices, on)
        converted.append(valued if valued is not None else amount)
    return Inventory.from_amounts(converted)
