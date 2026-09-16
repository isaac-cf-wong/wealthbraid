"""Tests for reports, explanations, traces, and scenarios, anchored to hand or closed-form values."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, entry, open_accounts

from wealthbraid.errors import NotFoundError, ValidationError
from wealthbraid.services.explain import explain_change, trace
from wealthbraid.services.reports import balances, cashflow, income_statement, net_worth
from wealthbraid.services.scenarios import monthly_rate, parse_spec, project, run_scenario


def _apply(book, *changes):
    return book.propose(
        actor=HUMAN, tool="t", summary="s", reasoning="r", confidence=1.0, approve=True, changes=list(changes)
    )


@pytest.fixture
def rich_book(funded_book):
    """Adds a USD brokerage account, a EUR/USD price, rent, and a split salary entry."""
    open_accounts(funded_book, "Assets:Broker", "Expenses:Rent", "Expenses:Tax", date="2026-01-01")
    _apply(
        funded_book,
        {
            "kind": "entry",
            "data": entry("2026-02-01", ("Expenses:Rent", "900.00"), ("Assets:Bank:Checking", "-900.00")),
        },
        {
            "kind": "entry",
            "data": entry(
                "2026-02-28",
                ("Assets:Bank:Checking", "2400.00"),
                ("Expenses:Tax", "600.00"),
                ("Income:Salary", "-3000.00"),
            ),
        },
        {
            "kind": "entry",
            "data": entry("2026-02-15", ("Assets:Broker", "1000"), ("Equity:Opening", "-1000"), commodity="USD"),
        },
        {"kind": "price", "data": {"date": "2026-02-01", "base": "USD", "quote": "EUR", "rate": "0.90"}},
    )
    return funded_book


def test_balances_and_totals(rich_book):
    """Hand-computed: checking = 3000 - 45.20 - 900 + 2400 = 4454.80."""
    report = balances(rich_book.state())
    rows = {row["account"]: row["balance"] for row in report["accounts"]}
    assert rows["Assets:Bank:Checking"] == {"EUR": "4454.80"}
    assert report["totals"]["Assets"] == {"EUR": "4454.80", "USD": "1000"}
    assert report["basis"]["as_of_record"] == rich_book.state().head.id
    early = balances(rich_book.state(), as_of=dt.date(2026, 1, 31))
    assert {r["account"]: r["balance"] for r in early["accounts"]}["Assets:Bank:Checking"] == {"EUR": "3000.00"}


def test_income_statement_period(rich_book):
    """February: income 3000; expenses 45.20 + 900 + 600 = 1545.20; net 1454.80."""
    report = income_statement(rich_book.state(), start=dt.date(2026, 2, 1), end=dt.date(2026, 2, 28))
    assert report["total_income"] == {"EUR": "3000.00"}
    assert report["total_expenses"] == {"EUR": "1545.20"}
    assert report["net_income"] == {"EUR": "1454.80"}


def test_net_worth_values_foreign_holdings(rich_book):
    """1000 USD at 0.90 = 900 EUR; before the price exists the USD stays unvalued."""
    report = net_worth(rich_book.state(), as_of=dt.date(2026, 2, 28), currency="EUR")
    assert Decimal(report["net_worth"]) == Decimal("4454.80") + Decimal(900)
    assert report["unvalued"] == {}
    no_price = net_worth(rich_book.state(), as_of=dt.date(2026, 1, 31), currency="USD")
    assert no_price["unvalued"] == {"EUR": "3000.00"}


def test_cashflow_months_and_savings_rate(rich_book):
    """January saves everything; February saves 1454.80 of 3000 = 48.5%."""
    report = cashflow(rich_book.state(), start=dt.date(2026, 1, 1), end=dt.date(2026, 2, 28), currency="EUR")
    jan, feb = report["months"]
    assert (jan["period"], jan["savings"], jan["savings_rate_percent"]) == ("2026-01", "3000.00", "100.0")
    assert (feb["savings"], feb["savings_rate_percent"]) == ("1454.80", "48.5")


def test_explain_change_decomposes_exactly(rich_book):
    """February's checking change (+1454.80) splits into salary, tax, rent, and food contributions."""
    result = explain_change(
        rich_book.state(), account="Assets:Bank:Checking", start=dt.date(2026, 2, 1), end=dt.date(2026, 2, 28)
    )
    assert result["opening"] == {"EUR": "3000.00"}
    assert result["change"] == {"EUR": "1454.80"}
    assert result["reconciles"] is True
    by_account = {row["account"]: row["amount"] for row in result["contributions"]}
    assert by_account == {
        "Income:Salary": "3000.00",
        "Expenses:Tax": "-600.00",
        "Expenses:Rent": "-900.00",
        "Expenses:Food": "-45.20",
    }
    assert result["contributions"][0]["account"] == "Income:Salary"
    with pytest.raises(NotFoundError):
        explain_change(rich_book.state(), account="Assets:Nope", start=dt.date(2026, 1, 1), end=dt.date(2026, 2, 1))


def test_trace_follows_corrections_operations_and_notes(funded_book):
    """Tracing the original entry shows every version and the operation and decision behind it."""
    original = funded_book.state().sorted_entries()[1].id
    correction = _apply(
        funded_book,
        {
            "kind": "correction",
            "data": {
                "target": original,
                "reason": "wrong amount",
                "replacement": entry("2026-02-03", ("Expenses:Food", "46.20"), ("Assets:Bank:Checking", "-46.20")),
            },
        },
    )
    funded_book.propose(
        actor=AGENT,
        tool="explain",
        summary="s",
        reasoning="r",
        confidence=0.8,
        changes=[{"kind": "note", "data": {"subjects": [original], "text": "Receipt said 46.20."}}],
    )
    result = trace(funded_book.state(), original)
    assert [v["id"] for v in result["versions"]] == [original, correction.results[0]]
    assert result["versions"][1]["reason"] == "wrong amount"
    assert result["current_version"] == correction.results[0]
    assert result["operation"]["decided_by"] == HUMAN
    assert result["notes"][0]["data"]["text"] == "Receipt said 46.20."
    with pytest.raises(NotFoundError):
        trace(funded_book.state(), "ent_missing")


def test_monthly_rate_compounds_to_annual():
    """Twelve months at the geometric monthly rate reproduce the annual rate."""
    rate = monthly_rate(Decimal("0.07"))
    assert abs((1 + rate) ** 12 - Decimal("1.07")) < Decimal("1e-25")
    with pytest.raises(ValidationError):
        monthly_rate(Decimal(-1))


def test_projection_matches_closed_forms():
    """Anchors: pure growth is S(1+r)^n; zero return is S + 12·n·C; inflation deflates by (1+i)^n."""
    spec = parse_spec(
        {
            "name": "x",
            "start": "2026-01-01",
            "years": 10,
            "assumptions": {"starting_amount": "10000", "annual_return": "0.05", "annual_inflation": "0.02"},
        }
    )
    rows = project(spec.assumptions, start_amount=Decimal(10000), years=10)
    expected = Decimal(10000) * Decimal("1.05") ** 10
    assert Decimal(rows[-1]["end"]) == expected.quantize(Decimal("0.01"))
    assert Decimal(rows[-1]["end_real"]) == (expected / Decimal("1.02") ** 10).quantize(Decimal("0.01"))

    flat = parse_spec(
        {
            "name": "y",
            "start": "2026-01-01",
            "years": 3,
            "assumptions": {
                "starting_amount": "100",
                "monthly_contribution": "50",
                "monthly_withdrawal": "20",
                "withdrawal_start_year": 2,
            },
        }
    )
    rows = project(flat.assumptions, start_amount=Decimal(100), years=3)
    assert [r["end"] for r in rows] == ["700.00", "1060.00", "1420.00"]
    assert all(r["reconciles"] for r in rows)


def test_contributions_grow_annually():
    """Year 2 contributions are 12 * C * (1 + g)."""
    spec = parse_spec(
        {
            "name": "g",
            "start": "2026-01-01",
            "years": 2,
            "assumptions": {"starting_amount": "0", "monthly_contribution": "100", "contribution_growth": "0.10"},
        }
    )
    rows = project(spec.assumptions, start_amount=Decimal(0), years=2)
    assert [r["contributions"] for r in rows] == ["1200.00", "1320.00"]


def test_run_scenario_uses_book_net_worth_and_variants(funded_book):
    """Without a starting amount the book's net worth on the start date is used; variants override assumptions."""
    spec = parse_spec(
        {
            "name": "plan",
            "start": "2026-03-01",
            "years": 1,
            "assumptions": {"annual_return": "0"},
            "variants": {"saver": {"monthly_contribution": "100"}},
        }
    )
    result = run_scenario(funded_book.state(), spec, default_currency="EUR")
    assert result["variants"]["base"]["starting_amount"] == "2954.80"
    assert result["variants"]["base"]["starting_amount_source"] == "book net worth"
    assert result["variants"]["saver"]["summary"]["end"] == "4154.80"
    assert result["basis"]["as_of_record"] == funded_book.state().head.id
    assert len(result["inputs_sha256"]) == 64
    with pytest.raises(ValidationError, match="invalid variant"):
        parse_spec({"name": "bad", "start": "2026-01-01", "years": 1, "variants": {"v": {"annual_return": 0.05}}})
