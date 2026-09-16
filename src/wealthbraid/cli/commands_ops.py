"""Operation commands: generic proposals, the review queue, decisions, and notes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from wealthbraid.cli.common import (
    ApproveOption,
    AtOption,
    JsonOption,
    emit,
    handle_errors,
    open_book,
    operation_json,
    operation_text,
    read_input,
    read_state,
    resolve_actor,
    table,
)
from wealthbraid.errors import NotFoundError, UsageError
from wealthbraid.services.review import review_queue

ops_app = typer.Typer(help="Inspect, approve, and reject operations.", no_args_is_help=True)
note_app = typer.Typer(help="Explanations and annotations attached to records.", no_args_is_help=True)

_PROPOSAL_KEYS = {"tool", "summary", "reasoning", "confidence", "changes", "evidence", "inputs"}


@handle_errors
def propose_command(
    file: Annotated[
        Path, typer.Argument(help='Proposal JSON file, or "-" for stdin. See `wealthbraid schema operation`.')
    ],
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Submit a proposal: {tool, summary, reasoning, confidence, changes, evidence?, inputs?}."""
    text = read_input(file)
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"proposal is not valid JSON: {exc}") from exc
    if not isinstance(proposal, dict):
        raise UsageError("proposal must be a JSON object")
    unknown = set(proposal) - _PROPOSAL_KEYS
    if unknown:
        raise UsageError(f"unknown proposal keys: {', '.join(sorted(unknown))}")
    missing = {"tool", "summary", "reasoning", "confidence", "changes"} - set(proposal)
    if missing:
        raise UsageError(f"proposal is missing: {', '.join(sorted(missing))}")
    book = open_book()
    operation = book.propose(
        actor=resolve_actor(book),
        tool=proposal["tool"],
        summary=proposal["summary"],
        changes=proposal["changes"],
        reasoning=proposal["reasoning"],
        confidence=proposal["confidence"],
        evidence=proposal.get("evidence", []),
        inputs=proposal.get("inputs"),
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def ops_list_command(
    status: Annotated[str | None, typer.Option(help="pending, applied, rejected, or approved.")] = "pending",
    all_statuses: Annotated[bool, typer.Option("--all", help="Show every status.")] = False,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """List operations (pending ones by default)."""
    state = read_state(at)
    operations = [op for op in state.operations.values() if all_statuses or status is None or op.status == status]
    data = [operation_json(op) for op in operations]
    emit(
        data,
        as_json,
        lambda rows: (
            table(
                [(r["id"], r["status"], r["tool"], f"{r['confidence']:.2f}", r["actor"], r["summary"]) for r in rows],
                ["id", "status", "tool", "conf", "actor", "summary"],
            )
            if rows
            else "no operations"
        ),
    )


def _operation(operation_id: str):
    book = open_book()
    operation = book.state().operations.get(operation_id)
    if operation is None:
        raise NotFoundError(f"operation not found: {operation_id}")
    return book, operation


@handle_errors
def ops_show_command(
    operation_id: Annotated[str, typer.Argument(help="Operation id.")], as_json: JsonOption = False
) -> None:
    """Show an operation with every proposed change."""
    _, operation = _operation(operation_id)
    data = operation_json(operation)

    def text(d: dict[str, Any]) -> str:
        lines = [operation_text(d)]
        for index, change in enumerate(d["changes"]):
            rationale = f"  — {change['rationale']}" if change.get("rationale") else ""
            lines.append(f"  [{index}] {change['kind']}{rationale}")
            lines.append("      " + json.dumps(change["data"], ensure_ascii=False))
        return "\n".join(lines)

    emit(data, as_json, text)


@handle_errors
def ops_approve_command(
    operation_id: Annotated[str, typer.Argument(help="Operation id.")],
    note: Annotated[str | None, typer.Option(help="Why you approve.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Approve a pending operation and apply its changes (humans only)."""
    book, _ = _operation(operation_id)
    operation = book.decide(operation_id, actor=resolve_actor(book), verdict="approve", note=note)
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def ops_reject_command(
    operation_id: Annotated[str, typer.Argument(help="Operation id.")],
    note: Annotated[str | None, typer.Option(help="Why you reject.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Reject a pending operation (humans only); nothing is applied."""
    book, _ = _operation(operation_id)
    operation = book.decide(operation_id, actor=resolve_actor(book), verdict="reject", note=note)
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def review_command(at: AtOption = None, as_json: JsonOption = False) -> None:
    """Show everything waiting for a human: proposals, unmatched lines, reconciliation exceptions, issues."""
    queue = review_queue(read_state(at))

    def text(q: dict[str, Any]) -> str:
        out = []
        counts = q["counts"]
        out.append(f"Pending operations ({counts['pending_operations']}):")
        out.extend(
            f"  {op['id']}  conf {op['confidence']:.2f}{' LOW' if op['low_confidence'] else ''}  {op['actor']}  {op['summary']}"
            for op in q["pending_operations"]
        )
        out.append(f"Unmatched statement lines ({counts['unmatched_lines']}):")
        out.extend(
            f"  {line['id']}  {line['date']}  {line['amount']} {line['commodity']}  {line['description']}"
            + (f"  (proposed in {', '.join(line['pending_operations'])})" if line["pending_operations"] else "")
            for line in q["unmatched_lines"][:20]
        )
        if counts["unmatched_lines"] > 20:  # noqa: PLR2004
            out.append(f"  … {counts['unmatched_lines'] - 20} more (`wealthbraid lines --unmatched`)")
        out.append(f"Reconciliation exceptions ({counts['reconciliation_exceptions']}):")
        out.extend(
            f"  {r['id']}  {r['account']} {r['date']}  {r['status']}  difference now {r['current_difference']}"
            for r in q["reconciliation_exceptions"]
        )
        out.append(f"Integrity issues ({counts['issues']}):")
        out.extend(f"  {i['record']}: {i['message']}" for i in q["issues"])
        return "\n".join(out)

    emit(queue, as_json, text)


@handle_errors
def note_add_command(
    subjects: Annotated[list[str], typer.Argument(help="Record ids the note is about.")],
    text: Annotated[str, typer.Option(help="The explanation.")],
    confidence: Annotated[float | None, typer.Option(min=0.0, max=1.0, help="Confidence in the explanation.")] = None,
    reasoning: Annotated[str | None, typer.Option(help="How the explanation was reached.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Attach an explanation to records (non-sensitive: applied without review by default)."""
    book = open_book()
    data: dict[str, Any] = {"subjects": subjects, "text": text}
    if confidence is not None:
        data["confidence"] = confidence
    operation = book.propose(
        actor=resolve_actor(book),
        tool="note.add",
        summary=f"Note on {', '.join(subjects)}",
        changes=[{"kind": "note", "data": data}],
        reasoning=reasoning or "Explanation added.",
        confidence=confidence if confidence is not None else 1.0,
    )
    emit(operation_json(operation), as_json, operation_text)


def register(app: typer.Typer) -> None:
    """Register operation commands.

    Args:
        app: The root Typer app.

    """
    ops_app.command("list")(ops_list_command)
    ops_app.command("show")(ops_show_command)
    ops_app.command("approve")(ops_approve_command)
    ops_app.command("reject")(ops_reject_command)
    note_app.command("add")(note_add_command)
    app.add_typer(ops_app, name="ops")
    app.add_typer(note_app, name="note")
    app.command("propose")(propose_command)
    app.command("review")(review_command)
