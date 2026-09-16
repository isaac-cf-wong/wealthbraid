"""Pure accounting engine.

This package holds the accounting core: value types, invariants, balancing, and
report mathematics. It depends on nothing outside the standard library and must
never import from :mod:`wealthbraid.cli`, :mod:`wealthbraid.app`, or
:mod:`wealthbraid.persistence`. Give it in-memory objects and it enforces
double-entry accounting rules; it performs no I/O and knows nothing about
serialization formats.
"""

from __future__ import annotations

from wealthbraid.engine.account import Account, AccountType, parse_account_name
from wealthbraid.engine.balancing import balance_transaction, infer_tolerances, is_balanced, residual
from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.ledger import Ledger
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.prices import Price, PriceDB, value_amount, value_inventory
from wealthbraid.engine.transaction import Posting, Transaction

__all__ = [
    "Account",
    "AccountType",
    "Amount",
    "Commodity",
    "Inventory",
    "Ledger",
    "Posting",
    "Price",
    "PriceDB",
    "Transaction",
    "balance_transaction",
    "infer_tolerances",
    "is_balanced",
    "parse_account_name",
    "residual",
    "value_amount",
    "value_inventory",
]
