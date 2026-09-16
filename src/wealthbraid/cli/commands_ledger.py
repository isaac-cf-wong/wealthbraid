"""Ledger commands: accounts, entries, corrections, and prices."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import typer

from wealthbraid.book.schema import parse_data
from wealthbraid.book.state import to_transaction
from wealthbraid.cli.common import (
    ApproveOption,
    AtOption,
    JsonOption,
    emit,
    handle_errors,
    open_book,
    operation_json,
    operation_text,
    parse_date,
    resolve_actor,
    table,
)
from wealthbraid.engine.balancing import balance_transaction
from wealthbraid.engine.money import Amount
from wealthbraid.engine.transaction import Posting, Transaction
from wealthbraid.errors import NotFoundError, UsageError
from wealthbraid.store.records import RecordKind

PostingOption = Annotated[
    list[str],
    typer.Option(
        "--posting",
        "-p",
        help='"ACCOUNT AMOUNT [COMMODITY]"; at most one posting may omit the amount to balance the entry.',
    ),
]
ReasoningOption = Annotated[str | None, typer.Option(help="Why this change is proposed (required for agents).")]
ConfidenceOption = Annotated[float, typer.Option(min=0.0, max=1.0, help="Proposer confidence in [0, 1].")]


def _reasoning(actor: str, reasoning: str | None, default: str) -> str:
    if reasoning:
        return reasoning
    if actor.startswith("agent:"):
        raise UsageError("agents must pass --reasoning to explain the proposal")
    return default


def parse_postings(specs: list[str], currency: str) -> list[dict[str, str]]:
    """Parse posting specs and fill at most one elided amount.

    Args:
        specs: ``"ACCOUNT AMOUNT [COMMODITY]"`` strings.
        currency: The commodity used when a spec omits one.

    Returns:
        Posting payloads with explicit amounts.

    Raises:
        UsageError: If a spec is malformed.

    """
    postings = []
    for spec in specs:
        parts = spec.split()
        if not parts or len(parts) > 3:  # noqa: PLR2004
            raise UsageError(f"posting must be 'ACCOUNT [AMOUNT [COMMODITY]]', got {spec!r}")
        amount = None
        if len(parts) >= 2:  # noqa: PLR2004
            try:
                Decimal(parts[1])
            except InvalidOperation as exc:
                raise UsageError(f"invalid amount in posting {spec!r}") from exc
            amount = Amount.of(parts[1], parts[2] if len(parts) == 3 else currency)  # noqa: PLR2004
        postings.append(Posting(parts[0], amount))
    if len(postings) < 2:  # noqa: PLR2004
        raise UsageError("an entry needs at least two postings")
    balanced = balance_transaction(Transaction(date=dt.date.min, postings=tuple(postings)))
    return [
        {"account": p.account, "amount": str(p.amount.quantity), "commodity": p.amount.commodity.code}
        for p in balanced.postings
        if p.amount is not None
    ]


@handle_errors
def accounts_command(at: AtOption = None, as_json: JsonOption = False) -> None:
    """List accounts with their open and close dates."""
    state = open_book().state(at=at)
    data = [
        {
            "account": a.name,
            "opened": a.opened.isoformat(),
            "closed": a.closed.isoformat() if a.closed else None,
            "commodities": list(a.commodities),
            "description": a.description,
            "record": a.open_record,
        }
        for a in sorted(state.accounts.values(), key=lambda a: a.name)
    ]
    emit(
        data,
        as_json,
        lambda rows: table(
            [
                (r["account"], r["opened"], r["closed"] or "", ",".join(r["commodities"]), r["description"] or "")
                for r in rows
            ],
            ["account", "opened", "closed", "commodities", "description"],
        ),
    )


@handle_errors
def open_command(
    accounts: Annotated[list[str], typer.Argument(help="Account names, e.g. Assets:Bank:Checking.")],
    date: Annotated[str, typer.Option(help="Opening date (YYYY-MM-DD).")],
    commodity: Annotated[list[str] | None, typer.Option(help="Restrict to these commodities.")] = None,
    description: Annotated[str | None, typer.Option(help="Account description.")] = None,
    reasoning: ReasoningOption = None,
    confidence: ConfidenceOption = 1.0,
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Propose opening accounts."""
    book = open_book()
    actor = resolve_actor(book)
    opened = parse_date(date, "--date")
    changes = []
    for name in accounts:
        data: dict[str, Any] = {"account": name, "date": opened.isoformat()}
        if commodity:
            data["commodities"] = commodity
        if description:
            data["description"] = description
        changes.append({"kind": "account.open", "data": data})
    operation = book.propose(
        actor=actor,
        tool="account.open",
        summary=f"Open {', '.join(accounts)}",
        changes=changes,
        reasoning=_reasoning(actor, reasoning, "Opened by hand."),
        confidence=confidence,
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def close_command(
    account: Annotated[str, typer.Argument(help="Account name.")],
    date: Annotated[str, typer.Option(help="Closing date (YYYY-MM-DD).")],
    reasoning: ReasoningOption = None,
    confidence: ConfidenceOption = 1.0,
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Propose closing an account."""
    book = open_book()
    actor = resolve_actor(book)
    operation = book.propose(
        actor=actor,
        tool="account.close",
        summary=f"Close {account}",
        changes=[
            {"kind": "account.close", "data": {"account": account, "date": parse_date(date, "--date").isoformat()}}
        ],
        reasoning=_reasoning(actor, reasoning, "Closed by hand."),
        confidence=confidence,
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def entries_command(
    account: Annotated[str | None, typer.Option(help="Only entries touching this account subtree.")] = None,
    start: Annotated[str | None, typer.Option("--from", help="First date (YYYY-MM-DD).")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last date (YYYY-MM-DD).")] = None,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """List current entry versions (corrected entries show their latest version)."""
    state = open_book().state(at=at)
    first = parse_date(start, "--from", default=dt.date.min)
    last = parse_date(end, "--to", default=dt.date.max)
    data = []
    for version in state.sorted_entries():
        if not first <= version.data.date <= last:
            continue
        postings = version.data.postings
        if account and not any(p.account == account or p.account.startswith(account + ":") for p in postings):
            continue
        data.append(
            {
                "id": version.id,
                "origin": version.origin,
                "corrected": len(version.history) > 1,
                **version.data.model_dump(mode="json", exclude_defaults=True),
            }
        )

    def text(rows: list[dict[str, Any]]) -> str:
        out = []
        for row in rows:
            flag = " (corrected)" if row["corrected"] else ""
            out.append(f"{row['date']}  {row.get('payee') or ''}  {row.get('narration') or ''}  [{row['id']}]{flag}")
            out.extend(f"    {p['account']:<40} {p['amount']:>14} {p['commodity']}" for p in row["postings"])
        return "\n".join(out) or "no entries"

    emit(data, as_json, text)


def _entry_payload(
    *,
    date: str,
    postings: list[str],
    payee: str | None,
    narration: str | None,
    tags: list[str] | None,
    lines: list[str] | None,
    evidence: list[str] | None,
    currency: str,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "date": parse_date(date, "--date").isoformat(),
        "postings": parse_postings(postings, currency),
    }
    for key, value in (
        ("payee", payee),
        ("narration", narration),
        ("tags", tags),
        ("lines", lines),
        ("evidence", evidence),
    ):
        if value:
            data[key] = value
    parse_data(RecordKind.ENTRY, data)
    return data


@handle_errors
def add_command(
    date: Annotated[str, typer.Option(help="Entry date (YYYY-MM-DD).")],
    posting: PostingOption,
    payee: Annotated[str | None, typer.Option(help="Counterparty.")] = None,
    narration: Annotated[str | None, typer.Option(help="Description.")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="Tag (repeatable).")] = None,
    line: Annotated[
        list[str] | None, typer.Option(help="Statement line id this entry accounts for (repeatable).")
    ] = None,
    evidence: Annotated[list[str] | None, typer.Option(help="Evidence id (repeatable).")] = None,
    reasoning: ReasoningOption = None,
    confidence: ConfidenceOption = 1.0,
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Propose a new entry."""
    book = open_book()
    actor = resolve_actor(book)
    data = _entry_payload(
        date=date,
        postings=posting,
        payee=payee,
        narration=narration,
        tags=tag,
        lines=line,
        evidence=evidence,
        currency=book.config.currency,
    )
    operation = book.propose(
        actor=actor,
        tool="entry.add",
        summary=f"Add entry {data['date']} {payee or narration or ''}".rstrip(),
        changes=[{"kind": "entry", "data": data}],
        reasoning=_reasoning(actor, reasoning, "Entered by hand."),
        confidence=confidence,
        evidence=evidence or [],
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def correct_command(
    entry_id: Annotated[str, typer.Argument(help="Current version id of the entry to correct.")],
    reason: Annotated[str, typer.Option(help="Why the correction is needed.")],
    date: Annotated[str | None, typer.Option(help="Corrected date (default: unchanged).")] = None,
    posting: Annotated[
        list[str] | None, typer.Option("--posting", "-p", help="Corrected postings (default: unchanged).")
    ] = None,
    payee: Annotated[str | None, typer.Option(help="Corrected payee.")] = None,
    narration: Annotated[str | None, typer.Option(help="Corrected description.")] = None,
    reasoning: ReasoningOption = None,
    confidence: ConfidenceOption = 1.0,
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Propose a correction that supersedes an entry (the original stays in the log)."""
    book = open_book()
    actor = resolve_actor(book)
    state = book.state()
    current = state.entries.get(entry_id)
    if current is None:
        latest = state.current_version(entry_id)
        hint = f"; its current version is {latest}" if latest else ""
        raise NotFoundError(f"{entry_id} is not a current entry version{hint}")
    replacement = current.data.model_dump(mode="json", exclude_defaults=True)
    if date:
        replacement["date"] = parse_date(date, "--date").isoformat()
    if posting:
        replacement["postings"] = parse_postings(posting, book.config.currency)
    if payee is not None:
        replacement["payee"] = payee
    if narration is not None:
        replacement["narration"] = narration
    balance_transaction(to_transaction(entry_id, parse_data(RecordKind.ENTRY, replacement)))
    operation = book.propose(
        actor=actor,
        tool="entry.correct",
        summary=f"Correct {entry_id}: {reason}",
        changes=[{"kind": "correction", "data": {"target": entry_id, "replacement": replacement, "reason": reason}}],
        reasoning=_reasoning(actor, reasoning, reason),
        confidence=confidence,
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def void_command(
    entry_id: Annotated[str, typer.Argument(help="Current version id of the entry to void.")],
    reason: Annotated[str, typer.Option(help="Why the entry should be voided.")],
    reasoning: ReasoningOption = None,
    confidence: ConfidenceOption = 1.0,
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Propose voiding an entry (recorded as a correction without replacement)."""
    book = open_book()
    actor = resolve_actor(book)
    operation = book.propose(
        actor=actor,
        tool="entry.void",
        summary=f"Void {entry_id}: {reason}",
        changes=[{"kind": "correction", "data": {"target": entry_id, "reason": reason}}],
        reasoning=_reasoning(actor, reasoning, reason),
        confidence=confidence,
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


@handle_errors
def price_command(
    base: Annotated[str, typer.Argument(help="Commodity being priced, e.g. USD or VWCE.")],
    rate: Annotated[str, typer.Argument(help="Value of one unit of BASE in QUOTE.")],
    quote: Annotated[str, typer.Argument(help="Quote commodity, e.g. EUR.")],
    date: Annotated[str, typer.Option(help="Price date (YYYY-MM-DD).")],
    source: Annotated[str | None, typer.Option(help="Where the price came from.")] = None,
    reasoning: ReasoningOption = None,
    confidence: ConfidenceOption = 1.0,
    approve: ApproveOption = False,
    as_json: JsonOption = False,
) -> None:
    """Propose a price (exchange rate) record."""
    book = open_book()
    actor = resolve_actor(book)
    data: dict[str, Any] = {"date": parse_date(date, "--date").isoformat(), "base": base, "quote": quote, "rate": rate}
    if source:
        data["source"] = source
    operation = book.propose(
        actor=actor,
        tool="price.add",
        summary=f"Price {base} = {rate} {quote} on {data['date']}",
        changes=[{"kind": "price", "data": data}],
        reasoning=_reasoning(actor, reasoning, "Price entered by hand."),
        confidence=confidence,
        approve=approve,
    )
    emit(operation_json(operation), as_json, operation_text)


def register(app: typer.Typer) -> None:
    """Register ledger commands.

    Args:
        app: The root Typer app.

    """
    app.command("accounts")(accounts_command)
    app.command("open")(open_command)
    app.command("close")(close_command)
    app.command("entries")(entries_command)
    app.command("add")(add_command)
    app.command("correct")(correct_command)
    app.command("void")(void_command)
    app.command("price")(price_command)
