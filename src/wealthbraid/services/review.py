"""The human review queue: everything that needs a person's attention.

The queue gathers, in one place:

* pending operations, lowest confidence first;
* statement lines no entry accounts for yet, with any pending proposals covering them;
* reconciliations that never balanced or that later changes invalidated;
* integrity issues raised while projecting the record log.
"""

from __future__ import annotations

from typing import Any

from wealthbraid.book.state import BookState
from wealthbraid.services.reconcile import reconciliation_status
from wealthbraid.services.reports import basis

LOW_CONFIDENCE = 0.6


def review_queue(state: BookState) -> dict[str, Any]:
    """Build the review queue.

    Args:
        state: The book state.

    Returns:
        Pending operations, unmatched lines, reconciliation exceptions, issues, and counts.

    """
    pending = sorted(
        (op for op in state.operations.values() if op.status == "pending"),
        key=lambda op: (op.data.confidence, op.record.seq),
    )
    operations = [
        {
            "id": op.id,
            "tool": op.data.tool,
            "summary": op.data.summary,
            "actor": op.record.actor,
            "proposed_at": op.record.recorded_at,
            "confidence": op.data.confidence,
            "low_confidence": op.data.confidence < LOW_CONFIDENCE,
            "changes": len(op.data.changes),
            "stale": bool(state.changed_since(op)),
        }
        for op in pending
    ]
    proposals = state.pending_line_proposals()
    lines = [
        {
            "id": line_id,
            "account": state.lines[line_id].account,
            "date": state.lines[line_id].date.isoformat(),
            "amount": state.lines[line_id].amount,
            "commodity": state.lines[line_id].commodity,
            "description": state.lines[line_id].description,
            "payee": state.lines[line_id].payee,
            "pending_operations": proposals.get(line_id, []),
        }
        for line_id in state.unmatched_lines()
    ]
    reconciliations = [row for row in reconciliation_status(state) if row["status"] != "balanced"]
    issues = [{"record": issue.record, "message": issue.message} for issue in state.issues]
    return {
        "basis": basis(state),
        "counts": {
            "pending_operations": len(operations),
            "unmatched_lines": len(lines),
            "reconciliation_exceptions": len(reconciliations),
            "issues": len(issues),
        },
        "pending_operations": operations,
        "unmatched_lines": lines,
        "reconciliation_exceptions": reconciliations,
        "issues": issues,
    }
