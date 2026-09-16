"""Shared CLI plumbing: global options, actor resolution, output, and errors.

Output contract (stable for agents):

* With ``--json`` (or ``WEALTHBRAID_JSON=1``) stdout carries exactly one JSON
  document and nothing else.
* Errors go to stderr; with ``--json`` they are ``{"error": {"code", "message"}}``.
* Exit codes: 0 ok, 1 error (``io`` for file-system failures), 2 usage, 3 not
  found, 4 validation, 5 integrity, 6 policy (e.g. an agent tried to approve, or
  no actor was given), 7 conflict. Command-line parsing errors follow the same
  shape when the process is started through :func:`wealthbraid.cli.main.run`.
* ``verify`` is a report: it prints its result on stdout even when the book
  fails verification, and exits 5 in that case.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer

from wealthbraid.book.book import Book, check_actor
from wealthbraid.book.state import BookState, OperationState
from wealthbraid.engine.errors import EngineError
from wealthbraid.errors import (
    NotFoundError,
    PolicyError,
    StorageError,
    UsageError,
    ValidationError,
    WealthbraidError,
)

ACTOR_ENV = "WEALTHBRAID_ACTOR"
JSON_ENV = "WEALTHBRAID_JSON"

JsonOption = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON to stdout.")]
AtOption = Annotated[
    str | None, typer.Option("--at", help="Compute as of this record id (reproduces a past view).", metavar="RECORD")
]
ApproveOption = Annotated[
    bool, typer.Option("--approve", help="Approve immediately (humans only); otherwise the operation waits for review.")
]


@dataclass
class GlobalOptions:
    """Options given before the subcommand."""

    book: Path | None = None
    actor: str | None = None


OPTIONS = GlobalOptions()
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def json_wanted(flag: bool) -> bool:
    """Report whether JSON output is requested.

    Args:
        flag: The command's ``--json`` flag.

    Returns:
        ``True`` if the flag or ``WEALTHBRAID_JSON`` asks for JSON.

    """
    return flag or os.environ.get(JSON_ENV, "").lower() in ("1", "true", "yes")


def open_book() -> Book:
    """Open the book selected by ``--book``, ``WEALTHBRAID_BOOK``, or discovery.

    Returns:
        The opened book.

    """
    return Book.discover(OPTIONS.book)


def given_actor() -> str | None:
    """Return the validated actor from ``--actor`` or ``WEALTHBRAID_ACTOR``, if any.

    Returns:
        The actor string, or ``None`` if none was given.

    Raises:
        PolicyError: If the given actor is malformed.

    """
    actor = OPTIONS.actor if OPTIONS.actor is not None else os.environ.get(ACTOR_ENV)
    if actor is None or actor == "":
        return None
    return check_actor(actor)


def resolve_actor(book: Book) -> str:
    """Determine who is acting for a command that writes.

    An identity is never inferred from the environment: a terminal being
    attached says nothing about whether a human or an agent is typing, so every
    write must name its actor with ``--actor`` or ``WEALTHBRAID_ACTOR``.

    Args:
        book: The book (its configured human is named in the error message).

    Returns:
        The actor string.

    Raises:
        PolicyError: If no actor is given.

    """
    actor = given_actor()
    if actor is not None:
        return actor
    raise PolicyError(
        f"this command writes to the book, so it must name its actor: pass --actor {book.config.human_actor} "
        f"(or agent:<name>) before the command, or set {ACTOR_ENV}"
    )


def read_state(at: str | None = None, book: Book | None = None) -> BookState:
    """Load book state for a read command, warning on stderr if integrity checks fail.

    Records that fail integrity checks are already excluded from the state, so
    the numbers never include tampered content; the warning tells the reader
    that the book needs ``wealthbraid verify``.

    Args:
        at: Stop at this record id, if given.
        book: The book; opened from the global options when omitted.

    Returns:
        The book state.

    """
    given_actor()
    state = (book or open_book()).state(at=at)
    if state.integrity_issues:
        typer.secho(
            f"warning: {len(state.integrity_issues)} record(s) fail integrity checks and were excluded; "
            "run `wealthbraid verify`",
            fg=typer.colors.YELLOW,
            err=True,
        )
    return state


def read_input(path: Path) -> str:
    """Read a UTF-8 input file, or stdin when the path is ``-``.

    Args:
        path: The path given on the command line.

    Returns:
        The file content.

    Raises:
        NotFoundError: If the file does not exist.
        UsageError: If the path is a directory or the content is not UTF-8.

    """
    if str(path) == "-":
        return typer.get_text_stream("stdin").read()
    if path.is_dir():
        raise UsageError(f"{path} is a directory, not a file")
    if not path.exists():
        raise NotFoundError(f"file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path} is not UTF-8 text: {exc}") from exc


def _default(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def emit(data: Any, as_json: bool, text: Callable[[Any], str] | None = None) -> None:
    """Print a command result.

    Args:
        data: The JSON-compatible result.
        as_json: Whether JSON output was requested.
        text: Renders the human-readable form; defaults to indented JSON.

    """
    if json_wanted(as_json) or text is None:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=_default))
    else:
        typer.echo(text(data))


def handle_errors[F: Callable[..., Any]](func: F) -> F:
    """Map wealthbraid and engine errors onto the CLI error contract.

    Args:
        func: A command function with an ``as_json`` parameter.

    Returns:
        The wrapped function.

    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except WealthbraidError as exc:
            _fail(exc, kwargs.get("as_json", False))
        except EngineError as exc:
            _fail(ValidationError(str(exc)), kwargs.get("as_json", False))
        except FileNotFoundError as exc:
            _fail(NotFoundError(f"file not found: {exc.filename}"), kwargs.get("as_json", False))
        except (IsADirectoryError, NotADirectoryError, UnicodeDecodeError) as exc:
            _fail(UsageError(str(exc)), kwargs.get("as_json", False))
        except OSError as exc:
            _fail(StorageError(str(exc)), kwargs.get("as_json", False))

    return wrapper  # type: ignore[return-value]


def fail_message(code: str, message: str, as_json: bool) -> None:
    """Print an error in the CLI error shape.

    Args:
        code: The error code.
        message: The message.
        as_json: Whether JSON output was requested.

    """
    if json_wanted(as_json):
        typer.echo(json.dumps({"error": {"code": code, "message": message}}), err=True)
    else:
        typer.secho(f"error [{code}]: {message}", fg=typer.colors.RED, err=True)


def _fail(exc: WealthbraidError, as_json: bool) -> None:
    fail_message(exc.code, str(exc), as_json)
    raise typer.Exit(exc.exit_code)


def parse_date(value: str | None, name: str, *, default: dt.date | None = None) -> dt.date:
    """Parse an ISO date option.

    Args:
        value: The option value.
        name: The option name, for errors.
        default: Returned when ``value`` is empty.

    Returns:
        The date.

    Raises:
        UsageError: If the value is missing without a default or malformed.

    """
    if not value:
        if default is None:
            raise UsageError(f"{name} is required (YYYY-MM-DD)")
        return default
    try:
        if not _DATE_RE.fullmatch(value):
            raise ValueError(value)
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise UsageError(f"{name} must be a date (YYYY-MM-DD), got {value!r}") from exc


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str], *, right: Sequence[int] = ()) -> str:
    """Render rows as a plain aligned text table.

    Args:
        rows: The table rows.
        headers: Column headers.
        right: Indices of right-aligned (numeric) columns.

    Returns:
        The table text.

    """
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]

    def line(row: list[str]) -> str:
        return "  ".join(
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]) for i, cell in enumerate(row)
        ).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line(cells[0]), rule, *(line(row) for row in cells[1:])])


def format_amounts(mapping: dict[str, str]) -> str:
    """Render ``{commodity: quantity}`` compactly.

    Args:
        mapping: Amounts by commodity.

    Returns:
        Text such as ``"12.50 EUR, 3 USD"`` (``"0"`` when empty).

    """
    return ", ".join(f"{qty} {code}" for code, qty in mapping.items()) or "0"


def operation_json(operation: OperationState) -> dict[str, Any]:
    """Summarise an operation for output.

    Args:
        operation: The operation.

    Returns:
        A JSON-compatible summary including provenance and status.

    """
    return {
        "id": operation.id,
        "status": operation.status,
        "tool": operation.data.tool,
        "summary": operation.data.summary,
        "actor": operation.record.actor,
        "proposed_at": operation.record.recorded_at,
        "reasoning": operation.data.reasoning,
        "confidence": operation.data.confidence,
        "evidence": operation.data.evidence,
        "inputs": operation.data.inputs,
        "book": str(OPTIONS.book.resolve()) if OPTIONS.book else None,
        "base": operation.data.base,
        "sensitive": operation.sensitive,
        "changes": [change.model_dump(mode="json", exclude_none=True) for change in operation.data.changes],
        "decision": (
            {
                "id": operation.decision.id,
                "actor": operation.decision.actor,
                "verdict": operation.decision.data.get("verdict"),
                "note": operation.decision.data.get("note"),
                "decided_at": operation.decision.recorded_at,
            }
            if operation.decision
            else None
        ),
        "results": operation.results,
    }


def operation_text(data: dict[str, Any]) -> str:
    """Render an operation summary for humans.

    Args:
        data: The output of :func:`operation_json`.

    Returns:
        A short multi-line description.

    """
    lines = [
        f"{data['id']}  [{data['status']}]  {data['summary']}",
        f"  proposed by {data['actor']} at {data['proposed_at']} with confidence {data['confidence']:.2f}",
        f"  reasoning: {data['reasoning']}",
        f"  changes: {len(data['changes'])} ({', '.join(sorted({c['kind'] for c in data['changes']}))})",
    ]
    if data["decision"]:
        decision = data["decision"]
        lines.append(
            f"  {decision['verdict']}d by {decision['actor']}" + (f": {decision['note']}" if decision["note"] else "")
        )
    if data["results"]:
        lines.append(f"  results: {', '.join(data['results'])}")
    if data["status"] == "pending":
        lines.append(f"  waiting for review: `wealthbraid ops approve {data['id']}` or the web UI")
    return "\n".join(lines)
