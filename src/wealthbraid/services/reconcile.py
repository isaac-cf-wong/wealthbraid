"""Reconciliation: checking a statement balance against the ledger.

``reconcile`` computes the ledger balance of an account (and its sub-accounts)
as of the statement date, compares it with the balance the statement reports,
lists the statement lines up to that date that no entry accounts for yet, and
proposes a ``reconciliation`` record for human approval.

An approved reconciliation is a checkpoint. ``reconciliation_status``
re-evaluates every checkpoint against the book as it is now, so a later
correction that changes a reconciled period shows up as an exception instead of
silently invalidating the reconciliation.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from wealthbraid.book.book import Book
from wealthbraid.book.state import BookState, OperationState
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import ValidationError


def _in_subtree(name: str, account: str) -> bool:
    return name == account or name.startswith(account + ":")


def ledger_balance(state: BookState, account: str, date: dt.date, commodity: str) -> Decimal:
    """Return the balance of an account subtree in one commodity as of a date.

    Args:
        state: The book state.
        account: The account.
        date: The as-of date, inclusive.
        commodity: The commodity.

    Returns:
        The balance.

    """
    return state.ledger(end=date).balance(account).get(Commodity(commodity))


def unmatched_lines_for(state: BookState, account: str, date: dt.date) -> list[str]:
    """Return unmatched statement lines of an account subtree up to a date.

    Args:
        state: The book state.
        account: The account.
        date: The as-of date, inclusive.

    Returns:
        Line ids in date order.

    """
    return [
        line_id
        for line_id in state.unmatched_lines()
        if _in_subtree(state.lines[line_id].account, account) and state.lines[line_id].date <= date
    ]


def reconcile(  # noqa: PLR0913 - keyword-only options
    book: Book,
    *,
    actor: str,
    account: str,
    date: dt.date,
    statement_balance: str,
    commodity: str | None = None,
    evidence: Sequence[str] = (),
    note: str | None = None,
) -> tuple[dict[str, Any], OperationState]:
    """Compare a statement balance with the ledger and propose a reconciliation.

    Args:
        book: The book.
        actor: The proposer.
        account: The statement account.
        date: The statement date.
        statement_balance: The balance printed on the statement.
        commodity: The statement commodity (defaults to the book currency).
        evidence: Evidence ids for the statement.
        note: A free-text note.

    Returns:
        The comparison and the proposed operation.

    Raises:
        ValidationError: If the account is not open or the balance is not a decimal.

    """
    state = book.state()
    if not any(_in_subtree(name, account) for name in state.accounts):
        raise ValidationError(f"no account {account} or sub-account is open")
    commodity = commodity or book.config.currency
    try:
        statement = Decimal(statement_balance)
    except ArithmeticError as exc:
        raise ValidationError(f"statement balance is not a decimal: {statement_balance!r}") from exc
    ledger = ledger_balance(state, account, date, commodity)
    difference = statement - ledger
    unmatched = unmatched_lines_for(state, account, date)
    unmatched_total = sum((Decimal(state.lines[line_id].amount) for line_id in unmatched), Decimal(0))
    comparison = {
        "account": account,
        "date": date.isoformat(),
        "commodity": commodity,
        "statement_balance": str(statement),
        "ledger_balance": str(ledger),
        "difference": str(difference),
        "balanced": difference == 0,
        "unmatched_lines": unmatched,
        "unmatched_total": str(unmatched_total),
        "unmatched_explains_difference": difference != 0 and unmatched_total == difference,
    }
    data: dict[str, Any] = {
        "account": account,
        "date": date.isoformat(),
        "commodity": commodity,
        "statement_balance": str(statement),
        "ledger_balance": str(ledger),
        "difference": str(difference),
        "unmatched_lines": unmatched,
        "evidence": list(evidence),
    }
    if note:
        data["note"] = note
    if difference == 0:
        reasoning = f"Statement and ledger agree at {statement} {commodity} on {date}."
    else:
        reasoning = f"Statement shows {statement} {commodity} but the ledger has {ledger}; difference {difference}."
        if comparison["unmatched_explains_difference"]:
            reasoning += f" The {len(unmatched)} unmatched statement lines account for exactly this difference."
    operation = book.propose(
        actor=actor,
        tool="reconcile",
        summary=f"Reconcile {account} at {date}: {'balanced' if difference == 0 else f'difference {difference} {commodity}'}",
        changes=[{"kind": "reconciliation", "data": data}],
        reasoning=reasoning,
        confidence=1.0,
        evidence=list(evidence),
        inputs={
            "account": account,
            "date": date.isoformat(),
            "statement_balance": str(statement),
            "commodity": commodity,
        },
    )
    return comparison, operation


RESOLVED_STATUSES = frozenset({"balanced", "superseded"})


def reconciliation_status(state: BookState) -> list[dict[str, Any]]:
    """Re-check every applied reconciliation against the current book.

    Args:
        state: The book state.

    Returns:
        One row per reconciliation with ``status``:

        * ``superseded``: a later reconciliation covers the same account, date, and commodity;
        * ``new_lines``: statement lines dated inside the period arrived after it was recorded
          and are not yet accounted for;
        * ``balanced``: the ledger now agrees with the statement;
        * ``changed``: it agreed when recorded, but later changes moved the ledger balance;
        * ``discrepancy``: it never agreed and still does not.

        :data:`RESOLVED_STATUSES` need no attention.

    """
    latest: dict[tuple[str, dt.date, str], str] = {}
    for record_id, data in state.reconciliations:
        latest[(data.account, data.date, data.commodity)] = record_id
    rows = []
    for record_id, data in state.reconciliations:
        now = ledger_balance(state, data.account, data.date, data.commodity)
        statement = Decimal(data.statement_balance)
        recorded = Decimal(data.ledger_balance)
        new_lines = [
            line_id
            for line_id in unmatched_lines_for(state, data.account, data.date)
            if line_id not in data.unmatched_lines
        ]
        if latest[(data.account, data.date, data.commodity)] != record_id:
            status = "superseded"
        elif new_lines:
            status = "new_lines"
        elif now == statement:
            status = "balanced"
        elif recorded == statement:
            status = "changed"
        else:
            status = "discrepancy"
        rows.append(
            {
                "id": record_id,
                "account": data.account,
                "date": data.date.isoformat(),
                "commodity": data.commodity,
                "statement_balance": data.statement_balance,
                "recorded_ledger_balance": data.ledger_balance,
                "current_ledger_balance": str(now),
                "current_difference": str(statement - now),
                "new_unmatched_lines": new_lines,
                "status": status,
            }
        )
    return rows
