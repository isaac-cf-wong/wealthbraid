"""Analysis commands: reports, explanations, and scenarios."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Annotated, Any

import typer

from wealthbraid.book.book import Book
from wealthbraid.cli.common import (
    AtOption,
    JsonOption,
    emit,
    format_amounts,
    handle_errors,
    json_wanted,
    open_book,
    parse_date,
    read_state,
    resolve_actor,
    table,
)
from wealthbraid.errors import NotFoundError, UsageError
from wealthbraid.services.explain import explain_change
from wealthbraid.services.reports import balances, cashflow, income_statement, net_worth
from wealthbraid.services.scenarios import SCENARIO_TEMPLATE, parse_spec, run_scenario
from wealthbraid.store.records import canonical_json

report_app = typer.Typer(
    help="Financial summaries; every report names the record it was computed from.", no_args_is_help=True
)
explain_app = typer.Typer(help="Deterministic explanations of changes.", no_args_is_help=True)
scenario_app = typer.Typer(help="Deterministic wealth projections.", no_args_is_help=True)

RecordOption = Annotated[
    bool, typer.Option("--record", help="Record the result as a note on the book head (with inputs and digest).")
]


def _today() -> dt.date:
    return dt.datetime.now().astimezone().date()


def _record_analysis(book: Book, *, tool: str, summary: str, text: str, inputs: dict[str, Any], result: Any) -> str:
    state = book.state()
    if state.head is None:
        raise UsageError("cannot record an analysis of an empty book")
    digest = hashlib.sha256(canonical_json(json.loads(json.dumps(result, default=str))).encode("utf-8")).hexdigest()
    operation = book.propose(
        actor=resolve_actor(book),
        tool=tool,
        summary=summary,
        changes=[{"kind": "note", "data": {"subjects": [state.head.id], "text": text}}],
        reasoning="Deterministic analysis; the result digest lets anyone re-run it and compare.",
        confidence=1.0,
        inputs={**inputs, "result_sha256": digest},
    )
    return operation.id


@handle_errors
def balances_command(
    as_of: Annotated[str | None, typer.Option(help="Balances as of this date (default: all entries).")] = None,
    account: Annotated[str | None, typer.Option(help="Only this account subtree.")] = None,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """Account balances with totals per account type."""
    state = read_state(at)
    report = balances(state, as_of=parse_date(as_of, "--as-of") if as_of else None, account=account)
    emit(
        report,
        as_json,
        lambda r: (
            table([(row["account"], format_amounts(row["balance"])) for row in r["accounts"]], ["account", "balance"])
            + "\n\n"
            + "\n".join(f"{root}: {format_amounts(total)}" for root, total in r["totals"].items())
        ),
    )


@handle_errors
def income_command(
    start: Annotated[str, typer.Option("--from", help="First date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--to", help="Last date (YYYY-MM-DD).")],
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """Income statement for a period."""
    report = income_statement(read_state(at), start=parse_date(start, "--from"), end=parse_date(end, "--to"))

    def text(r: dict[str, Any]) -> str:
        rows = [("Income", "")] + [(f"  {x['account']}", format_amounts(x["amount"])) for x in r["income"]]
        rows += [("Expenses", "")] + [(f"  {x['account']}", format_amounts(x["amount"])) for x in r["expenses"]]
        rows += [
            ("Total income", format_amounts(r["total_income"])),
            ("Total expenses", format_amounts(r["total_expenses"])),
            ("Net income", format_amounts(r["net_income"])),
        ]
        return table(rows, ["", "amount"])

    emit(report, as_json, text)


@handle_errors
def networth_command(
    as_of: Annotated[str | None, typer.Option(help="Valuation date (default: today).")] = None,
    currency: Annotated[str | None, typer.Option(help="Reporting currency (default: book currency).")] = None,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """Net worth valued in one currency using recorded prices."""
    book = open_book()
    report = net_worth(
        read_state(at, book),
        as_of=parse_date(as_of, "--as-of", default=_today()),
        currency=currency or book.config.currency,
    )

    def text(r: dict[str, Any]) -> str:
        lines = [
            table(
                [
                    (row["account"], format_amounts(row["balance"]), format_amounts(row["value"]))
                    for row in r["accounts"]
                ],
                ["account", "balance", f"value ({r['currency']})"],
            ),
            "",
            f"Assets {r['assets']}  Liabilities {r['liabilities']}  Net worth {r['net_worth']} {r['currency']}",
        ]
        if r["unvalued_assets"]:
            lines.append(f"Assets not valued (no price): {format_amounts(r['unvalued_assets'])}")
        if r["unvalued_liabilities"]:
            lines.append(f"Liabilities not valued (no price): {format_amounts(r['unvalued_liabilities'])}")
        lines.extend(
            f"Price {p['base']}/{p['quote']} {p['rate']} from {p['date']} ({p['age_days']} days old)"
            for p in r["prices_used"]
        )
        lines.extend(f"WARNING: {w}" for w in r["price_warnings"])
        return "\n".join(lines)

    emit(report, as_json, text)


@handle_errors
def cashflow_command(
    start: Annotated[str, typer.Option("--from", help="First date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--to", help="Last date (YYYY-MM-DD).")],
    currency: Annotated[str | None, typer.Option(help="Commodity to summarise (default: book currency).")] = None,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """Monthly income, spending, savings, and savings rate."""
    book = open_book()
    report = cashflow(
        read_state(at, book),
        start=parse_date(start, "--from"),
        end=parse_date(end, "--to"),
        currency=currency or book.config.currency,
    )
    emit(
        report,
        as_json,
        lambda r: table(
            [
                (m["period"], m["income"], m["expenses"], m["savings"], m["savings_rate_percent"] or "-")
                for m in [*r["months"], r["total"]]
            ],
            ["period", "income", "expenses", "savings", "rate %"],
            right=[1, 2, 3, 4],
        ),
    )


@handle_errors
def explain_change_command(
    account: Annotated[str, typer.Argument(help="Account (subtree) to explain.")],
    start: Annotated[str, typer.Option("--from", help="First date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--to", help="Last date (YYYY-MM-DD).")],
    record: RecordOption = False,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """Explain why an account's balance changed over a period, by counter account."""
    book = open_book()
    first, last = parse_date(start, "--from"), parse_date(end, "--to")
    result = explain_change(read_state(at, book), account=account, start=first, end=last)

    def text(r: dict[str, Any]) -> str:
        lines = [
            (
                f"{r['account']} from {first} to {last}: {format_amounts(r['opening'])} → {format_amounts(r['closing'])} "
                f"(change {format_amounts(r['change'])})"
            ),
            table(
                [(c["account"], c["amount"], c["commodity"], c["postings"]) for c in r["contributions"]],
                ["contribution from", "amount", "", "postings"],
                right=[1, 3],
            ),
            f"decomposition reconciles: {'yes' if r['reconciles'] else 'NO'}",
        ]
        return "\n".join(lines)

    if record:
        top = ", ".join(f"{c['account']} {c['amount']} {c['commodity']}" for c in result["contributions"][:5])
        result["recorded_operation"] = _record_analysis(
            book,
            tool="explain.change",
            summary=f"Explain {account} {first}..{last}",
            text=f"{account} changed by {format_amounts(result['change'])} from {first} to {last}. Largest contributions: {top}.",
            inputs={"account": account, "from": first.isoformat(), "to": last.isoformat()},
            result=result,
        )
    emit(result, as_json, text)


@handle_errors
def scenario_run_command(
    file: Annotated[Path, typer.Argument(help="Scenario file (.toml or .json).")],
    record: RecordOption = False,
    at: AtOption = None,
    as_json: JsonOption = False,
) -> None:
    """Run a scenario and its variants."""
    if not file.is_file():
        raise NotFoundError(f"file not found: {file}")
    raw_text = file.read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text) if file.suffix == ".json" else tomllib.loads(raw_text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UsageError(f"cannot parse {file}: {exc}") from exc
    spec = parse_spec(raw)
    book = open_book()
    result = run_scenario(read_state(at, book), spec, default_currency=book.config.currency)

    def text(r: dict[str, Any]) -> str:
        lines = [f"Scenario {r['scenario']} ({r['currency']}), inputs sha256 {r['inputs_sha256'][:12]}…"]
        for name, variant in r["variants"].items():
            s = variant["summary"]
            lines.append(
                f"\n{name}: start {variant['starting_amount']} ({variant['starting_amount_source']}) → end {s['end']} "
                f"(real {s['end_real']}); contributions {s['total_contributions']}, withdrawals {s['total_withdrawals']}, "
                f"growth {s['total_growth']}"
            )
            lines.append(
                table(
                    [
                        (
                            y["year"],
                            y["start"],
                            y["contributions"],
                            y["withdrawals"],
                            y["growth"],
                            y["end"],
                            y["end_real"],
                        )
                        for y in variant["years"]
                    ],
                    ["year", "start", "contrib", "withdraw", "growth", "end", "end (real)"],
                    right=[0, 1, 2, 3, 4, 5, 6],
                )
            )
        return "\n".join(lines)

    if record:
        ends = ", ".join(f"{name}: {v['summary']['end']}" for name, v in result["variants"].items())
        result["recorded_operation"] = _record_analysis(
            book,
            tool="scenario.run",
            summary=f"Scenario {spec.name}",
            text=f"Scenario {spec.name} over {spec.years} years from {spec.start} ({result['currency']}): {ends}.",
            inputs={"spec": result["spec"], "inputs_sha256": result["inputs_sha256"]},
            result=result,
        )
    emit(result, as_json, text)


@handle_errors
def scenario_template_command(as_json: JsonOption = False) -> None:
    """Print an annotated scenario file to start from."""
    if json_wanted(as_json):
        emit({"format": "toml", "template": SCENARIO_TEMPLATE}, as_json=True)
    else:
        typer.echo(SCENARIO_TEMPLATE, nl=False)


def register(app: typer.Typer) -> None:
    """Register analysis commands.

    Args:
        app: The root Typer app.

    """
    report_app.command("balances")(balances_command)
    report_app.command("income")(income_command)
    report_app.command("networth")(networth_command)
    report_app.command("cashflow")(cashflow_command)
    explain_app.command("change")(explain_change_command)
    scenario_app.command("run")(scenario_run_command)
    scenario_app.command("template")(scenario_template_command)
    app.add_typer(report_app, name="report")
    app.add_typer(explain_app, name="explain")
    app.add_typer(scenario_app, name="scenario")
