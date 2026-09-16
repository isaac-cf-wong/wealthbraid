"""The ``wealthbraid`` command-line interface.

The CLI is the agent-facing surface: every command supports ``--json``, every
mutation is recorded as an operation with its actor and reasoning, and nothing
an agent proposes changes balances until a human approves it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from wealthbraid.cli import commands_analysis, commands_book, commands_ledger, commands_ops, commands_statements
from wealthbraid.cli.common import JSON_ENV, OPTIONS, fail_message, json_wanted

app = typer.Typer(
    name="wealthbraid",
    help=(
        "Local-first, AI-native personal wealth management on an append-only, double-entry ledger.\n\n"
        "Agents propose; humans approve. Every record is traceable to its evidence and decision."
    ),
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.callback()
def main(
    book: Annotated[
        Path | None,
        typer.Option(
            "--book", "-b", help="Book directory (default: $WEALTHBRAID_BOOK or search upwards).", envvar=None
        ),
    ] = None,
    actor: Annotated[
        str | None,
        typer.Option("--actor", help="Who is acting: human:<name> or agent:<name> (default: $WEALTHBRAID_ACTOR)."),
    ] = None,
) -> None:
    """Select the book and actor for the command."""
    OPTIONS.book = book
    OPTIONS.actor = actor


commands_book.register(app)
commands_ledger.register(app)
commands_statements.register(app)
commands_ops.register(app)
commands_analysis.register(app)


_GLOBAL_OPTIONS = {"--actor", "--book", "-b"}


def run(argv: list[str] | None = None) -> None:
    """Run the CLI as a console script, keeping the error contract for parsing errors.

    Click reports unknown options, missing arguments, and bad values as plain
    text. When JSON output is requested (``--json`` or ``WEALTHBRAID_JSON``),
    those errors are printed as ``{"error": {"code": "usage", ...}}`` instead,
    with exit code 2 either way.

    Args:
        argv: The arguments, without the program name; defaults to ``sys.argv[1:]``.

    """
    args = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in args or json_wanted(flag=False)
    try:
        result = app(args=args, prog_name="wealthbraid", standalone_mode=False)
    except typer.Abort:
        sys.exit(1)
    except Exception as exc:
        # Typer vendors its own click, so parsing errors are matched by their
        # interface (a usage-class exception with exit code 2) rather than by type.
        if getattr(exc, "exit_code", None) != 2 or not hasattr(exc, "format_message"):  # noqa: PLR2004
            raise
        message = exc.format_message()
        if getattr(exc, "option_name", None) in _GLOBAL_OPTIONS:
            message += (
                f"; {exc.option_name} is a global option, so it goes before the command: "  # type: ignore[attr-defined]
                f"wealthbraid {exc.option_name} VALUE <command> ..."  # type: ignore[attr-defined]
            )
        if wants_json:
            fail_message("usage", message, as_json=True)
        else:
            context = getattr(exc, "ctx", None)
            if context is not None:
                typer.echo(context.get_usage(), err=True)
            typer.echo(f"Error: {message}", err=True)
        sys.exit(2)
    sys.exit(result if isinstance(result, int) else 0)


__all__ = ["JSON_ENV", "app", "run"]
