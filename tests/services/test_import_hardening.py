"""Import deduplication and amount parsing edge cases."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, open_accounts

from wealthbraid.book.config import ImportProfile
from wealthbraid.errors import ValidationError
from wealthbraid.services.importing import fingerprint_rows, import_csv, parse_csv

REF = ImportProfile(name="ref", date="Date", amount="Amount", description="Description", external_id="Ref")
NOREF = ImportProfile(name="noref", date="Date", amount="Amount", description="Description")
ACCOUNT = "Assets:Bank:Checking"


@pytest.fixture
def bank(book):
    open_accounts(book, ACCOUNT, "Expenses:Food", "Income:Salary")
    return book


def _import(book, tmp_path, name, content, profile=REF):
    path = tmp_path / name
    path.write_bytes(content)
    result = import_csv(book, path, actor=AGENT, profile=profile, account=ACCOUNT)
    if result.operation is not None:
        book.decide(result.operation.id, actor=HUMAN, verdict="approve")
    return result


def test_reused_bank_references_do_not_hide_later_statements(bank, tmp_path):
    """References that restart each month are not treated as duplicates."""
    _import(
        bank,
        tmp_path,
        "jan.csv",
        b"Date,Description,Amount,Ref\n2026-01-05,SALARY,3000.00,1\n2026-01-06,GROCER,-45.20,2\n",
    )
    feb = _import(
        bank,
        tmp_path,
        "feb.csv",
        b"Date,Description,Amount,Ref\n2026-02-05,SALARY,3100.00,1\n2026-02-06,GROCER,-51.30,2\n",
    )
    assert (feb.new_lines, feb.duplicates) == (2, [])
    assert len(bank.state().lines) == 4


def test_same_transactions_with_and_without_references_are_deduplicated(bank, tmp_path):
    body = "2026-01-05,SALARY,3000.00{}\n2026-01-06,GROCER,-45.20{}\n"
    _import(bank, tmp_path, "a.csv", ("Date,Description,Amount,Ref\n" + body.format(",R1", ",R2")).encode())
    second = _import(
        bank, tmp_path, "b.csv", ("Date,Description,Amount\n" + body.format("", "")).encode(), profile=NOREF
    )
    assert (second.new_lines, second.duplicates) == (0, [2, 3])


def test_rows_sharing_a_reference_in_one_file_are_both_recorded(bank, tmp_path):
    result = _import(
        bank,
        tmp_path,
        "t.csv",
        b"Date,Description,Amount,Ref\n2026-01-05,TRANSFER,-500.00,ABC\n2026-01-05,FEE,-2.50,ABC\n",
    )
    assert result.new_lines == 2
    stored = sorted(line.external_id for line in bank.state().lines.values())
    assert stored == ["ABC", "ABC"]


def test_identical_rows_in_one_file_are_kept_and_reimport_is_idempotent(bank, tmp_path):
    content = b"Date,Description,Amount\n2026-01-05,COFFEE,-3.50\n2026-01-05,COFFEE,-3.50\n"
    assert _import(bank, tmp_path, "c.csv", content, profile=NOREF).new_lines == 2
    assert _import(bank, tmp_path, "c.csv", content, profile=NOREF).duplicates == [2, 3]


def test_fingerprint_depends_on_date_amount_and_commodity():
    rows = parse_csv(b"Date,Description,Amount,Ref\n2026-01-05,X,1.00,7\n", REF)
    other_date = parse_csv(b"Date,Description,Amount,Ref\n2026-02-05,X,1.00,7\n", REF)
    other_amount = parse_csv(b"Date,Description,Amount,Ref\n2026-01-05,X,2.00,7\n", REF)
    base = fingerprint_rows(rows, account=ACCOUNT, commodity="EUR")
    assert base != fingerprint_rows(other_date, account=ACCOUNT, commodity="EUR")
    assert base != fingerprint_rows(other_amount, account=ACCOUNT, commodity="EUR")
    assert base != fingerprint_rows(rows, account=ACCOUNT, commodity="USD")


def test_line_numbers_follow_the_physical_file():
    rows = parse_csv(b"Date,Description,Amount\n2026-01-05,A,1\n\n2026-01-06,B,2\n", NOREF)
    assert [row.row for row in rows] == [2, 4]


@pytest.mark.parametrize(
    ("text", "decimal_comma", "expected"),
    [
        ("1,234.50", False, "1234.50"),
        ("1234.50", False, "1234.50"),
        ("-1,234,567.8", False, "-1234567.8"),
        ("1.234,50", True, "1234.50"),
        ("1234,50", True, "1234.50"),
        ("(10,00)", True, "-10.00"),
    ],
)
def test_amounts_parse_with_valid_grouping(text, decimal_comma, expected):
    profile = ImportProfile(
        name="p", date="d", amount="a", description=None, delimiter=";", decimal_comma=decimal_comma
    )
    (row,) = parse_csv(f"d;a\n2026-01-01;{text}\n".encode(), profile)
    assert row.amount == Decimal(expected)


@pytest.mark.parametrize(
    ("text", "decimal_comma"),
    [
        ("1234,50", False),
        ("1,23", False),
        ("1,234,56", False),
        ("1,234.50", True),
        ("1.23", True),
        ("1.234.56,7", False),
    ],
)
def test_ambiguous_separators_are_rejected(text, decimal_comma):
    """A wrongly configured decimal separator is an error, never a 100x amount."""
    profile = ImportProfile(
        name="p", date="d", amount="a", description=None, delimiter=";", decimal_comma=decimal_comma
    )
    with pytest.raises(ValidationError, match="invalid amount"):
        parse_csv(f"d;a\n2026-01-01;{text}\n".encode(), profile)
