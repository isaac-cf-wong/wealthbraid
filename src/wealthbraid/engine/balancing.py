"""Transaction balancing: the central accounting invariant.

A transaction must sum to zero **within each commodity independently**. This
module infers per-commodity tolerances from the precision of the amounts
involved, fills in at most one elided posting amount, and verifies that every
commodity nets to zero within tolerance.

Tolerance inference follows the standard rule: for a commodity whose most
precise amount has ``d`` fractional digits, the tolerance is half of the last
place, ``0.5 * 10**-d``. Amounts written as integers (``d == 0``) must balance
exactly.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from wealthbraid.engine.errors import (
    AmbiguousBalanceError,
    BalanceError,
    UnresolvedElidedAmountError,
)
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.transaction import Posting, Transaction

_ZERO = Decimal(0)
_DEFAULT_TOLERANCE_MULTIPLIER = Decimal("0.5")


def infer_tolerances(
    postings: Iterable[Posting],
    *,
    multiplier: Decimal = _DEFAULT_TOLERANCE_MULTIPLIER,
) -> dict[Commodity, Decimal]:
    """Infer a per-commodity balancing tolerance from posting precision.

    Args:
        postings: The postings to inspect (elided postings are ignored).
        multiplier: Fraction of the last significant place to allow; defaults to
            one half.

    Returns:
        A mapping of commodity to tolerance. Commodities whose amounts are all
        integers map to a tolerance of exactly zero.

    """
    max_digits: dict[Commodity, int] = {}
    for posting in postings:
        if posting.amount is None:
            continue
        commodity = posting.amount.commodity
        digits = posting.amount.fractional_digits
        max_digits[commodity] = max(max_digits.get(commodity, 0), digits)

    tolerances: dict[Commodity, Decimal] = {}
    for commodity, digits in max_digits.items():
        tolerances[commodity] = _ZERO if digits == 0 else multiplier * Decimal(1).scaleb(-digits)
    return tolerances


def residual(postings: Iterable[Posting]) -> dict[Commodity, Decimal]:
    """Sum the explicit posting amounts per commodity.

    Args:
        postings: The postings to sum (elided postings are ignored).

    Returns:
        A mapping of commodity to net quantity across the explicit postings.

    """
    totals: dict[Commodity, Decimal] = {}
    for posting in postings:
        if posting.amount is None:
            continue
        commodity = posting.amount.commodity
        totals[commodity] = totals.get(commodity, _ZERO) + posting.amount.quantity
    return totals


def balance_transaction(
    transaction: Transaction,
    *,
    multiplier: Decimal = _DEFAULT_TOLERANCE_MULTIPLIER,
) -> Transaction:
    """Return a balanced copy of ``transaction``, inferring any elided amount.

    At most one posting may leave its amount elided; that posting absorbs the
    single-commodity residual so the transaction nets to zero. After filling,
    every commodity is verified to balance within the inferred tolerance.

    Args:
        transaction: The transaction to balance.
        multiplier: Tolerance multiplier passed to :func:`infer_tolerances`.

    Returns:
        A new :class:`Transaction` whose postings all carry concrete amounts and
        sum to zero within tolerance.

    Raises:
        AmbiguousBalanceError: If more than one posting has an elided amount.
        UnresolvedElidedAmountError: If an elided amount cannot be inferred
            because the residual is empty or spans multiple commodities.
        BalanceError: If the transaction does not balance within tolerance.

    """
    postings = list(transaction.postings)
    elided = transaction.elided_postings
    if len(elided) > 1:
        raise AmbiguousBalanceError(f"Transaction has {len(elided)} postings without an amount; at most one is allowed")

    tolerances = infer_tolerances(postings, multiplier=multiplier)
    totals = residual(postings)

    if elided:
        index = elided[0]
        outstanding = {commodity: qty for commodity, qty in totals.items() if qty != _ZERO}
        if len(outstanding) != 1:
            raise UnresolvedElidedAmountError(
                "Cannot infer an elided posting amount: the residual "
                f"{'is empty' if not outstanding else 'spans multiple commodities'}"
            )
        commodity, quantity = next(iter(outstanding.items()))
        postings[index] = postings[index].with_amount(Amount(-quantity, commodity))
        totals[commodity] = _ZERO

    imbalances = {
        commodity: quantity
        for commodity, quantity in totals.items()
        if abs(quantity) > tolerances.get(commodity, _ZERO)
    }
    if imbalances:
        details = ", ".join(
            f"{quantity} {commodity}" for commodity, quantity in sorted(imbalances.items(), key=lambda kv: kv[0].code)
        )
        raise BalanceError(f"Transaction does not balance; residual: {details}")

    return transaction.with_postings(tuple(postings))


def is_balanced(transaction: Transaction, *, multiplier: Decimal = _DEFAULT_TOLERANCE_MULTIPLIER) -> bool:
    """Report whether a transaction balances (allowing elided inference).

    Args:
        transaction: The transaction to check.
        multiplier: Tolerance multiplier passed to :func:`infer_tolerances`.

    Returns:
        ``True`` if :func:`balance_transaction` would succeed.

    """
    try:
        balance_transaction(transaction, multiplier=multiplier)
    except (AmbiguousBalanceError, UnresolvedElidedAmountError, BalanceError):
        return False
    return True
