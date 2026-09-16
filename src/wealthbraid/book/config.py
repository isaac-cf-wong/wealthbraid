"""Book settings (``wealthbraid.toml``) and book discovery.

A book is a directory containing ``wealthbraid.toml``. Settings are not part of
the append-only log: they only shape *future* operations, and every operation
records the settings it used (import profile, categorization rules) in its
``inputs``, so past results stay reproducible after settings change.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wealthbraid.engine.errors import EngineError
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import NotFoundError, UsageError

CONFIG_NAME = "wealthbraid.toml"
BOOK_ENV = "WEALTHBRAID_BOOK"
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class CategorizeRule:
    """Assign a counter account to statement lines whose text matches a pattern.

    Attributes:
        pattern: A case-insensitive regular expression searched in payee and description.
        account: The account to assign.
        account_filter: Only apply to lines from this statement account (subtree), if set.
        confidence: The confidence recorded for matches of this rule.

    """

    pattern: str
    account: str
    account_filter: str | None = None
    confidence: float = 0.9

    def matches(self, text: str, line_account: str) -> bool:
        """Report whether the rule applies to a statement line.

        Args:
            text: The line's payee and description joined together.
            line_account: The statement account the line belongs to.

        Returns:
            ``True`` if the rule applies.

        """
        if self.account_filter and not (
            line_account == self.account_filter or line_account.startswith(self.account_filter + ":")
        ):
            return False
        return re.search(self.pattern, text, flags=re.IGNORECASE) is not None

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-compatible form for recording in operation inputs.

        Returns:
            The rule as a dictionary.

        """
        data: dict[str, Any] = {"pattern": self.pattern, "account": self.account, "confidence": self.confidence}
        if self.account_filter:
            data["account_filter"] = self.account_filter
        return data


@dataclass(frozen=True)
class ImportProfile:
    """Column mapping for a CSV statement format.

    Attributes:
        name: The profile name used on the command line.
        account: Default statement account for files of this format.
        date: Header of the date column.
        amount: Header of a signed amount column (or use ``debit``/``credit``).
        debit: Header of an outflow column (positive numbers reduce the balance).
        credit: Header of an inflow column.
        description: Header of the description column.
        payee: Header of the payee column.
        external_id: Header of a bank reference column.
        date_format: ``strptime`` format, or ``None`` for ISO dates.
        delimiter: Field delimiter.
        decimal_comma: Whether amounts use ``,`` as the decimal separator.
        commodity: The statement currency.

    """

    name: str
    account: str | None = None
    date: str = "date"
    amount: str | None = "amount"
    debit: str | None = None
    credit: str | None = None
    description: str | None = "description"
    payee: str | None = None
    external_id: str | None = None
    date_format: str | None = None
    delimiter: str = ","
    decimal_comma: bool = False
    commodity: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-compatible form for recording in operation inputs.

        Returns:
            The profile fields that are set.

        """
        return {key: value for key, value in self.__dict__.items() if value is not None}


@dataclass(frozen=True)
class BookConfig:
    """Parsed ``wealthbraid.toml`` settings.

    Attributes:
        root: The book directory.
        name: A human-friendly book name.
        currency: The reporting commodity.
        user: The local human's name; approvals from the web UI are recorded as ``human:<user>``.
        auto_apply: Whether operations with only non-sensitive changes are applied without review.
        rules: Ordered categorization rules.
        profiles: Import profiles by name.

    """

    root: Path
    name: str = "wealthbraid book"
    currency: str = "EUR"
    user: str = "owner"
    auto_apply: bool = True
    rules: tuple[CategorizeRule, ...] = ()
    profiles: dict[str, ImportProfile] = field(default_factory=dict)

    @property
    def human_actor(self) -> str:
        """Return the actor string for the local human.

        Returns:
            ``"human:<user>"``.

        """
        return f"human:{self.user}"


def load_config(root: Path) -> BookConfig:
    """Load and validate a book's settings.

    Args:
        root: The book directory.

    Returns:
        The parsed :class:`BookConfig`.

    Raises:
        NotFoundError: If the settings file does not exist.
        UsageError: If the settings are malformed.

    """
    path = root / CONFIG_NAME
    if not path.is_file():
        raise NotFoundError(f"No {CONFIG_NAME} in {root}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path}: {exc}") from exc

    book = raw.get("book", {})
    currency = str(book.get("currency", "EUR"))
    user = str(raw.get("user", {}).get("name", "owner"))
    _check_commodity(currency, path)
    if not _USER_RE.match(user):
        raise UsageError(f"{path}: user.name must contain only letters, digits, '.', '_' or '-'")

    try:
        rules = tuple(
            CategorizeRule(
                pattern=str(rule["pattern"]),
                account=str(rule["account"]),
                account_filter=rule.get("account_filter"),
                confidence=float(rule.get("confidence", 0.9)),
            )
            for rule in raw.get("rules", [])
        )
        profiles = {}
        for name, profile in raw.get("import", {}).get("profiles", {}).items():
            profiles[name] = ImportProfile(name=name, **profile)
    except (KeyError, TypeError) as exc:
        raise UsageError(f"{path}: invalid rules or import profile: {exc}") from exc
    for rule in rules:
        try:
            re.compile(rule.pattern)
        except re.error as exc:
            raise UsageError(f"{path}: invalid rule pattern {rule.pattern!r}: {exc}") from exc

    return BookConfig(
        root=root,
        name=str(book.get("name", "wealthbraid book")),
        currency=currency,
        user=user,
        auto_apply=bool(raw.get("policy", {}).get("auto_apply_non_sensitive", True)),
        rules=rules,
        profiles=profiles,
    )


def _check_commodity(code: str, path: Path) -> None:
    try:
        Commodity(code)
    except EngineError as exc:
        raise UsageError(f"{path}: book.currency: {exc}") from exc


def find_book(start: Path | None = None, explicit: Path | None = None) -> Path:
    """Locate a book directory.

    Precedence: an explicit path, then ``$WEALTHBRAID_BOOK``, then the nearest
    ancestor of ``start`` (default: the working directory) containing
    ``wealthbraid.toml``.

    Args:
        start: Where to begin the upward search.
        explicit: A path given on the command line.

    Returns:
        The book directory.

    Raises:
        NotFoundError: If no book can be found.

    """
    if explicit is not None:
        explicit = explicit.expanduser().resolve()
        if not (explicit / CONFIG_NAME).is_file():
            raise NotFoundError(f"{explicit} is not a wealthbraid book (no {CONFIG_NAME})")
        return explicit
    env = os.environ.get(BOOK_ENV)
    if env is not None:
        if not env.strip():
            raise NotFoundError(f"{BOOK_ENV} is set but empty; unset it or point it at a book")
        return find_book(explicit=Path(env))
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise NotFoundError(f"No wealthbraid book found from {current}; run `wealthbraid init` or pass --book")


CONFIG_TEMPLATE = """\
# wealthbraid book settings. Settings shape future operations only; every
# operation records the settings it used, so history stays reproducible.

[book]
name = {name}
currency = {currency}   # reporting currency for net worth and scenarios

[user]
name = {user}   # approvals from the web UI are recorded as human:<name>

[policy]
# Operations that only add evidence or notes neither change balances nor decide
# what can enter the book. When true they are applied immediately (recorded as
# approved by system:policy); everything else, including imported statement
# lines, always waits for a human decision.
auto_apply_non_sensitive = true

# Categorization rules, tried in order by `wealthbraid categorize`.
# [[rules]]
# pattern = "supermarket|grocer"      # case-insensitive, searched in payee + description
# account = "Expenses:Food:Groceries"
# confidence = 0.9                     # recorded on the proposal

# CSV import profiles, used with `wealthbraid import csv --profile NAME`.
# [import.profiles.mybank]
# account = "Assets:Bank:Checking"
# date = "Date"
# amount = "Amount"
# description = "Description"
# date_format = "%d/%m/%Y"
# commodity = "EUR"
"""

GITIGNORE_TEMPLATE = """\
# Local, regenerable state
.wealthbraid/
"""


def init_book(root: Path, *, name: str, currency: str, user: str) -> Path:
    """Create a new, empty book.

    Args:
        root: The directory to initialise (created if missing).
        name: The book name.
        currency: The reporting currency.
        user: The local human's name.

    Returns:
        The book directory.

    Raises:
        UsageError: If a book already exists there or the settings are invalid.

    """
    config_path = root / CONFIG_NAME
    if config_path.exists():
        raise UsageError(f"A book already exists at {root}")
    _check_commodity(currency, config_path)
    if not _USER_RE.match(user):
        raise UsageError("user name must contain only letters, digits, '.', '_' or '-'")
    root.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        CONFIG_TEMPLATE.format(name=_toml_string(name), currency=_toml_string(currency), user=_toml_string(user)),
        encoding="utf-8",
    )
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_TEMPLATE, encoding="utf-8")
    (root / "records").mkdir(exist_ok=True)
    (root / "evidence").mkdir(exist_ok=True)
    return root


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
