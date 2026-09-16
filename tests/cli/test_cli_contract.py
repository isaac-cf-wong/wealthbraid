"""The agent-facing CLI contract: identity, error shapes, exit codes, and output stability."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wealthbraid.cli import main as cli_main
from wealthbraid.cli.main import app

runner = CliRunner()
HUMAN = ["--actor", "human:alice"]
AGENT = ["--actor", "agent:claude"]


def wb(book: Path, *args: str, stdin: str | None = None, env: dict | None = None):
    return runner.invoke(app, ["--book", str(book), *args], input=stdin, env=env, catch_exceptions=False)


def error_of(result) -> dict:
    assert result.stdout == ""
    return json.loads(result.stderr)["error"]


@pytest.fixture
def cli_book(tmp_path: Path) -> Path:
    root = tmp_path / "book"
    assert runner.invoke(app, ["init", str(root), "--user", "alice"]).exit_code == 0
    result = wb(
        root, *HUMAN, "open", "Assets:Bank:Checking", "Expenses:Food", "--date", "2026-01-01", "--approve", "--json"
    )
    assert result.exit_code == 0, result.output
    return root


def _pending(book: Path) -> str:
    result = wb(
        book,
        *AGENT,
        "add",
        "--date",
        "2026-02-01",
        "-p",
        "Expenses:Food 5",
        "-p",
        "Assets:Bank:Checking",
        "--reasoning",
        "r",
        "--json",
    )
    return json.loads(result.stdout)["id"]


# -- identity -------------------------------------------------------------------------


class _Terminal:
    def isatty(self) -> bool:
        return True


def test_terminal_does_not_imply_a_human(cli_book, monkeypatch):
    """Even with a TTY attached, a command without an explicit actor gets no identity."""
    from wealthbraid.book.book import Book
    from wealthbraid.cli import common
    from wealthbraid.errors import PolicyError

    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(sys, "stdout", _Terminal())
    monkeypatch.delenv("WEALTHBRAID_ACTOR", raising=False)
    monkeypatch.setattr(common.OPTIONS, "actor", None)
    with pytest.raises(PolicyError):
        common.resolve_actor(Book(cli_book))


@pytest.mark.parametrize("actor", ["banana", "system:policy", "human:alice\n"])
def test_invalid_actor_is_refused_even_for_reads(cli_book, actor):
    result = runner.invoke(app, ["--book", str(cli_book), "--actor", actor, "status", "--json"])
    assert result.exit_code == 6


# -- error shapes -----------------------------------------------------------------------


def test_missing_input_files_are_not_found(cli_book, tmp_path):
    missing = str(tmp_path / "missing.json")
    for args in (["propose", missing], ["categorize", "--assignments", missing, "--reasoning", "r"]):
        result = wb(cli_book, *AGENT, *args, "--json")
        assert result.exit_code == 3, result.output
        assert error_of(result)["code"] == "not_found"


def test_directory_and_non_utf8_inputs_are_usage_errors(cli_book, tmp_path):
    binary = tmp_path / "p.json"
    binary.write_bytes(b"\xff\xfe\x00")
    for path in (tmp_path, binary):
        result = wb(cli_book, *AGENT, "propose", str(path), "--json")
        assert result.exit_code == 2, result.output
        assert error_of(result)["code"] == "usage"


@pytest.mark.parametrize(
    "argv",
    [["status", "--nope", "--json"], ["frobnicate", "--json"], ["show", "--json"]],
)
def test_click_usage_errors_are_json_when_requested(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli_main.run(argv)
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "usage"


def test_actor_after_subcommand_gets_a_placement_hint(capsys):
    with pytest.raises(SystemExit):
        cli_main.run(["ops", "approve", "opr_x", "--actor", "human:alice", "--json"])
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "before the command" in message


def test_conflict_exit_code(cli_book):
    operation = _pending(cli_book)
    assert wb(cli_book, *HUMAN, "ops", "reject", operation, "--json").exit_code == 0
    result = wb(cli_book, *HUMAN, "ops", "approve", operation, "--json")
    assert result.exit_code == 7
    assert error_of(result)["code"] == "conflict"


def test_void_and_correct_unknown_entry_are_both_not_found(cli_book):
    for args in (["void", "ent_nope", "--reason", "x"], ["correct", "ent_nope", "--reason", "x"]):
        result = wb(cli_book, *HUMAN, *args, "--json")
        assert result.exit_code == 3
        assert error_of(result)["code"] == "not_found"


def test_unknown_schema_kind_is_usage():
    result = runner.invoke(app, ["schema", "bogus", "--json"])
    assert result.exit_code == 2


# -- output stability ----------------------------------------------------------------------


def test_entries_json_has_a_stable_key_set(cli_book):
    wb(
        cli_book,
        *HUMAN,
        "add",
        "--date",
        "2026-02-01",
        "-p",
        "Expenses:Food 5",
        "-p",
        "Assets:Bank:Checking",
        "--approve",
    )
    wb(
        cli_book,
        *HUMAN,
        "add",
        "--date",
        "2026-02-02",
        "--payee",
        "P",
        "--narration",
        "N",
        "--tag",
        "t",
        "-p",
        "Expenses:Food 6",
        "-p",
        "Assets:Bank:Checking",
        "--approve",
    )
    rows = json.loads(wb(cli_book, "entries", "--json").stdout)
    assert len({tuple(sorted(row)) for row in rows}) == 1


def test_reads_warn_when_the_book_fails_integrity(cli_book):
    segment = next((cli_book / "records").rglob("*.jsonl"))
    segment.write_text(segment.read_text().replace("Expenses:Food", "Expenses:Fool", 1))
    result = wb(cli_book, "report", "balances", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["basis"]["integrity_issues"] >= 1
    assert "verify" in result.stderr


@pytest.mark.parametrize("command", [["status"], ["log"], ["review"], ["ops", "list"]])
def test_at_is_available_on_read_commands(cli_book, command):
    head = json.loads(wb(cli_book, "status", "--json").stdout)["head"]
    result = wb(cli_book, *command, "--at", head, "--json")
    assert result.exit_code == 0, result.output


def test_negative_limit_is_rejected(cli_book):
    assert wb(cli_book, "log", "--limit", "-1", "--json").exit_code == 2


@pytest.mark.parametrize("value", ["20260101", "2026-W01-1"])
def test_dates_must_be_plain_iso(cli_book, value):
    result = wb(
        cli_book, *HUMAN, "add", "--date", value, "-p", "Expenses:Food 5", "-p", "Assets:Bank:Checking", "--json"
    )
    assert result.exit_code == 2


def test_init_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "data.txt").write_text("x")
    assert runner.invoke(app, ["init", str(tmp_path), "--json"]).exit_code == 2
    assert runner.invoke(app, ["init", str(tmp_path), "--force", "--json"]).exit_code == 0


def test_empty_book_env_is_an_error(cli_book, monkeypatch):
    """An empty WEALTHBRAID_BOOK is a mistake, not a request to search upwards."""
    monkeypatch.chdir(cli_book)
    result = runner.invoke(app, ["status", "--json"], env={"WEALTHBRAID_BOOK": ""})
    assert result.exit_code == 3


def test_scenario_template_supports_json():
    result = runner.invoke(app, ["scenario", "template", "--json"])
    assert result.exit_code == 0
    assert "annual_return" in json.loads(result.stdout)["template"]


def test_serve_refuses_non_loopback_hosts(cli_book):
    for host in ("0.0.0.0", "::1", "192.168.1.5"):  # noqa: S104 - asserting these are refused
        result = wb(cli_book, "serve", "--host", host)
        assert result.exit_code == 6
    assert "--allow-remote" not in runner.invoke(app, ["serve", "--help"]).stdout
