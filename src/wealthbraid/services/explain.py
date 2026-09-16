"""Explanations: why a balance changed, and where a record came from.

``explain_change`` decomposes the change in an account's balance over a period
into contributions from counter accounts. Within each entry, the postings to
the explained account subtree are balanced by the entry's other postings, so
the contributions sum exactly to the balance change. The result carries that
check explicitly (``reconciles``) rather than assuming it.

``trace`` walks provenance links in both directions: from a record to the
operation, decision, evidence, and statement lines behind it, and to the
corrections, matches, and notes that came after it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Any

from wealthbraid.book.state import BookState, OperationState
from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import NotFoundError, ValidationError
from wealthbraid.services.reports import amounts, basis
from wealthbraid.store.records import RecordKind

_ZERO = Decimal(0)


def _in_subtree(name: str, account: str) -> bool:
    return name == account or name.startswith(account + ":")


def explain_change(state: BookState, *, account: str, start: dt.date, end: dt.date) -> dict[str, Any]:
    """Decompose an account subtree's balance change over a period.

    Args:
        state: The book state.
        account: The account (subtree root) to explain.
        start: First day of the period, inclusive.
        end: Last day of the period, inclusive.

    Returns:
        Opening and closing balances, the change, contributions per counter
        account (largest first), the entries involved, and a ``reconciles`` flag.

    Raises:
        NotFoundError: If no opened account lies in the subtree.
        ValidationError: If ``start`` is after ``end``.

    """
    if start > end:
        raise ValidationError(f"the start date {start} must be on or before the end date {end}")
    if not any(_in_subtree(name, account) for name in state.accounts):
        raise NotFoundError(f"no account {account} or sub-account is open")
    opening = state.ledger(end=start - dt.timedelta(days=1)).balance(account)
    closing = state.ledger(end=end).balance(account)

    contributions: dict[tuple[str, Commodity], Decimal] = defaultdict(lambda: _ZERO)
    counts: dict[tuple[str, Commodity], int] = defaultdict(int)
    entries = []
    for version in state.sorted_entries():
        if not start <= version.data.date <= end:
            continue
        inside = [p for p in version.transaction.postings if _in_subtree(p.account, account)]
        if not inside:
            continue
        change = Inventory.from_amounts(p.amount for p in inside if p.amount is not None)
        for posting in version.transaction.postings:
            if posting.amount is None or _in_subtree(posting.account, account):
                continue
            key = (posting.account, posting.amount.commodity)
            contributions[key] -= posting.amount.quantity
            counts[key] += 1
        entries.append(
            {
                "id": version.id,
                "date": version.data.date.isoformat(),
                "payee": version.data.payee,
                "narration": version.data.narration,
                "change": amounts(change),
            }
        )

    delta = closing.merge(Inventory({a.commodity: -a.quantity for a in opening.amounts()}))
    explained = Inventory()
    for (_, commodity), quantity in contributions.items():
        explained = explained.merge(Inventory({commodity: quantity}))
    rows = sorted(
        (
            {
                "account": name,
                "commodity": commodity.code,
                "amount": str(quantity),
                "postings": counts[(name, commodity)],
            }
            for (name, commodity), quantity in contributions.items()
            if quantity != 0
        ),
        key=lambda row: (-abs(Decimal(row["amount"])), row["account"], row["commodity"]),
    )
    return {
        "explanation": "balance-change",
        "basis": basis(state, account=account, start=start, end=end),
        "account": account,
        "opening": amounts(opening),
        "closing": amounts(closing),
        "change": amounts(delta),
        "contributions": rows,
        "entries": entries,
        "reconciles": explained == delta,
    }


def _operation_summary(operation: OperationState) -> dict[str, Any]:
    return {
        "id": operation.id,
        "tool": operation.data.tool,
        "summary": operation.data.summary,
        "actor": operation.record.actor,
        "proposed_at": operation.record.recorded_at,
        "reasoning": operation.data.reasoning,
        "confidence": operation.data.confidence,
        "inputs": operation.data.inputs,
        "evidence": operation.data.evidence,
        "status": operation.status,
        "decided_by": operation.decision.actor if operation.decision else None,
        "decision_note": operation.decision.data.get("note") if operation.decision else None,
        "results": operation.results,
    }


def trace(state: BookState, record_id: str) -> dict[str, Any]:
    """Collect the provenance of a record.

    Args:
        state: The book state.
        record_id: Any record id.

    Returns:
        The record, the operation behind it, and related records upstream
        (evidence, statement lines, earlier versions) and downstream
        (corrections, matching entries, notes, derived lines).

    Raises:
        NotFoundError: If the record does not exist.

    """
    record = state.by_id.get(record_id)
    if record is None:
        raise NotFoundError(f"record not found: {record_id}")
    result: dict[str, Any] = {
        "record": record.to_json(),
        "operation": None,
        "issues": [issue.message for issue in state.issues_for([record_id])],
        "notes": [state.by_id[note].to_json() for note in state.notes.get(record_id, [])],
    }
    if record.operation and record.operation in state.operations:
        result["operation"] = _operation_summary(state.operations[record.operation])

    kind = record.kind
    if kind in (RecordKind.ENTRY, RecordKind.CORRECTION):
        current = state.current_version(record_id)
        version = state.entries.get(current) if current else None
        history = version.history if version else _history_to(state, record_id)
        result["versions"] = [
            {
                "id": vid,
                "kind": state.by_id[vid].kind.value,
                "recorded_at": state.by_id[vid].recorded_at,
                "reason": state.by_id[vid].data.get("reason"),
                "operation": state.by_id[vid].operation,
            }
            for vid in history
        ]
        result["current_version"] = current
        if record_id in state.voided:
            result["voided_by"] = state.voided[record_id]
        if version is not None:
            line_ids, cited = list(version.data.lines), list(version.data.evidence)
        else:
            payload = record.data.get("replacement") if kind is RecordKind.CORRECTION else record.data
            line_ids, cited = list((payload or {}).get("lines", [])), list((payload or {}).get("evidence", []))
        result["lines"] = [
            {"id": line_id, **state.by_id[line_id].data} for line_id in line_ids if line_id in state.by_id
        ]
        evidence_ids = sorted(set(cited) | {line["evidence"] for line in result["lines"]})
        result["evidence"] = [{"id": eid, **state.by_id[eid].data} for eid in evidence_ids if eid in state.by_id]
    elif kind is RecordKind.LINE:
        match = state.line_matches.get(record_id)
        result["matched_by"] = match
        evidence_id = record.data.get("evidence")
        result["evidence"] = (
            [{"id": evidence_id, **state.by_id[evidence_id].data}] if evidence_id in state.by_id else []
        )
    elif kind is RecordKind.EVIDENCE:
        result["lines"] = [line_id for line_id, line in state.lines.items() if line.evidence == record_id]
        result["cited_by_operations"] = [op.id for op in state.operations.values() if record_id in op.data.evidence]
    elif kind is RecordKind.OPERATION:
        result["operation"] = _operation_summary(state.operations[record_id]) if record_id in state.operations else None
        result["changes"] = record.data.get("changes", [])
    return result


def _history_to(state: BookState, record_id: str) -> list[str]:
    """Reconstruct the version chain ending at (or passing through) a superseded or voided id.

    Args:
        state: The book state.
        record_id: An entry or correction id.

    Returns:
        Version ids from the original entry forward through every known successor.

    """
    backwards = {successor: predecessor for predecessor, successor in state.superseded.items()}
    chain = [record_id]
    while chain[0] in backwards:
        chain.insert(0, backwards[chain[0]])
    while chain[-1] in state.superseded:
        chain.append(state.superseded[chain[-1]])
    if chain[-1] in state.voided:
        chain.append(state.voided[chain[-1]])
    return chain
