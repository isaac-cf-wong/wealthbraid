"""Financial summaries derived from book state.

Every report is a plain, JSON-compatible dictionary with amounts as decimal
strings and a ``basis`` block naming the record the report was computed from
(``as_of_record``). Re-running a report with ``--at <record>`` against the same
record log reproduces it exactly.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Any

from wealthbraid.book.state import BookState
from wealthbraid.engine.account import AccountType, parse_account_name
from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.money import Commodity
from wealthbraid.engine.prices import value_inventory

_ZERO = Decimal(0)


def basis(state: BookState, **parameters: Any) -> dict[str, Any]:
    """Describe what a derived view was computed from.

    Args:
        state: The book state.
        **parameters: The report parameters (dates are converted to ISO strings).

    Returns:
        The head record id, record count, and parameters.

    """
    return {
        "as_of_record": state.head.id if state.head else None,
        "records": len(state.records),
        "parameters": {
            key: value.isoformat() if isinstance(value, dt.date) else value
            for key, value in parameters.items()
            if value is not None
        },
    }


def amounts(inventory: Inventory) -> dict[str, str]:
    """Render an inventory as ``{commodity: quantity}``.

    Args:
        inventory: The inventory.

    Returns:
        A commodity-sorted mapping of decimal strings.

    """
    return {amount.commodity.code: str(amount.quantity) for amount in inventory.amounts()}


def _account_type(name: str) -> AccountType:
    return parse_account_name(name)[0]


def balances(state: BookState, *, as_of: dt.date | None = None, account: str | None = None) -> dict[str, Any]:
    """Return the balance of every account, with roll-ups per account type.

    Args:
        state: The book state.
        as_of: Include entries up to and including this date.
        account: Only include this account and its descendants.

    Returns:
        ``accounts`` (non-empty leaf balances) and ``totals`` by account type.

    """
    ledger = state.ledger(end=as_of)
    rows = []
    totals: dict[str, Inventory] = defaultdict(Inventory)
    for name, inventory in sorted(ledger.account_balances().items()):
        if account and not (name == account or name.startswith(account + ":")):
            continue
        if inventory.is_empty():
            continue
        rows.append({"account": name, "balance": amounts(inventory)})
        root = _account_type(name).value
        totals[root] = totals[root].merge(inventory)
    return {
        "report": "balances",
        "basis": basis(state, as_of=as_of, account=account),
        "accounts": rows,
        "totals": {root: amounts(inventory) for root, inventory in sorted(totals.items())},
    }


def income_statement(state: BookState, *, start: dt.date | None, end: dt.date | None) -> dict[str, Any]:
    """Return income and expenses over a period.

    Income is shown as a positive number (credit-normal accounts are sign-flipped).

    Args:
        state: The book state.
        start: First day of the period, inclusive.
        end: Last day of the period, inclusive.

    Returns:
        Income rows, expense rows, their totals, and net income per commodity.

    """
    ledger = state.ledger(start=start, end=end)
    income, expenses = [], []
    income_total, expense_total = Inventory(), Inventory()
    for name, inventory in sorted(ledger.account_balances().items()):
        if inventory.is_empty():
            continue
        kind = _account_type(name)
        if kind is AccountType.INCOME:
            flipped = Inventory({a.commodity: -a.quantity for a in inventory.amounts()})
            income.append({"account": name, "amount": amounts(flipped)})
            income_total = income_total.merge(flipped)
        elif kind is AccountType.EXPENSES:
            expenses.append({"account": name, "amount": amounts(inventory)})
            expense_total = expense_total.merge(inventory)
    net = income_total.merge(Inventory({a.commodity: -a.quantity for a in expense_total.amounts()}))
    return {
        "report": "income-statement",
        "basis": basis(state, start=start, end=end),
        "income": income,
        "expenses": expenses,
        "total_income": amounts(income_total),
        "total_expenses": amounts(expense_total),
        "net_income": amounts(net),
    }


def net_worth(state: BookState, *, as_of: dt.date, currency: str) -> dict[str, Any]:
    """Return assets, liabilities, and net worth valued in one currency.

    Holdings without a price path to ``currency`` are reported separately rather than dropped.

    Args:
        state: The book state.
        as_of: The valuation date (entries and prices up to this date).
        currency: The reporting currency.

    Returns:
        Valued totals, the unvalued remainder, and the per-account breakdown.

    """
    ledger = state.ledger(end=as_of)
    prices = ledger.price_db()
    target = Commodity(currency)
    assets, liabilities = Inventory(), Inventory()
    rows = []
    for name, inventory in sorted(ledger.account_balances().items()):
        kind = _account_type(name)
        if inventory.is_empty() or kind not in (AccountType.ASSETS, AccountType.LIABILITIES):
            continue
        valued = value_inventory(inventory, target, prices, as_of)
        rows.append({"account": name, "balance": amounts(inventory), "value": amounts(valued)})
        if kind is AccountType.ASSETS:
            assets = assets.merge(valued)
        else:
            liabilities = liabilities.merge(valued)
    total = assets.merge(liabilities)
    return {
        "report": "net-worth",
        "basis": basis(state, as_of=as_of, currency=currency),
        "currency": currency,
        "assets": str(assets.get(target)),
        "liabilities": str(liabilities.get(target)),
        "net_worth": str(total.get(target)),
        "unvalued": {code: qty for code, qty in amounts(total).items() if code != currency},
        "accounts": rows,
    }


def _month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def _next_month(day: dt.date) -> dt.date:
    return (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def cashflow(state: BookState, *, start: dt.date, end: dt.date, currency: str) -> dict[str, Any]:
    """Return monthly income, spending, savings, and savings rate in one commodity.

    Only postings denominated in ``currency`` are counted; this report does not
    convert, so it never silently mixes currencies.

    Args:
        state: The book state.
        start: First day of the range.
        end: Last day of the range.
        currency: The commodity to summarise.

    Returns:
        One row per calendar month plus range totals.

    """
    target = Commodity(currency)
    months: dict[dt.date, dict[str, Decimal]] = {}
    cursor = _month_start(start)
    while cursor <= end:
        months[cursor] = {"income": _ZERO, "expenses": _ZERO}
        cursor = _next_month(cursor)
    for version in state.sorted_entries():
        date = version.data.date
        if date < start or date > end:
            continue
        bucket = months[_month_start(date)]
        for posting in version.transaction.postings:
            if posting.amount is None or posting.amount.commodity != target:
                continue
            kind = _account_type(posting.account)
            if kind is AccountType.INCOME:
                bucket["income"] -= posting.amount.quantity
            elif kind is AccountType.EXPENSES:
                bucket["expenses"] += posting.amount.quantity

    def row(label: str, income: Decimal, expenses: Decimal) -> dict[str, Any]:
        savings = income - expenses
        rate = (savings / income * 100).quantize(Decimal("0.1")) if income > 0 else None
        return {
            "period": label,
            "income": str(income),
            "expenses": str(expenses),
            "savings": str(savings),
            "savings_rate_percent": str(rate) if rate is not None else None,
        }

    rows = [row(month.strftime("%Y-%m"), values["income"], values["expenses"]) for month, values in months.items()]
    total_income = sum((v["income"] for v in months.values()), _ZERO)
    total_expenses = sum((v["expenses"] for v in months.values()), _ZERO)
    return {
        "report": "cashflow",
        "basis": basis(state, start=start, end=end, currency=currency),
        "currency": currency,
        "months": rows,
        "total": row("total", total_income, total_expenses),
    }
