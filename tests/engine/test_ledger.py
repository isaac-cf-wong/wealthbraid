"""Tests for the Ledger aggregate: strict accounts, storage, and balances."""

from __future__ import annotations

import datetime as dt

import pytest

from wealthbraid.engine.account import Account
from wealthbraid.engine.errors import DuplicateAccountError, UndeclaredAccountError
from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.ledger import Ledger
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.transaction import Posting, Transaction

DATE = dt.date(2026, 7, 4)


def _ledger(*names: str) -> Ledger:
    ledger = Ledger()
    for name in names:
        ledger.declare_account(Account(name))
    return ledger


def test_declare_and_lookup_account():
    """Declared accounts are retrievable and reported as declared."""
    ledger = _ledger("Assets:Cash")
    assert ledger.is_declared("Assets:Cash")
    assert ledger.account("Assets:Cash").name == "Assets:Cash"


def test_duplicate_account_declaration_raises():
    """Declaring the same account twice is an error."""
    ledger = _ledger("Assets:Cash")
    with pytest.raises(DuplicateAccountError):
        ledger.declare_account(Account("Assets:Cash"))


def test_lookup_undeclared_account_raises():
    """Looking up an undeclared account raises."""
    with pytest.raises(UndeclaredAccountError):
        Ledger().account("Assets:Cash")


def test_accounts_are_sorted():
    """Accounts are returned in name-sorted order."""
    ledger = _ledger("Income:Salary", "Assets:Cash", "Expenses:Food")
    assert [a.name for a in ledger.accounts()] == ["Assets:Cash", "Expenses:Food", "Income:Salary"]


def test_add_transaction_requires_declared_accounts():
    """Posting to an undeclared account is rejected (strict accounts)."""
    ledger = _ledger("Assets:Cash")
    txn = Transaction(
        date=DATE,
        postings=(Posting("Assets:Cash", Amount.of("10", "USD")), Posting("Income:Salary", Amount.of("-10", "USD"))),
    )
    with pytest.raises(UndeclaredAccountError):
        ledger.add_transaction(txn)


def test_add_transaction_balances_and_stores():
    """Adding a transaction infers elided amounts and stores the balanced form."""
    ledger = _ledger("Assets:Cash", "Income:Salary")
    txn = Transaction(
        date=DATE,
        postings=(Posting("Assets:Cash", Amount.of("10.00", "USD")), Posting("Income:Salary")),
    )
    stored = ledger.add_transaction(txn)
    assert stored.postings[1].amount == Amount.of("-10.00", "USD")
    assert ledger.transactions() == [stored]


def test_account_balances_include_all_declared():
    """Every declared account appears in the balances, empty if inactive."""
    ledger = _ledger("Assets:Cash", "Income:Salary", "Expenses:Food")
    ledger.add_transaction(
        Transaction(
            date=DATE,
            postings=(
                Posting("Assets:Cash", Amount.of("10.00", "USD")),
                Posting("Income:Salary", Amount.of("-10.00", "USD")),
            ),
        )
    )
    balances = ledger.account_balances()
    assert balances["Assets:Cash"].get(Commodity("USD")) == Amount.of("10.00", "USD").quantity
    assert balances["Expenses:Food"] == Inventory()


def test_balance_rolls_up_subaccounts():
    """A parent balance rolls up its descendants when requested."""
    ledger = _ledger("Assets:Bank:Checking", "Assets:Bank:Savings", "Income:Salary")
    ledger.add_transaction(
        Transaction(
            date=DATE,
            postings=(
                Posting("Assets:Bank:Checking", Amount.of("70.00", "USD")),
                Posting("Assets:Bank:Savings", Amount.of("30.00", "USD")),
                Posting("Income:Salary", Amount.of("-100.00", "USD")),
            ),
        )
    )
    rolled = ledger.balance("Assets:Bank")
    assert rolled.get(Commodity("USD")) == Amount.of("100.00", "USD").quantity
    leaf = ledger.balance("Assets:Bank:Checking", include_subaccounts=False)
    assert leaf.get(Commodity("USD")) == Amount.of("70.00", "USD").quantity


def test_multi_currency_balances():
    """Balances track each commodity independently."""
    ledger = _ledger("Assets:USD", "Assets:EUR", "Equity:Opening")
    ledger.add_transaction(
        Transaction(
            date=DATE,
            postings=(
                Posting("Assets:USD", Amount.of("100.00", "USD")),
                Posting("Assets:EUR", Amount.of("50.00", "EUR")),
                Posting("Equity:Opening", Amount.of("-100.00", "USD")),
                Posting("Equity:Opening", Amount.of("-50.00", "EUR")),
            ),
        )
    )
    opening = ledger.balance("Equity:Opening")
    assert opening.get(Commodity("USD")) == Amount.of("-100.00", "USD").quantity
    assert opening.get(Commodity("EUR")) == Amount.of("-50.00", "EUR").quantity


def test_transactions_returns_a_copy():
    """The returned transaction list is a copy, not the internal store."""
    ledger = _ledger("Assets:Cash", "Income:Salary")
    ledger.add_transaction(
        Transaction(
            date=DATE,
            postings=(
                Posting("Assets:Cash", Amount.of("1", "USD")),
                Posting("Income:Salary", Amount.of("-1", "USD")),
            ),
        )
    )
    snapshot = ledger.transactions()
    snapshot.clear()
    assert len(ledger.transactions()) == 1
