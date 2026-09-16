"""Tests for the transaction balancing algorithm and tolerance inference."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from wealthbraid.engine.balancing import (
    balance_transaction,
    infer_tolerances,
    is_balanced,
    residual,
)
from wealthbraid.engine.errors import (
    AmbiguousBalanceError,
    BalanceError,
    UnresolvedElidedAmountError,
)
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.transaction import Posting, Transaction

DATE = dt.date(2026, 7, 4)


def _txn(*postings: Posting) -> Transaction:
    return Transaction(date=DATE, postings=postings)


def test_infer_tolerances_two_decimals():
    """Two-decimal amounts get a half-cent tolerance."""
    tolerances = infer_tolerances([Posting("Assets:Cash", Amount.of("10.00", "USD"))])
    assert tolerances[Commodity("USD")] == Decimal("0.005")


def test_infer_tolerances_integers_are_exact():
    """Integer amounts require exact balance (zero tolerance)."""
    tolerances = infer_tolerances([Posting("Assets:Cash", Amount.of("10", "USD"))])
    assert tolerances[Commodity("USD")] == Decimal(0)


def test_infer_tolerances_uses_max_precision():
    """The tolerance follows the most precise amount for the commodity."""
    tolerances = infer_tolerances(
        [
            Posting("Assets:Cash", Amount.of("10.0", "USD")),
            Posting("Expenses:Food", Amount.of("-2.005", "USD")),
        ]
    )
    assert tolerances[Commodity("USD")] == Decimal("0.0005")


def test_residual_sums_per_commodity():
    """Residual reports the net per commodity across explicit postings."""
    totals = residual(
        [
            Posting("Assets:Cash", Amount.of("10", "USD")),
            Posting("Expenses:Food", Amount.of("-4", "USD")),
            Posting("Assets:Euro", Amount.of("3", "EUR")),
        ]
    )
    assert totals[Commodity("USD")] == Decimal(6)
    assert totals[Commodity("EUR")] == Decimal(3)


def test_exactly_balanced_transaction_passes():
    """A transaction that nets to zero is returned unchanged in balance."""
    txn = _txn(
        Posting("Assets:Cash", Amount.of("10.00", "USD")),
        Posting("Income:Salary", Amount.of("-10.00", "USD")),
    )
    balanced = balance_transaction(txn)
    assert balanced.postings == txn.postings


def test_single_elided_posting_is_inferred():
    """A single elided posting absorbs the residual."""
    txn = _txn(
        Posting("Assets:Cash", Amount.of("10.00", "USD")),
        Posting("Income:Salary"),
    )
    balanced = balance_transaction(txn)
    assert balanced.postings[1].amount == Amount.of("-10.00", "USD")


def test_multi_currency_balances_per_commodity():
    """Each commodity must independently net to zero."""
    txn = _txn(
        Posting("Assets:USD", Amount.of("10.00", "USD")),
        Posting("Income:USD", Amount.of("-10.00", "USD")),
        Posting("Assets:EUR", Amount.of("5.00", "EUR")),
        Posting("Income:EUR", Amount.of("-5.00", "EUR")),
    )
    assert is_balanced(txn)


def test_elided_with_multi_commodity_residual_is_ambiguous():
    """An elided posting cannot absorb a residual spanning commodities."""
    txn = _txn(
        Posting("Assets:USD", Amount.of("10.00", "USD")),
        Posting("Assets:EUR", Amount.of("5.00", "EUR")),
        Posting("Income:Mixed"),
    )
    with pytest.raises(UnresolvedElidedAmountError):
        balance_transaction(txn)


def test_elided_with_no_residual_is_unresolved():
    """An elided posting with nothing to absorb cannot be inferred."""
    txn = _txn(
        Posting("Assets:Cash", Amount.of("10.00", "USD")),
        Posting("Income:Salary", Amount.of("-10.00", "USD")),
        Posting("Expenses:Rounding"),
    )
    with pytest.raises(UnresolvedElidedAmountError):
        balance_transaction(txn)


def test_two_elided_postings_are_ambiguous():
    """At most one posting may elide its amount."""
    txn = _txn(Posting("Assets:Cash"), Posting("Income:Salary"))
    with pytest.raises(AmbiguousBalanceError):
        balance_transaction(txn)


def test_imbalanced_transaction_raises():
    """A residual beyond tolerance is a balance error."""
    txn = _txn(
        Posting("Assets:Cash", Amount.of("10.00", "USD")),
        Posting("Income:Salary", Amount.of("-9.99", "USD")),
    )
    with pytest.raises(BalanceError):
        balance_transaction(txn)


def test_tolerance_multiplier_admits_rounding():
    """A one-ulp residual balances when the tolerance multiplier allows it."""
    txn = _txn(
        Posting("Assets:Cash", Amount.of("10.00", "USD")),
        Posting("Income:Salary", Amount.of("-9.99", "USD")),
    )
    # Residual is 0.01; with multiplier 1.0 the tolerance is a full cent.
    balanced = balance_transaction(txn, multiplier=Decimal("1.0"))
    assert balanced.postings == txn.postings
    # The default (0.5) tolerance rejects it.
    assert not is_balanced(txn)
