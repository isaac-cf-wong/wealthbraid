"""CLI tests: the agent-facing contract (JSON output, exit codes, actor rules) end to end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wealthbraid.cli.main import app

runner = CliRunner()
AGENT = ["--actor", "agent:claude"]
HUMAN = ["--actor", "human:alice"]


def wb(book: Path, *args: str, stdin: str | None = None):
    return runner.invoke(app, ["--book", str(book), *args], input=stdin, catch_exceptions=False)


def ok_json(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.fixture
def cli_book(tmp_path: Path) -> Path:
    root = tmp_path / "book"
    result = runner.invoke(app, ["init", str(root), "--user", "alice", "--currency", "EUR", "--json"])
    assert result.exit_code == 0, result.output
    ok_json(
        wb(
            root,
            *HUMAN,
            "open",
            "Assets:Bank:Checking",
            "Income:Salary",
            "Expenses:Food",
            "Expenses:Coffee",
            "--date",
            "2026-01-01",
            "--approve",
            "--json",
        )
    )
    return root


def test_help_lists_command_groups():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "propose",
        "review",
        "ops",
        "import",
        "categorize",
        "reconcile",
        "report",
        "explain",
        "scenario",
        "serve",
    ):
        assert command in result.stdout


def test_init_refuses_existing_book(cli_book):
    result = runner.invoke(app, ["init", str(cli_book), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_non_interactive_mutation_requires_actor(cli_book):
    """Scripts and agents must say who they are."""
    result = wb(
        cli_book, "add", "--date", "2026-02-01", "-p", "Expenses:Food 5", "-p", "Assets:Bank:Checking", "--json"
    )
    assert result.exit_code == 6
    assert "--actor" in json.loads(result.stderr)["error"]["message"]


def test_agent_proposal_then_human_approval(cli_book):
    """The full agent loop: propose (pending), fail to self-approve, human approves, balances move."""
    proposal = ok_json(
        wb(
            cli_book,
            *AGENT,
            "add",
            "--date",
            "2026-02-01",
            "--payee",
            "Cafe",
            "-p",
            "Expenses:Coffee 3.50",
            "-p",
            "Assets:Bank:Checking",
            "--reasoning",
            "Receipt photo shows a cafe purchase.",
            "--confidence",
            "0.7",
            "--json",
        )
    )
    assert proposal["status"] == "pending"
    assert proposal["changes"][0]["data"]["postings"][1] == {
        "account": "Assets:Bank:Checking",
        "amount": "-3.50",
        "commodity": "EUR",
    }

    denied = wb(cli_book, *AGENT, "ops", "approve", proposal["id"], "--json")
    assert denied.exit_code == 6
    self_approve = wb(
        cli_book,
        *AGENT,
        "add",
        "--date",
        "2026-02-01",
        "-p",
        "Expenses:Coffee 1",
        "-p",
        "Assets:Bank:Checking",
        "--reasoning",
        "r",
        "--approve",
        "--json",
    )
    assert self_approve.exit_code == 6

    balances = ok_json(wb(cli_book, "report", "balances", "--json"))
    assert balances["accounts"] == []

    approved = ok_json(wb(cli_book, *HUMAN, "ops", "approve", proposal["id"], "--note", "ok", "--json"))
    assert approved["status"] == "applied"
    assert approved["decision"]["actor"] == "human:alice"
    balances = ok_json(wb(cli_book, "report", "balances", "--json"))
    assert {r["account"]: r["balance"] for r in balances["accounts"]}["Expenses:Coffee"] == {"EUR": "3.50"}

    trace = ok_json(wb(cli_book, "trace", approved["results"][0], "--json"))
    assert trace["operation"]["reasoning"] == "Receipt photo shows a cafe purchase."
    assert ok_json(wb(cli_book, "verify", "--json"))["ok"] is True


def test_agents_must_give_reasoning(cli_book):
    result = wb(
        cli_book, *AGENT, "add", "--date", "2026-02-01", "-p", "Expenses:Food 5", "-p", "Assets:Bank:Checking", "--json"
    )
    assert result.exit_code == 2


def test_validation_errors_are_json_with_exit_code(cli_book):
    result = wb(
        cli_book,
        *HUMAN,
        "add",
        "--date",
        "2026-02-01",
        "-p",
        "Expenses:Food 5",
        "-p",
        "Assets:Bank:Checking -4",
        "--approve",
        "--json",
    )
    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "validation"
    assert result.stdout == ""


def test_generic_proposal_from_stdin(cli_book):
    proposal = {
        "tool": "agent.categorize",
        "summary": "Groceries",
        "reasoning": "Supermarket receipt.",
        "confidence": 0.9,
        "changes": [
            {
                "kind": "entry",
                "data": {
                    "date": "2026-02-02",
                    "postings": [
                        {"account": "Expenses:Food", "amount": "20.00", "commodity": "EUR"},
                        {"account": "Assets:Bank:Checking", "amount": "-20.00", "commodity": "EUR"},
                    ],
                },
            }
        ],
    }
    op = ok_json(wb(cli_book, *AGENT, "propose", "-", "--json", stdin=json.dumps(proposal)))
    assert op["status"] == "pending"
    listed = ok_json(wb(cli_book, "ops", "list", "--json"))
    assert [o["id"] for o in listed] == [op["id"]]
    rejected = ok_json(wb(cli_book, *HUMAN, "ops", "reject", op["id"], "--note", "duplicate", "--json"))
    assert rejected["status"] == "rejected"
    bad = wb(cli_book, *AGENT, "propose", "-", "--json", stdin=json.dumps({**proposal, "extra": 1}))
    assert bad.exit_code == 2


def test_statement_pipeline_via_cli(cli_book, tmp_path):
    statement = tmp_path / "jan.csv"
    statement.write_text("Date,Description,Amount\n2026-01-31,SALARY,3000.00\n2026-01-15,COFFEE BAR,-3.50\n")
    imported = ok_json(
        wb(
            cli_book,
            *AGENT,
            "import",
            "csv",
            str(statement),
            "--account",
            "Assets:Bank:Checking",
            "--date-column",
            "Date",
            "--amount-column",
            "Amount",
            "--description-column",
            "Description",
            "--json",
        )
    )
    assert (imported["new_lines"], imported["operation"]["status"]) == (2, "applied")
    lines = ok_json(wb(cli_book, "lines", "--unmatched", "--json"))
    coffee = next(line["id"] for line in lines if line["description"] == "COFFEE BAR")
    salary = next(line["id"] for line in lines if line["description"] == "SALARY")

    assignments = tmp_path / "assign.json"
    assignments.write_text(
        json.dumps(
            [
                {"line": coffee, "account": "Expenses:Coffee", "confidence": 0.8, "rationale": "cafe"},
                {"line": salary, "account": "Income:Salary", "confidence": 0.99, "rationale": "employer"},
            ]
        )
    )
    categorized = ok_json(
        wb(
            cli_book,
            *AGENT,
            "categorize",
            "--assignments",
            str(assignments),
            "--reasoning",
            "Obvious payees.",
            "--json",
        )
    )
    assert categorized["operation"]["status"] == "pending"
    assert categorized["uncategorized_lines"] == []

    review = ok_json(wb(cli_book, "review", "--json"))
    assert review["counts"]["pending_operations"] == 1
    ok_json(wb(cli_book, *HUMAN, "ops", "approve", categorized["operation"]["id"], "--json"))

    reconciled = ok_json(
        wb(
            cli_book,
            *AGENT,
            "reconcile",
            "Assets:Bank:Checking",
            "--date",
            "2026-01-31",
            "--balance",
            "2996.50",
            "--evidence",
            imported["evidence"],
            "--json",
        )
    )
    assert reconciled["comparison"]["balanced"] is True
    ok_json(wb(cli_book, *HUMAN, "ops", "approve", reconciled["operation"]["id"], "--json"))
    assert ok_json(wb(cli_book, "reconciliations", "--json"))[0]["status"] == "balanced"

    explained = ok_json(
        wb(
            cli_book,
            *AGENT,
            "explain",
            "change",
            "Assets:Bank:Checking",
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-31",
            "--record",
            "--json",
        )
    )
    assert explained["reconciles"] is True
    assert explained["recorded_operation"].startswith("opr_")

    corrected = ok_json(
        wb(
            cli_book,
            *HUMAN,
            "correct",
            ok_json(wb(cli_book, "entries", "--account", "Expenses:Coffee", "--json"))[0]["id"],
            "--reason",
            "was a food purchase",
            "-p",
            "Assets:Bank:Checking -3.50",
            "-p",
            "Expenses:Food 3.50",
            "--approve",
            "--json",
        )
    )
    assert corrected["status"] == "applied"
    assert ok_json(wb(cli_book, "reconciliations", "--json"))[0]["status"] == "balanced"
    assert ok_json(wb(cli_book, "verify", "--json"))["ok"] is True


def test_reports_are_reproducible_with_at(cli_book):
    first = ok_json(
        wb(
            cli_book,
            *HUMAN,
            "add",
            "--date",
            "2026-01-31",
            "-p",
            "Assets:Bank:Checking 100",
            "-p",
            "Income:Salary",
            "--approve",
            "--json",
        )
    )
    head = ok_json(wb(cli_book, "status", "--json"))["head"]
    ok_json(wb(cli_book, *HUMAN, "void", first["results"][0], "--reason", "test", "--approve", "--json"))
    then = ok_json(wb(cli_book, "report", "networth", "--as-of", "2026-02-01", "--at", head, "--json"))
    now = ok_json(wb(cli_book, "report", "networth", "--as-of", "2026-02-01", "--json"))
    assert (then["net_worth"], now["net_worth"]) == ("100", "0")
    assert then["basis"]["as_of_record"] == head


def test_scenario_template_and_run(cli_book, tmp_path):
    template = runner.invoke(app, ["scenario", "template"])
    path = tmp_path / "plan.toml"
    path.write_text(template.stdout)
    result = ok_json(wb(cli_book, "scenario", "run", str(path), "--json"))
    assert set(result["variants"]) == {"base", "cautious", "early-retirement"}
    assert all(v["summary"]["reconciles"] for v in result["variants"].values())


def test_verify_exit_code_on_tampering(cli_book):
    segment = next((cli_book / "records").rglob("*.jsonl"))
    segment.write_text(segment.read_text().replace("Expenses:Coffee", "Expenses:Tea"))
    result = wb(cli_book, "verify", "--json")
    assert result.exit_code == 5
    assert json.loads(result.stdout)["ok"] is False


def test_schema_outputs_json_schema():
    result = runner.invoke(app, ["schema", "operation"])
    schema = json.loads(result.stdout)
    assert "changes" in schema["properties"]
