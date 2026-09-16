"""Statement commands: evidence, CSV import, statement lines, categorization, reconciliation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import typer

from wealthbraid.book.config import ImportProfile
from wealthbraid.cli.common import (
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
from wealthbraid.errors import NotFoundError, UsageError
from wealthbraid.services.categorize import categorize, parse_assignments
from wealthbraid.services.importing import import_csv
from wealthbraid.services.reconcile import reconcile, reconciliation_status

evidence_app = typer.Typer(help="Source documents stored immutably by digest.", no_args_is_help=True)
import_app = typer.Typer(help="Import statements as evidence plus statement lines.", no_args_is_help=True)


@handle_errors
def evidence_add_command(
    file: Annotated[Path, typer.Argument(help="The document to store.")],
    source: Annotated[str | None, typer.Option(help="Where it came from, e.g. the bank.")] = None,
    description: Annotated[str | None, typer.Option(help="What the document is.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Store a document as evidence (idempotent: the same bytes give the same record)."""
    if not file.is_file():
        raise NotFoundError(f"file not found: {file}")
    book = open_book()
    evidence_id, operation = book.add_evidence(
        file.read_bytes(), filename=file.name, actor=resolve_actor(book), source=source, description=description
    )
    data = {"evidence": evidence_id, "created": operation is not None, "operation": operation.id if operation else None}
    emit(data, as_json, lambda d: f"{d['evidence']} ({'stored' if d['created'] else 'already stored'})")


@handle_errors
def evidence_list_command(at: AtOption = None, as_json: JsonOption = False) -> None:
    """List evidence records."""
    state = open_book().state(at=at)
    data = [{"id": eid, **ev.model_dump(mode="json")} for eid, ev in state.evidence.items()]
    emit(
        data,
        as_json,
        lambda rows: table(
            [(r["id"], r["filename"], r["size"], r["source"] or "", r["sha256"][:16]) for r in rows],
            ["id", "filename", "bytes", "source", "sha256"],
            right=[2],
        ),
    )


@handle_errors
def import_csv_command(
    file: Annotated[Path, typer.Argument(help="CSV statement.")],
    account: Annotated[str | None, typer.Option(help="Statement account (overrides the profile).")] = None,
    profile: Annotated[str | None, typer.Option(help="Import profile from wealthbraid.toml.")] = None,
    commodity: Annotated[str | None, typer.Option(help="Statement currency (default: profile, then book).")] = None,
    source: Annotated[str | None, typer.Option(help="Where the statement came from.")] = None,
    date_column: Annotated[str | None, typer.Option(help="Date column header.")] = None,
    amount_column: Annotated[str | None, typer.Option(help="Signed amount column header.")] = None,
    description_column: Annotated[str | None, typer.Option(help="Description column header.")] = None,
    payee_column: Annotated[str | None, typer.Option(help="Payee column header.")] = None,
    reference_column: Annotated[str | None, typer.Option(help="Bank reference column header.")] = None,
    date_format: Annotated[str | None, typer.Option(help="strptime date format, e.g. %d/%m/%Y.")] = None,
    delimiter: Annotated[str | None, typer.Option(help="Field delimiter.")] = None,
    decimal_comma: Annotated[bool | None, typer.Option(help="Amounts use a decimal comma.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Import a CSV statement: stores the file and records one statement line per new row."""
    book = open_book()
    if profile is not None:
        if profile not in book.config.profiles:
            raise UsageError(
                f"unknown import profile {profile!r}; defined: {', '.join(book.config.profiles) or 'none'}"
            )
        mapping = book.config.profiles[profile]
    else:
        mapping = ImportProfile(name="command-line")
    overrides = {
        "date": date_column,
        "amount": amount_column,
        "description": description_column,
        "payee": payee_column,
        "external_id": reference_column,
        "date_format": date_format,
        "delimiter": delimiter,
        "decimal_comma": decimal_comma,
    }
    mapping = replace(mapping, **{key: value for key, value in overrides.items() if value is not None})
    result = import_csv(
        book, file, actor=resolve_actor(book), profile=mapping, account=account, commodity=commodity, source=source
    )
    data = {
        "evidence": result.evidence,
        "rows": result.rows,
        "new_lines": result.new_lines,
        "duplicate_rows": result.duplicates,
        "operation": operation_json(result.operation) if result.operation else None,
    }
    emit(
        data,
        as_json,
        lambda d: (
            f"Imported {d['new_lines']} new statement lines from {d['rows']} rows "
            f"({len(d['duplicate_rows'])} duplicates skipped); evidence {d['evidence']}."
            + ("\nNext: `wealthbraid categorize` to propose entries." if d["new_lines"] else "")
        ),
    )


@handle_errors
def lines_command(
    unmatched: Annotated[bool, typer.Option(help="Only lines no entry accounts for.")] = False,
    account: Annotated[str | None, typer.Option(help="Only lines of this statement account.")] = None,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """List statement lines and the entries matching them."""
    state = open_book().state(at=at)
    ids = state.unmatched_lines() if unmatched else sorted(state.lines, key=lambda i: (state.lines[i].date, i))
    data = [
        {"id": line_id, "matched_by": state.line_matches.get(line_id), **state.lines[line_id].model_dump(mode="json")}
        for line_id in ids
        if account is None or state.lines[line_id].account == account
    ]
    emit(
        data,
        as_json,
        lambda rows: table(
            [
                (
                    r["id"],
                    r["date"],
                    r["account"],
                    r["amount"],
                    r["commodity"],
                    r["description"],
                    r["matched_by"] or "-",
                )
                for r in rows
            ],
            ["id", "date", "account", "amount", "", "description", "matched by"],
            right=[3],
        ),
    )


@handle_errors
def categorize_command(
    assignments: Annotated[
        Path | None,
        typer.Option(
            help='JSON file ("-" for stdin): [{"line": "lin_…", "account": "Expenses:…", "confidence": 0.8, '
            '"rationale": "…"}]. Without it, the book\'s rules are used.'
        ),
    ] = None,
    reasoning: Annotated[str | None, typer.Option(help="Reasoning summary (required with --assignments).")] = None,
    limit: Annotated[int | None, typer.Option(help="Categorize at most this many lines.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Propose entries for unmatched statement lines (always waits for approval)."""
    book = open_book()
    parsed = None
    if assignments is not None:
        raw_text = typer.get_text_stream("stdin").read() if str(assignments) == "-" else assignments.read_text("utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise UsageError(f"assignments are not valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise UsageError("assignments must be a JSON array")
        parsed = parse_assignments(raw)
    operation, remaining = categorize(
        book, actor=resolve_actor(book), assignments=parsed, reasoning=reasoning, limit=limit
    )
    data = {"operation": operation_json(operation) if operation else None, "uncategorized_lines": remaining}

    def text(d: dict[str, Any]) -> str:
        head = operation_text(d["operation"]) if d["operation"] else "No lines matched; nothing proposed."
        return f"{head}\n{len(d['uncategorized_lines'])} unmatched statement lines have no proposal yet."

    emit(data, as_json, text)


@handle_errors
def reconcile_command(
    account: Annotated[str, typer.Argument(help="Statement account.")],
    date: Annotated[str, typer.Option(help="Statement date (YYYY-MM-DD).")],
    balance: Annotated[str, typer.Option(help="Closing balance printed on the statement.")],
    commodity: Annotated[str | None, typer.Option(help="Statement currency (default: book currency).")] = None,
    evidence: Annotated[list[str] | None, typer.Option(help="Evidence id of the statement (repeatable).")] = None,
    note: Annotated[str | None, typer.Option(help="Note for the reviewer.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Compare a statement balance with the ledger and propose a reconciliation checkpoint."""
    book = open_book()
    comparison, operation = reconcile(
        book,
        actor=resolve_actor(book),
        account=account,
        date=parse_date(date, "--date"),
        statement_balance=balance,
        commodity=commodity,
        evidence=evidence or [],
        note=note,
    )
    data = {"comparison": comparison, "operation": operation_json(operation)}

    def text(d: dict[str, Any]) -> str:
        c = d["comparison"]
        lines = [
            (
                f"{c['account']} on {c['date']}: statement {c['statement_balance']} vs ledger {c['ledger_balance']} "
                f"{c['commodity']} → difference {c['difference']}"
            ),
        ]
        if c["unmatched_lines"]:
            lines.append(
                f"{len(c['unmatched_lines'])} unmatched statement lines total {c['unmatched_total']}"
                + (" (exactly the difference)" if c["unmatched_explains_difference"] else "")
            )
        lines.append(operation_text(d["operation"]))
        return "\n".join(lines)

    emit(data, as_json, text)


@handle_errors
def reconciliations_command(at: AtOption = None, as_json: JsonOption = False) -> None:
    """Re-check every recorded reconciliation against the current ledger."""
    rows = reconciliation_status(open_book().state(at=at))
    emit(
        rows,
        as_json,
        lambda data: table(
            [
                (r["id"], r["account"], r["date"], r["statement_balance"], r["current_ledger_balance"], r["status"])
                for r in data
            ],
            ["id", "account", "date", "statement", "ledger now", "status"],
            right=[3, 4],
        ),
    )


def register(app: typer.Typer) -> None:
    """Register statement commands.

    Args:
        app: The root Typer app.

    """
    evidence_app.command("add")(evidence_add_command)
    evidence_app.command("list")(evidence_list_command)
    import_app.command("csv")(import_csv_command)
    app.add_typer(evidence_app, name="evidence")
    app.add_typer(import_app, name="import")
    app.command("lines")(lines_command)
    app.command("categorize")(categorize_command)
    app.command("reconcile")(reconcile_command)
    app.command("reconciliations")(reconciliations_command)
