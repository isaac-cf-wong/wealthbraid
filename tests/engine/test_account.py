"""Tests for accounts and account-name validation."""

from __future__ import annotations

import pytest

from wealthbraid.engine.account import Account, AccountType, parse_account_name
from wealthbraid.engine.errors import InvalidAccountNameError


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("Assets:Cash", AccountType.ASSETS),
        ("Assets:Bank:Checking", AccountType.ASSETS),
        ("Liabilities:CreditCard", AccountType.LIABILITIES),
        ("Equity:Opening-Balances", AccountType.EQUITY),
        ("Income:Salary", AccountType.INCOME),
        ("Expenses:Food:Groceries", AccountType.EXPENSES),
    ],
)
def test_valid_account_names(name, expected_type):
    """Well-formed names parse to the correct account type."""
    account_type, components = parse_account_name(name)
    assert account_type is expected_type
    assert components[0] == expected_type.value


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Asset:Cash",  # unknown root
        "assets:Cash",  # lowercase root
        "Assets",  # no component below root
        "Assets:cash",  # lowercase component
        "Assets:Ca sh",  # whitespace
        "Assets::Cash",  # empty component
    ],
)
def test_invalid_account_names(name):
    """Malformed names are rejected."""
    with pytest.raises(InvalidAccountNameError):
        parse_account_name(name)


def test_account_derives_type():
    """An Account derives its type from its name."""
    assert Account("Assets:Cash").type is AccountType.ASSETS


def test_account_rejects_bad_name():
    """Constructing an Account validates the name."""
    with pytest.raises(InvalidAccountNameError):
        Account("NotARoot:Cash")


def test_account_components():
    """Components include the root."""
    assert Account("Assets:Bank:Checking").components == ("Assets", "Bank", "Checking")


def test_is_child_of():
    """An account is a child of itself and of its ancestors, not of siblings."""
    account = Account("Assets:Bank:Checking")
    assert account.is_child_of("Assets:Bank:Checking")
    assert account.is_child_of("Assets:Bank")
    assert account.is_child_of("Assets")
    assert not account.is_child_of("Assets:Bank:Savings")
    assert not account.is_child_of("Assets:Ban")


@pytest.mark.parametrize(
    ("account_type", "debit_normal"),
    [
        (AccountType.ASSETS, True),
        (AccountType.EXPENSES, True),
        (AccountType.LIABILITIES, False),
        (AccountType.EQUITY, False),
        (AccountType.INCOME, False),
    ],
)
def test_normal_balance(account_type, debit_normal):
    """Assets and Expenses are debit-normal; the rest are credit-normal."""
    assert account_type.is_debit_normal is debit_normal


def test_account_metadata_and_tags():
    """Accounts carry metadata, aliases, and tags."""
    account = Account(
        "Assets:Cash",
        metadata={"currency": "USD"},
        aliases=("cash",),
        tags=frozenset({"liquid"}),
    )
    assert account.metadata["currency"] == "USD"
    assert account.aliases == ("cash",)
    assert "liquid" in account.tags
