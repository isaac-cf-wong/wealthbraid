"""End-to-end tests of the statement pipeline: import → categorize → approve → reconcile."""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, entry, open_accounts

from wealthbraid.book.book import Book
from wealthbraid.book.config import CategorizeRule, ImportProfile
from wealthbraid.book.verify import verify_book
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import UsageError, ValidationError
from wealthbraid.services.categorize import Assignment, categorize
from wealthbraid.services.importing import fingerprint_rows, import_csv, parse_csv
from wealthbraid.services.reconcile import reconcile, reconciliation_status
from wealthbraid.services.review import review_queue

STATEMENT = b"""\xef\xbb\xbfDate,Description,Amount
01/02/2026,SALARY ACME,3000.00
03/02/2026,GROCER 123,-45.20
03/02/2026,COFFEE BAR,-3.50
03/02/2026,COFFEE BAR,-3.50
"""

PROFILE = ImportProfile(name="bank", date="Date", amount="Amount", description="Description", date_format="%d/%m/%Y")


@pytest.fixture
def bank_book(book: Book) -> Book:
    open_accounts(book, "Assets:Bank:Checking", "Income:Salary", "Expenses:Food", "Expenses:Coffee")
    return book


def _import(book: Book, path, **kwargs):
    """Import a statement and approve its lines as the human reviewer."""
    result = import_csv(book, path, actor=AGENT, profile=PROFILE, account="Assets:Bank:Checking", **kwargs)
    if result.operation is not None:
        result.operation = book.decide(result.operation.id, actor=HUMAN, verdict="approve")
    return result


def _write(tmp_path, content: bytes = STATEMENT, name: str = "feb.csv"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_parse_csv_handles_bom_formats_and_debit_credit():
    """Dates, BOM, decimal commas, parentheses negatives, and debit/credit columns parse exactly."""
    rows = parse_csv(STATEMENT, PROFILE)
    assert [(r.row, r.date.isoformat(), r.amount) for r in rows[:2]] == [
        (2, "2026-02-01", Decimal("3000.00")),
        (3, "2026-02-03", Decimal("-45.20")),
    ]
    euro = ImportProfile(
        name="eu", date="d", amount=None, debit="out", credit="in", description=None, delimiter=";", decimal_comma=True
    )
    rows = parse_csv(b"d;out;in\n2026-01-05;1.234,50;\n2026-01-06;;(10,00)\n", euro)
    assert [r.amount for r in rows] == [Decimal("-1234.50"), Decimal("-10.00")]


def test_parse_csv_reports_missing_columns_and_bad_cells():
    """Errors name the column or the row."""
    with pytest.raises(ValidationError, match="missing column"):
        parse_csv(b"when,amount\n", PROFILE)
    with pytest.raises(ValidationError, match="row 2: invalid amount"):
        parse_csv(b"Date,Description,Amount\n01/01/2026,x,abc\n", PROFILE)
    with pytest.raises(ValidationError, match="row 2: invalid date"):
        parse_csv(b"Date,Description,Amount\n2026-13-01,x,1\n", PROFILE)


def test_identical_rows_get_distinct_fingerprints():
    """Two genuine identical purchases in one file are both kept."""
    rows = parse_csv(STATEMENT, PROFILE)
    prints = fingerprint_rows(rows, account="Assets:Bank:Checking", commodity="EUR")
    assert prints[2] != prints[3]
    assert len(set(prints)) == 4


def test_import_creates_evidence_and_lines_after_approval(bank_book, tmp_path):
    """Import stores the file; lines wait for approval and never touch balances."""
    result = import_csv(bank_book, _write(tmp_path), actor=AGENT, profile=PROFILE, account="Assets:Bank:Checking")
    assert (result.rows, result.new_lines, result.duplicates) == (4, 4, [])
    assert result.operation.status == "pending"
    assert bank_book.state().lines == {}
    result.operation = bank_book.decide(result.operation.id, actor=HUMAN, verdict="approve")
    assert result.operation.data.evidence == [result.evidence]
    assert result.operation.data.inputs["profile"]["account"] == "Assets:Bank:Checking"
    state = bank_book.state()
    assert len(state.unmatched_lines()) == 4
    assert state.ledger().balance("Assets:Bank:Checking").is_empty()
    assert bank_book.store.read_evidence(state.evidence[result.evidence].sha256) == STATEMENT


def test_reimport_skips_duplicates_but_keeps_new_rows(bank_book, tmp_path):
    """An overlapping statement only adds rows not seen before."""
    _import(bank_book, _write(tmp_path))
    again = _import(bank_book, _write(tmp_path))
    assert (again.new_lines, again.duplicates, again.operation) == (0, [2, 3, 4, 5], None)
    extended = STATEMENT + b"05/02/2026,BOOKSHOP,-12.00\n"
    third = import_csv(
        bank_book, _write(tmp_path, extended, "feb2.csv"), actor=AGENT, profile=PROFILE, account="Assets:Bank:Checking"
    )
    assert (third.new_lines, third.duplicates) == (1, [2, 3, 4, 5])


def test_import_requires_open_account(book, tmp_path):
    """Importing into an unopened account fails before anything is stored."""
    with pytest.raises(ValidationError, match="account not opened"):
        import_csv(book, _write(tmp_path), actor=AGENT, profile=PROFILE, account="Assets:Bank:Checking")
    assert book.store.read_records() == []


def test_rule_categorization_proposes_matching_entries(bank_book, tmp_path):
    """Rules produce one pending entry per matched line; unmatched lines remain."""
    _import(bank_book, _write(tmp_path))
    bank_book.config = dataclasses.replace(
        bank_book.config,
        rules=(
            CategorizeRule(pattern="grocer", account="Expenses:Food", confidence=0.95),
            CategorizeRule(pattern="salary", account="Income:Salary", confidence=0.99),
        ),
    )
    operation, remaining = categorize(bank_book, actor=AGENT)
    assert operation.status == "pending"
    assert len(operation.data.changes) == 2
    assert operation.data.confidence == 0.95
    assert operation.data.inputs["source"] == "rules"
    assert len(remaining) == 2
    rationales = sorted(change.rationale for change in operation.data.changes)
    assert rationales[0].startswith("rule 1 /grocer/ matched 'GROCER 123'")

    again, remaining_again = categorize(bank_book, actor=AGENT)
    assert again is None
    assert remaining_again == remaining
    assert all(ids == [operation.id] for ids in bank_book.state().pending_line_proposals().values())

    bank_book.decide(operation.id, actor=HUMAN, verdict="approve")
    state = bank_book.state()
    assert len(state.unmatched_lines()) == 2
    assert state.pending_line_proposals() == {}
    assert state.ledger().balance("Assets:Bank:Checking").get(Commodity("EUR")) == Decimal("2954.80")
    assert verify_book(bank_book).ok


def test_agent_assignments_need_reasoning_and_valid_lines(bank_book, tmp_path):
    """Explicit assignments carry reasoning and per-line confidence; bad lines are refused."""
    _import(bank_book, _write(tmp_path))
    lines = bank_book.state().unmatched_lines()
    coffee = [line for line in lines if bank_book.state().lines[line].description == "COFFEE BAR"]
    assignments = [
        Assignment(line=line, account="Expenses:Coffee", confidence=0.55, rationale="cafe") for line in coffee
    ]
    with pytest.raises(UsageError, match="reasoning"):
        categorize(bank_book, actor=AGENT, assignments=assignments)
    with pytest.raises(UsageError, match="assigned twice"):
        categorize(bank_book, actor=AGENT, assignments=[assignments[0], assignments[0]], reasoning="r")
    with pytest.raises(ValidationError, match="unknown statement line"):
        categorize(
            bank_book, actor=AGENT, assignments=[Assignment("lin_nope", "Expenses:Coffee", 1, "x")], reasoning="r"
        )
    operation, _ = categorize(bank_book, actor=AGENT, assignments=assignments, reasoning="Card payments at a cafe.")
    assert operation.data.confidence == 0.55
    queue = review_queue(bank_book.state())
    assert queue["pending_operations"][0]["low_confidence"] is True


def test_reconcile_reports_difference_explained_by_unmatched_lines(bank_book, tmp_path):
    """Before categorizing, the whole statement movement is explained by unmatched lines."""
    _import(bank_book, _write(tmp_path))
    comparison, operation = reconcile(
        bank_book,
        actor=AGENT,
        account="Assets:Bank:Checking",
        date=dt.date(2026, 2, 28),
        statement_balance="2947.80",
    )
    assert comparison["ledger_balance"] == "0"
    assert comparison["difference"] == "2947.80"
    assert comparison["unmatched_explains_difference"] is True
    assert operation.status == "pending"


def test_reconciliation_that_later_changes_is_flagged(funded_book):
    """An approved, balanced reconciliation becomes an exception when a later correction moves the balance."""
    _, operation = reconcile(
        funded_book, actor=HUMAN, account="Assets:Bank:Checking", date=dt.date(2026, 2, 28), statement_balance="2954.80"
    )
    funded_book.decide(operation.id, actor=HUMAN, verdict="approve")
    assert [row["status"] for row in reconciliation_status(funded_book.state())] == ["balanced"]

    grocery = funded_book.state().sorted_entries()[1]
    funded_book.propose(
        actor=HUMAN,
        tool="correct",
        summary="s",
        reasoning="r",
        confidence=1.0,
        approve=True,
        changes=[
            {
                "kind": "correction",
                "data": {
                    "target": grocery.id,
                    "reason": "amount",
                    "replacement": entry("2026-02-03", ("Expenses:Food", "50.00"), ("Assets:Bank:Checking", "-50.00")),
                },
            }
        ],
    )
    (row,) = reconciliation_status(funded_book.state())
    assert row["status"] == "changed"
    assert row["current_difference"] == "4.80"
    assert review_queue(funded_book.state())["counts"]["reconciliation_exceptions"] == 1
