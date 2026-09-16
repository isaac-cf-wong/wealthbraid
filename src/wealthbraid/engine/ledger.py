"""The :class:`Ledger` aggregate: accounts, transactions, and balances.

The ledger is the in-memory root of the accounting model. It enforces the two
structural invariants at the point of mutation: every posted account must have
been declared (strict accounts), and every stored transaction must balance. It
also computes account balances, including hierarchical roll-ups. It performs no
I/O; persistence lives behind the storage ports.
"""

from __future__ import annotations

from decimal import Decimal

from wealthbraid.engine.account import Account
from wealthbraid.engine.balancing import balance_transaction
from wealthbraid.engine.errors import (
    DuplicateAccountError,
    UndeclaredAccountError,
)
from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.prices import Price, PriceDB
from wealthbraid.engine.transaction import Transaction

_ZERO = Decimal(0)


class Ledger:
    """A collection of declared accounts, balanced transactions, and prices."""

    def __init__(self) -> None:
        """Initialise an empty ledger."""
        self._accounts: dict[str, Account] = {}
        self._transactions: list[Transaction] = []
        self._prices: list[Price] = []

    # -- accounts --------------------------------------------------------

    def declare_account(self, account: Account) -> None:
        """Register an account in the chart of accounts.

        Args:
            account: The account to declare.

        Raises:
            DuplicateAccountError: If an account with the same name already
                exists.

        """
        if account.name in self._accounts:
            raise DuplicateAccountError(f"Account already declared: {account.name}")
        self._accounts[account.name] = account

    def is_declared(self, name: str) -> bool:
        """Report whether an account name has been declared.

        Args:
            name: The account name to check.

        Returns:
            ``True`` if the account is declared.

        """
        return name in self._accounts

    def account(self, name: str) -> Account:
        """Return a declared account by name.

        Args:
            name: The account name.

        Returns:
            The declared :class:`Account`.

        Raises:
            UndeclaredAccountError: If the account has not been declared.

        """
        try:
            return self._accounts[name]
        except KeyError as exc:
            raise UndeclaredAccountError(f"Account not declared: {name}") from exc

    def accounts(self) -> list[Account]:
        """Return all declared accounts, sorted by name.

        Returns:
            A deterministic, name-sorted list of accounts.

        """
        return [self._accounts[name] for name in sorted(self._accounts)]

    # -- transactions ----------------------------------------------------

    def add_transaction(self, transaction: Transaction) -> Transaction:
        """Balance, validate, and store a transaction.

        The transaction is balanced (inferring at most one elided amount), every
        referenced account is checked against the chart of accounts, and the
        balanced transaction is appended to the ledger.

        Args:
            transaction: The transaction to add.

        Returns:
            The stored, balanced :class:`Transaction`.

        Raises:
            UndeclaredAccountError: If any posting references an undeclared
                account.
            AmbiguousBalanceError: If more than one posting elides its amount.
            UnresolvedElidedAmountError: If an elided amount cannot be inferred.
            BalanceError: If the transaction does not balance within tolerance.

        """
        for posting in transaction.postings:
            if posting.account not in self._accounts:
                raise UndeclaredAccountError(f"Account not declared: {posting.account}")
        balanced = balance_transaction(transaction)
        self._transactions.append(balanced)
        return balanced

    def transactions(self) -> list[Transaction]:
        """Return the stored transactions in insertion order.

        Returns:
            A shallow copy of the transaction list.

        """
        return list(self._transactions)

    # -- prices ----------------------------------------------------------

    def add_price(self, price: Price) -> None:
        """Record an exchange rate.

        Args:
            price: The price to add.

        """
        self._prices.append(price)

    def prices(self) -> list[Price]:
        """Return the recorded prices, sorted deterministically.

        Returns:
            The prices sorted by date, base, then quote.

        """
        return PriceDB(self._prices).prices()

    def price_db(self) -> PriceDB:
        """Return a price database over the ledger's prices.

        Returns:
            A :class:`~wealthbraid.engine.prices.PriceDB` for valuation queries.

        """
        return PriceDB(self._prices)

    # -- balances --------------------------------------------------------

    def account_balances(self) -> dict[str, Inventory]:
        """Return the leaf balance of every declared account.

        Every declared account is present in the result, with an empty inventory
        if it has no activity.

        Returns:
            A mapping of account name to its :class:`Inventory`.

        """
        totals: dict[str, dict] = {name: {} for name in self._accounts}
        for transaction in self._transactions:
            for posting in transaction.postings:
                if posting.amount is None:
                    continue
                commodity = posting.amount.commodity
                bucket = totals[posting.account]
                bucket[commodity] = bucket.get(commodity, _ZERO) + posting.amount.quantity
        return {name: Inventory(balances) for name, balances in totals.items()}

    def balance(self, name: str, *, include_subaccounts: bool = True) -> Inventory:
        """Return the balance of an account, optionally rolling up descendants.

        Args:
            name: The account name to total.
            include_subaccounts: If true, include all descendant accounts.

        Returns:
            The combined :class:`Inventory` for the account.

        """
        prefix = name + ":"
        result = Inventory()
        for account_name, inventory in self.account_balances().items():
            if account_name == name or (include_subaccounts and account_name.startswith(prefix)):
                result = result.merge(inventory)
        return result
