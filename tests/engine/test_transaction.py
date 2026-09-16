"""Tests for transactions and postings."""

from __future__ import annotations

import datetime as dt

import pytest

from wealthbraid.engine.errors import InvalidTransactionError
from wealthbraid.engine.money import Amount
from wealthbraid.engine.transaction import Posting, Transaction

DATE = dt.date(2026, 7, 4)


def _txn(**overrides) -> Transaction:
    postings = overrides.pop(
        "postings",
        (Posting("Assets:Cash", Amount.of("10", "USD")), Posting("Income:Salary", Amount.of("-10", "USD"))),
    )
    return Transaction(date=DATE, postings=postings, **overrides)


def test_transaction_requires_postings():
    """A transaction with no postings is invalid."""
    with pytest.raises(InvalidTransactionError):
        Transaction(date=DATE, postings=())


def test_postings_normalised_to_tuple():
    """A list of postings is stored as a tuple."""
    txn = Transaction(date=DATE, postings=[Posting("Assets:Cash", Amount.of("1", "USD"))])
    assert isinstance(txn.postings, tuple)


def test_elided_postings_indices():
    """Elided postings are reported by index."""
    txn = _txn(postings=(Posting("Assets:Cash", Amount.of("10", "USD")), Posting("Income:Salary")))
    assert txn.elided_postings == (1,)


def test_posting_with_amount():
    """Posting.with_amount fills in a concrete amount."""
    filled = Posting("Income:Salary").with_amount(Amount.of("-10", "USD"))
    assert filled.amount == Amount.of("-10", "USD")


def test_with_postings():
    """with_postings returns an updated copy."""
    txn = _txn()
    new_postings = (Posting("Assets:Cash", Amount.of("1", "USD")), Posting("Income:Salary", Amount.of("-1", "USD")))
    assert txn.with_postings(new_postings).postings == new_postings
