"""Book-level commands: init, status, verify, schema, log, show, trace, serve."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from wealthbraid.book.book import Book
from wealthbraid.book.config import init_book
from wealthbraid.book.schema import json_schema
from wealthbraid.book.verify import verify_book
from wealthbraid.cli.common import AtOption, JsonOption, emit, handle_errors, open_book, read_state, table
from wealthbraid.errors import IntegrityError, NotFoundError, PolicyError, UsageError
from wealthbraid.services.explain import trace
from wealthbraid.services.review import review_queue
from wealthbraid.store.records import RecordKind


@handle_errors
def init_command(
    path: Annotated[Path, typer.Argument(help="Directory for the new book.")] = Path(),
    name: Annotated[str, typer.Option(help="Book name.")] = "My wealth",
    currency: Annotated[str, typer.Option(help="Reporting currency.")] = "EUR",
    user: Annotated[str, typer.Option(help="Your name; approvals are recorded as human:<user>.")] = "owner",
    force: Annotated[bool, typer.Option(help="Create the book even if the directory already has files.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Create a new, empty book."""
    root = path.resolve()
    if root.exists() and not root.is_dir():
        raise UsageError(f"{root} exists and is not a directory")
    if root.is_dir() and any(root.iterdir()) and not force and not (root / "wealthbraid.toml").exists():
        raise UsageError(f"{root} is not empty; choose an empty directory or pass --force")
    root = init_book(root, name=name, currency=currency, user=user)
    emit(
        {"book": str(root), "currency": currency, "user": f"human:{user}"},
        as_json,
        lambda d: (
            f"Created book at {d['book']} (currency {d['currency']}, approvals as {d['user']}).\n"
            "Tip: keep the book in git — the record log only ever grows, so diffs stay reviewable."
        ),
    )


@handle_errors
def status_command(at: AtOption = None, as_json: JsonOption = False) -> None:
    """Summarise the book: records, accounts, entries, and what needs review."""
    book = open_book()
    state = read_state(at, book)
    queue = review_queue(state)
    data = {
        "book": str(book.root),
        "name": book.config.name,
        "currency": book.config.currency,
        "head": state.head.id if state.head else None,
        "records": len(state.records),
        "accounts": len(state.accounts),
        "entries": len(state.entries),
        "evidence": len(state.evidence),
        "statement_lines": len(state.lines),
        "integrity_issues": len(state.integrity_issues),
        "needs_review": queue["counts"],
    }

    def text(d: dict[str, Any]) -> str:
        review = d["needs_review"]
        return "\n".join(
            [
                f"{d['name']}  ({d['book']})",
                f"head {d['head']}  ·  {d['records']} records",
                (
                    f"{d['accounts']} accounts · {d['entries']} entries · {d['evidence']} evidence · "
                    f"{d['statement_lines']} statement lines"
                ),
                (
                    f"needs review: {review['pending_operations']} pending operations, {review['unmatched_lines']} "
                    f"unmatched lines, {review['reconciliation_exceptions']} reconciliation exceptions, "
                    f"{review['issues']} issues"
                ),
            ]
        )

    emit(data, as_json, text)


@handle_errors
def verify_command(as_json: JsonOption = False) -> None:
    """Rebuild the book from its files and check ids, the hash chain, evidence, and invariants."""
    book = open_book()
    report = verify_book(book)
    data = {
        "ok": report.ok,
        "records": report.records,
        "head": report.head,
        "problems": [{"check": p.check, "record": p.record, "message": p.message} for p in report.problems],
    }

    def text(d: dict[str, Any]) -> str:
        if d["ok"]:
            return f"OK: {d['records']} records verified (head {d['head']})."
        lines = [f"FAILED: {len(d['problems'])} problem(s) in {d['records']} records"]
        lines += [f"  [{p['check']}] {p['record'] or '-'}: {p['message']}" for p in d["problems"]]
        return "\n".join(lines)

    emit(data, as_json, text)
    if not report.ok:
        raise typer.Exit(IntegrityError.exit_code)


@handle_errors
def schema_command(
    kind: Annotated[str, typer.Argument(help="Record kind, e.g. operation, entry, correction.")] = "operation",
    as_json: JsonOption = False,
) -> None:
    """Print the JSON Schema for a record payload (the proposal format by default)."""
    emit(json_schema(kind), True)


@handle_errors
def log_command(
    kind: Annotated[str | None, typer.Option(help="Only records of this kind.")] = None,
    limit: Annotated[int, typer.Option(min=0, help="Show at most this many (latest first).")] = 20,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """List recent records, newest first."""
    state = read_state(at)
    if kind is not None and kind not in {k.value for k in RecordKind}:
        raise UsageError(f"unknown record kind {kind!r}; expected one of: {', '.join(k.value for k in RecordKind)}")
    records = [r for r in reversed(state.records) if kind is None or r.kind.value == kind][:limit]
    data = [r.to_json() for r in records]
    emit(
        data,
        as_json,
        lambda rows: table(
            [(r["seq"], r["id"], r["kind"], r["actor"], r["recorded_at"], r["operation"] or "") for r in rows],
            ["seq", "id", "kind", "actor", "recorded_at", "operation"],
            right=[0],
        ),
    )


@handle_errors
def show_command(
    record_id: Annotated[str, typer.Argument(help="Record id.")], at: AtOption = None, as_json: JsonOption = False
) -> None:
    """Print a record exactly as stored."""
    state = read_state(at)
    record = state.by_id.get(record_id)
    if record is None:
        raise NotFoundError(f"record not found: {record_id}")
    emit(record.to_json(), True)


@handle_errors
def trace_command(
    record_id: Annotated[str, typer.Argument(help="Record id.")], at: AtOption = None, as_json: JsonOption = False
) -> None:
    """Show where a record came from and what happened to it since."""
    result = trace(read_state(at), record_id)

    def text(d: dict[str, Any]) -> str:
        record = d["record"]
        lines = [f"{record['id']} ({record['kind']}) recorded {record['recorded_at']} by {record['actor']}"]
        op = d.get("operation")
        if op:
            lines.append(f"  operation {op['id']} [{op['status']}] {op['tool']}: {op['summary']}")
            lines.append(
                f"    proposed by {op['actor']}, confidence {op['confidence']:.2f}; reasoning: {op['reasoning']}"
            )
            if op["decided_by"]:
                lines.append(
                    f"    decided by {op['decided_by']}" + (f": {op['decision_note']}" if op["decision_note"] else "")
                )
        for version in d.get("versions", []):
            reason = f" — {version['reason']}" if version.get("reason") else ""
            lines.append(f"  version {version['id']} ({version['kind']}, {version['recorded_at']}){reason}")
        if "current_version" in d:
            lines.append(f"  current version: {d['current_version'] or 'voided'}")
        for line in d.get("lines", []) if isinstance(d.get("lines"), list) else []:
            if isinstance(line, dict):
                lines.append(
                    f"  statement line {line['id']}: {line['date']} {line['amount']} {line['commodity']} {line.get('description', '')}"
                )
        for evidence in d.get("evidence", []):
            lines.append(f"  evidence {evidence['id']}: {evidence['filename']} (sha256 {evidence['sha256'][:12]}…)")
        for note in d.get("notes", []):
            lines.append(f"  note by {note['actor']}: {note['data']['text']}")
        for issue in d.get("issues", []):
            lines.append(f"  ISSUE: {issue}")
        return "\n".join(lines)

    emit(result, as_json, text)


LOOPBACK_BIND_HOSTS = ("127.0.0.1", "localhost")


@handle_errors
def serve_command(
    host: Annotated[str, typer.Option(help="Interface to bind: 127.0.0.1 or localhost.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port.")] = 8765,
) -> None:
    """Start the local web UI for reviewing and approving (loopback only)."""
    import uvicorn  # noqa: PLC0415

    from wealthbraid.web.app import create_app  # noqa: PLC0415

    if host not in LOOPBACK_BIND_HOSTS:
        raise PolicyError(
            f"refusing to serve on {host}: the web UI has no authentication and only runs on "
            f"{' or '.join(LOOPBACK_BIND_HOSTS)}. To reach it from elsewhere, use an authenticated tunnel "
            "such as `ssh -L 8765:127.0.0.1:8765`."
        )
    book = open_book()
    typer.echo(f"wealthbraid web UI for {book.config.name}: http://127.0.0.1:{port}/  (Ctrl+C to stop)", err=True)
    uvicorn.run(create_app(Book(book.root), port=port), host="127.0.0.1", port=port, log_level="warning")


def register(app: typer.Typer) -> None:
    """Register the book-level commands.

    Args:
        app: The root Typer app.

    """
    app.command("init")(init_command)
    app.command("status")(status_command)
    app.command("verify")(verify_command)
    app.command("schema")(schema_command)
    app.command("log")(log_command)
    app.command("show")(show_command)
    app.command("trace")(trace_command)
    app.command("serve")(serve_command)
