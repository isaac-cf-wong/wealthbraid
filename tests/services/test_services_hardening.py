"""Reconciliation status, scenario reporting, valuation, and smaller service edge cases."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, entry, open_accounts

from wealthbraid.book.config import CategorizeRule, ImportProfile
from wealthbraid.errors import ValidationError
from wealthbraid.services.categorize import categorize
from wealthbraid.services.explain import explain_change
from wealthbraid.services.importing import import_csv
from wealthbraid.services.reconcile import reconcile, reconciliation_status
from wealthbraid.services.reports import net_worth
from wealthbraid.services.review import review_queue
from wealthbraid.services.scenarios import parse_spec, project

FEB = dt.date(2026, 2, 28)


def _apply(book, *changes):
    return book.propose(
        actor=HUMAN, tool="t", summary="s", reasoning="r", confidence=1.0, approve=True, changes=list(changes)
    )


def _reconcile(book, balance: str, date: dt.date = FEB, account: str = "Assets:Bank:Checking"):
    _, operation = reconcile(book, actor=HUMAN, account=account, date=date, statement_balance=balance)
    return book.decide(operation.id, actor=HUMAN, verdict="approve").results[0]


# -- reconciliation status ------------------------------------------------------------


def test_fixed_discrepancy_becomes_balanced(funded_book):
    """A statement that did not match, then does after a fix, is no longer an exception."""
    record = _reconcile(funded_book, "2944.80")
    assert [r["status"] for r in reconciliation_status(funded_book.state())] == ["discrepancy"]
    _apply(
        funded_book,
        {"kind": "entry", "data": entry("2026-02-10", ("Expenses:Food", "10.00"), ("Assets:Bank:Checking", "-10.00"))},
    )
    (row,) = reconciliation_status(funded_book.state())
    assert (row["id"], row["status"]) == (record, "balanced")
    assert review_queue(funded_book.state())["counts"]["reconciliation_exceptions"] == 0


def test_newer_reconciliation_supersedes_older_for_same_account_and_date(funded_book):
    _reconcile(funded_book, "1.00")
    _reconcile(funded_book, "2954.80")
    statuses = [r["status"] for r in reconciliation_status(funded_book.state())]
    assert statuses == ["superseded", "balanced"]
    assert review_queue(funded_book.state())["counts"]["reconciliation_exceptions"] == 0


def test_back_dated_statement_line_reopens_a_reconciled_period(funded_book, tmp_path):
    _reconcile(funded_book, "2954.80")
    statement = tmp_path / "late.csv"
    statement.write_bytes(b"date,description,amount\n2026-01-15,MYSTERY FEE,-25.00\n")
    result = import_csv(
        funded_book, statement, actor=AGENT, profile=ImportProfile(name="p"), account="Assets:Bank:Checking"
    )
    funded_book.decide(result.operation.id, actor=HUMAN, verdict="approve")
    (row,) = reconciliation_status(funded_book.state())
    assert row["status"] == "new_lines"
    assert len(row["new_unmatched_lines"]) == 1
    assert review_queue(funded_book.state())["counts"]["reconciliation_exceptions"] == 1


def test_reconcile_accepts_a_parent_account(funded_book):
    open_accounts(funded_book, "Assets:Bank:Savings")
    comparison, _ = reconcile(funded_book, actor=HUMAN, account="Assets:Bank", date=FEB, statement_balance="2954.80")
    assert comparison["balanced"] is True


# -- scenarios ----------------------------------------------------------------------------


def test_reported_scenario_figures_add_up_to_the_cent():
    """start + contributions - withdrawals + growth == end, using the numbers as printed."""
    spec = parse_spec(
        {
            "name": "x",
            "start": "2026-01-01",
            "years": 10,
            "assumptions": {
                "starting_amount": "10000",
                "monthly_contribution": "1500",
                "contribution_growth": "0.02",
                "annual_return": "0.05",
                "monthly_withdrawal": "200",
                "withdrawal_start_year": 4,
            },
        }
    )
    rows = project(spec.assumptions, start_amount=Decimal(10000), years=10)
    for row in rows:
        reported = (
            Decimal(row["start"]) + Decimal(row["contributions"]) - Decimal(row["withdrawals"]) + Decimal(row["growth"])
        )
        assert reported == Decimal(row["end"]), row
        assert row["reconciles"] is True
    for earlier, later in itertools.pairwise(rows):
        assert earlier["end"] == later["start"]


def test_scenario_overflow_is_a_validation_error():
    spec = parse_spec(
        {
            "name": "x",
            "start": "2026-01-01",
            "years": 100,
            "assumptions": {"starting_amount": "1e30", "annual_return": "5"},
        }
    )
    with pytest.raises(ValidationError):
        project(spec.assumptions, start_amount=Decimal("1e30"), years=100)


# -- valuation ------------------------------------------------------------------------------


@pytest.fixture
def fx_book(funded_book):
    open_accounts(funded_book, "Assets:Broker", "Liabilities:Loan")
    _apply(
        funded_book,
        {
            "kind": "entry",
            "data": entry("2026-02-01", ("Assets:Broker", "1000"), ("Equity:Opening", "-1000"), commodity="USD"),
        },
        {"kind": "price", "data": {"date": "2020-01-01", "base": "EUR", "quote": "USD", "rate": "1.10"}},
    )
    return funded_book


def test_inverse_valuation_is_rounded_and_names_the_price_used(fx_book):
    report = net_worth(fx_book.state(), as_of=FEB, currency="EUR")
    broker = next(row for row in report["accounts"] if row["account"] == "Assets:Broker")
    assert broker["value"] == {"EUR": "909.09"}
    assert report["prices_used"] == [
        {"base": "EUR", "quote": "USD", "rate": "1.10", "date": "2020-01-01", "age_days": 2250}
    ]


def test_unvalued_holdings_are_not_netted_across_assets_and_liabilities(funded_book):
    open_accounts(funded_book, "Assets:Vault", "Liabilities:GoldLoan")
    _apply(
        funded_book,
        {
            "kind": "entry",
            "data": entry("2026-02-01", ("Assets:Vault", "3"), ("Liabilities:GoldLoan", "-3"), commodity="GOLD"),
        },
    )
    report = net_worth(funded_book.state(), as_of=FEB, currency="EUR")
    assert report["unvalued_assets"] == {"GOLD": "3"}
    assert report["unvalued_liabilities"] == {"GOLD": "-3"}


def test_contradictory_direct_and_inverse_prices_are_flagged(fx_book):
    _apply(fx_book, {"kind": "price", "data": {"date": "2026-02-01", "base": "USD", "quote": "EUR", "rate": "0.95"}})
    report = net_worth(fx_book.state(), as_of=FEB, currency="EUR")
    assert any("EUR" in w and "USD" in w for w in report["price_warnings"])


# -- smaller service gaps ------------------------------------------------------------------------


def test_rules_naming_unopened_accounts_are_skipped_not_fatal(funded_book, tmp_path):
    statement = tmp_path / "s.csv"
    statement.write_bytes(b"date,description,amount\n2026-02-05,GROCER,-5.00\n2026-02-06,CINEMA,-9.00\n")
    result = import_csv(
        funded_book, statement, actor=AGENT, profile=ImportProfile(name="p"), account="Assets:Bank:Checking"
    )
    funded_book.decide(result.operation.id, actor=HUMAN, verdict="approve")
    funded_book.config = dataclasses.replace(
        funded_book.config,
        rules=(
            CategorizeRule(pattern="grocer", account="Expenses:Food"),
            CategorizeRule(pattern="cinema", account="Expenses:Fun:Cinema"),
        ),
    )
    operation, remaining = categorize(funded_book, actor=AGENT)
    assert len(operation.data.changes) == 1
    assert operation.data.inputs["skipped_rules"] == [
        {"pattern": "cinema", "account": "Expenses:Fun:Cinema", "confidence": 0.9}
    ]
    assert len(remaining) == 1


def test_explain_rejects_an_inverted_range(funded_book):
    with pytest.raises(ValidationError, match="before"):
        explain_change(funded_book.state(), account="Assets", start=dt.date(2026, 3, 1), end=dt.date(2026, 1, 1))


def test_explain_lists_internal_transfers(funded_book):
    open_accounts(funded_book, "Assets:Cash")
    _apply(
        funded_book,
        {"kind": "entry", "data": entry("2026-02-10", ("Assets:Cash", "50"), ("Assets:Bank:Checking", "-50"))},
    )
    result = explain_change(funded_book.state(), account="Assets", start=dt.date(2026, 2, 1), end=FEB)
    assert len(result["entries"]) == 2
    assert result["reconciles"] is True
