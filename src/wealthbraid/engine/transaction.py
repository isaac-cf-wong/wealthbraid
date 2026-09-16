"""Transactions and postings.

A :class:`Transaction` is a dated, balanced set of :class:`Posting` legs plus
metadata (payee, description, tags, links, attachments). Postings may leave the
amount elided (``None``); the balancing rules fill in at most one such amount.
Both types are immutable so that transformations produce new values and history
stays auditable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from wealthbraid.engine.errors import InvalidTransactionError
from wealthbraid.engine.money import Amount


@dataclass(frozen=True)
class Posting:
    """A single leg of a transaction: an account and an optional amount.

    Attributes:
        account: The account name this leg posts to.
        amount: The signed amount, or ``None`` if it is to be inferred by
            balancing.
        metadata: Arbitrary key/value metadata for the leg.

    """

    account: str
    amount: Amount | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def with_amount(self, amount: Amount) -> Posting:
        """Return a copy of this posting with a concrete amount.

        Args:
            amount: The amount to set.

        Returns:
            A new :class:`Posting` carrying ``amount``.

        """
        return replace(self, amount=amount)


@dataclass(frozen=True)
class Transaction:
    """A dated, balanced double-entry transaction.

    Attributes:
        date: The transaction date.
        postings: The transaction's legs; at least one is required.
        payee: The counterparty, if any.
        description: A human-readable description of the transaction.
        tags: Free-form tags.
        links: Identifiers linking related transactions.
        attachments: Paths to supporting documents.
        metadata: Arbitrary key/value metadata (memo, notes, external ids).
        id: An optional stable identifier assigned by higher layers.

    """

    date: dt.date
    postings: tuple[Posting, ...]
    payee: str | None = None
    description: str | None = None
    tags: frozenset[str] = frozenset()
    links: frozenset[str] = frozenset()
    attachments: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    id: str | None = None

    def __post_init__(self) -> None:
        """Validate structural invariants and normalise the postings container.

        Raises:
            InvalidTransactionError: If there are no postings.

        """
        object.__setattr__(self, "postings", tuple(self.postings))
        if not self.postings:
            raise InvalidTransactionError("A transaction must have at least one posting")

    @property
    def elided_postings(self) -> tuple[int, ...]:
        """Return the indices of postings whose amount is not yet set.

        Returns:
            A tuple of indices into :attr:`postings` with ``amount is None``.

        """
        return tuple(i for i, posting in enumerate(self.postings) if posting.amount is None)

    def with_postings(self, postings: tuple[Posting, ...]) -> Transaction:
        """Return a copy of this transaction with replaced postings.

        Args:
            postings: The new postings.

        Returns:
            A new :class:`Transaction` carrying ``postings``.

        """
        return replace(self, postings=tuple(postings))
