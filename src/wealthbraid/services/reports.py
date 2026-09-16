"""Financial summaries derived from book state.

Every report is a plain, JSON-compatible dictionary with amounts as decimal
strings and a ``basis`` block naming the record the report was computed from
(``as_of_record``). Re-running a report with ``--at <record>`` against the same
record log reproduces it exactly.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from wealthbraid.book.state import BookState
from wealthbraid.engine.account import AccountType, parse_account_name
from wealthbraid.engine.inventory import Inventory
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.prices import Price

_ZERO = Decimal(0)
_CENT = Decimal("0.01")
_PRICE_TOLERANCE = Decimal("0.01")


def basis(state: BookState, **parameters: Any) -> dict[str, Any]:
    """Describe what a derived view was computed from.

    Args:
        state: The book state.
        **parameters: The report parameters (dates are converted to ISO strings).

    Returns:
        The head record id, record count, counts of issues (records excluded
        from the ledger) and integrity issues (records whose content or chain
        link is broken), and the parameters.

    """
    return {
        "as_of_record": state.head.id if state.head else None,
        "records": len(state.records),
        "issues": len(state.issues),
        "integrity_issues": len(state.integrity_issues),
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

    Converted amounts are rounded to cents; native amounts are kept as recorded.
    The report lists every recorded price it relied on, with its date and age,
    and warns when a pair has direct and inverse prices that disagree by more
    than one percent. Holdings without a price path to ``currency`` are reported
    per side in ``unvalued_assets`` and ``unvalued_liabilities`` rather than
    dropped or netted.

    Args:
        state: The book state.
        as_of: The valuation date (entries and prices up to this date).
        currency: The reporting currency.

    Returns:
        Valued totals, unvalued remainders, the prices used, price warnings, and
        the per-account breakdown.

    """
    ledger = state.ledger(end=as_of)
    prices = ledger.price_db()
    target = Commodity(currency)
    totals = {AccountType.ASSETS: Inventory(), AccountType.LIABILITIES: Inventory()}
    used: dict[tuple[str, str, dt.date], Price] = {}
    rows = []
    for name, inventory in sorted(ledger.account_balances().items()):
        kind = _account_type(name)
        if inventory.is_empty() or kind not in totals:
            continue
        valued = []
        for amount in inventory.amounts():
            steps = prices.path(amount.commodity, target, as_of)
            if steps is None:
                valued.append(amount)
                continue
            rate = Decimal(1)
            for step in steps:
                rate *= step.rate
                used[(step.price.base.code, step.price.quote.code, step.price.date)] = step.price
            quantity = amount.quantity * rate
            if steps:
                quantity = quantity.quantize(_CENT, rounding=ROUND_HALF_EVEN)
            valued.append(Amount(quantity, target))
        value = Inventory.from_amounts(valued)
        rows.append({"account": name, "balance": amounts(inventory), "value": amounts(value)})
        totals[kind] = totals[kind].merge(value)
    assets, liabilities = totals[AccountType.ASSETS], totals[AccountType.LIABILITIES]
    return {
        "report": "net-worth",
        "basis": basis(state, as_of=as_of, currency=currency),
        "currency": currency,
        "assets": str(assets.get(target)),
        "liabilities": str(liabilities.get(target)),
        "net_worth": str(assets.merge(liabilities).get(target)),
        "unvalued_assets": {code: qty for code, qty in amounts(assets).items() if code != currency},
        "unvalued_liabilities": {code: qty for code, qty in amounts(liabilities).items() if code != currency},
        "prices_used": [
            {
                "base": price.base.code,
                "quote": price.quote.code,
                "rate": str(price.rate.quantity),
                "date": price.date.isoformat(),
                "age_days": (as_of - price.date).days,
            }
            for _, price in sorted(used.items())
        ],
        "price_warnings": price_warnings(prices.latest(as_of)),
        "accounts": rows,
    }


def price_warnings(latest: dict[tuple[Commodity, Commodity], Price]) -> list[str]:
    """Flag pairs whose direct and inverse prices disagree by more than one percent.

    Args:
        latest: The latest price per directed pair.

    Returns:
        One human-readable warning per inconsistent pair.

    """
    warnings = []
    for (base, quote), price in sorted(latest.items(), key=lambda item: (item[0][0].code, item[0][1].code)):
        inverse = latest.get((quote, base))
        if inverse is None or base.code > quote.code:
            continue
        product = price.rate.quantity * inverse.rate.quantity
        if abs(product - 1) > _PRICE_TOLERANCE:
            warnings.append(
                f"{base.code}/{quote.code} {price.rate.quantity} ({price.date}) and "
                f"{quote.code}/{base.code} {inverse.rate.quantity} ({inverse.date}) disagree: product {product}"
            )
    return warnings


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
