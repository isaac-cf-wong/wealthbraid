"""The ``wealthbraid`` command-line interface.

The CLI is the agent-facing surface: every command supports ``--json``, every
mutation is recorded as an operation with its actor and reasoning, and nothing
an agent proposes changes balances until a human approves it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from wealthbraid.cli import commands_analysis, commands_book, commands_ledger, commands_ops, commands_statements
from wealthbraid.cli.common import OPTIONS

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
