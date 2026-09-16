"""Shared fixtures: temporary books with a deterministic clock."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from wealthbraid.book.book import Book
from wealthbraid.book.config import init_book

HUMAN = "human:alice"
AGENT = "agent:claude"


class StepClock:
    """A clock that advances one minute per call, starting at a fixed instant."""

    def __init__(self, start: dt.datetime | None = None) -> None:
        self.now = start or dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        current = self.now
        self.now += dt.timedelta(minutes=1)
        return current


@pytest.fixture
def book_root(tmp_path: Path) -> Path:
    """An initialised, empty book directory."""
    return init_book(tmp_path / "book", name="Test book", currency="EUR", user="alice")


@pytest.fixture
def book(book_root: Path) -> Book:
    """An empty book with a deterministic clock."""
    return Book(book_root, clock=StepClock())


def open_accounts(book: Book, *names: str, date: str = "2026-01-01") -> None:
    """Open accounts as the human, approved immediately."""
    book.propose(
        actor=HUMAN,
        tool="account.open",
        summary="open accounts",
        changes=[{"kind": "account.open", "data": {"account": name, "date": date}} for name in names],
        reasoning="setup",
        confidence=1.0,
        approve=True,
    )


def entry(date: str, *postings: tuple[str, str], commodity: str = "EUR", **extra) -> dict:
    """Build an entry change payload."""
    return {
        "date": date,
        "postings": [{"account": a, "amount": q, "commodity": commodity} for a, q in postings],
        **extra,
    }


@pytest.fixture
def funded_book(book: Book) -> Book:
    """A book with a checking account, salary, and groceries, plus two posted entries."""
    open_accounts(book, "Assets:Bank:Checking", "Income:Salary", "Expenses:Food", "Equity:Opening")
    book.propose(
        actor=HUMAN,
        tool="entry.add",
        summary="salary and groceries",
        changes=[
            {
                "kind": "entry",
                "data": entry(
                    "2026-01-31", ("Assets:Bank:Checking", "3000.00"), ("Income:Salary", "-3000.00"), payee="Employer"
                ),
            },
            {
                "kind": "entry",
                "data": entry(
                    "2026-02-03", ("Expenses:Food", "45.20"), ("Assets:Bank:Checking", "-45.20"), payee="Grocer"
                ),
            },
        ],
        reasoning="setup",
        confidence=1.0,
        approve=True,
    )
    return book


def client_for(app, *, client: tuple[str, int] = ("127.0.0.1", 50000), **kwargs):
    """Return a TestClient whose requests come from ``client``.

    Older Starlette versions have no ``client`` argument on ``TestClient``, so
    the address is set by a thin ASGI wrapper instead.
    """
    from starlette.testclient import TestClient

    async def with_client(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": client}
        await app(scope, receive, send)

    return TestClient(with_client, **kwargs)
