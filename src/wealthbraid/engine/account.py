"""Accounts: the five account types and the :class:`Account` value type.

Account names are hierarchical, colon-delimited paths whose first component is
one of the five canonical roots (Assets, Liabilities, Equity, Income, Expenses).
Names are validated against a strict grammar so that typos become errors rather
than silently created accounts.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from wealthbraid.engine.errors import InvalidAccountNameError

# Each non-root component starts with an uppercase letter or digit, then allows
# letters, digits, hyphen, and underscore.
_COMPONENT_RE = re.compile(r"[A-Z0-9][A-Za-z0-9_-]*")
_ACCOUNT_SEPARATOR = ":"

# A valid account has a root plus at least one further component.
_MIN_COMPONENTS = 2


class AccountType(enum.Enum):
    """The five roots of a double-entry chart of accounts.

    The enum value is the canonical root name used as the first component of an
    account path.
    """

    ASSETS = "Assets"
    LIABILITIES = "Liabilities"
    EQUITY = "Equity"
    INCOME = "Income"
    EXPENSES = "Expenses"

    @property
    def is_debit_normal(self) -> bool:
        """Report whether this account type increases on the debit side.

        Assets and Expenses are debit-normal; Liabilities, Equity, and Income
        are credit-normal. This underpins report sign conventions.

        Returns:
            ``True`` for Assets and Expenses, ``False`` otherwise.

        """
        return self in (AccountType.ASSETS, AccountType.EXPENSES)

    @classmethod
    def from_root(cls, root: str) -> AccountType:
        """Return the account type for a root component name.

        Args:
            root: The first component of an account path (e.g. ``"Assets"``).

        Returns:
            The matching :class:`AccountType`.

        Raises:
            InvalidAccountNameError: If ``root`` is not a canonical root.

        """
        for account_type in cls:
            if account_type.value == root:
                return account_type
        valid = ", ".join(t.value for t in cls)
        raise InvalidAccountNameError(f"Account root must be one of: {valid}; got {root!r}")


def parse_account_name(name: str) -> tuple[AccountType, tuple[str, ...]]:
    """Validate an account name and split it into its type and components.

    Args:
        name: The full colon-delimited account name.

    Returns:
        A tuple of the account's :class:`AccountType` and its components
        (including the root).

    Raises:
        InvalidAccountNameError: If the name is empty, has an unknown root, or
            contains a malformed component.

    """
    if not name:
        raise InvalidAccountNameError("Account name must not be empty")
    components = name.split(_ACCOUNT_SEPARATOR)
    account_type = AccountType.from_root(components[0])
    for component in components[1:]:
        if not _COMPONENT_RE.fullmatch(component):
            raise InvalidAccountNameError(f"Invalid account component {component!r} in {name!r}")
    if len(components) < _MIN_COMPONENTS:
        raise InvalidAccountNameError(f"Account {name!r} needs at least one component below the root")
    return account_type, tuple(components)


@dataclass(frozen=True)
class Account:
    """A declared account in the chart of accounts.

    Attributes:
        name: The full colon-delimited account name.
        type: The account's root type, derived from ``name``.
        metadata: Arbitrary key/value metadata.
        aliases: Alternative names that resolve to this account.
        tags: Free-form tags attached to the account.

    """

    name: str
    type: AccountType = field(init=False)
    metadata: Mapping[str, str] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Validate the account name and derive its type.

        Raises:
            InvalidAccountNameError: If the name is malformed.

        """
        account_type, _ = parse_account_name(self.name)
        object.__setattr__(self, "type", account_type)

    @property
    def components(self) -> tuple[str, ...]:
        """Return the account's path components, including the root.

        Returns:
            The colon-separated components as a tuple.

        """
        return tuple(self.name.split(_ACCOUNT_SEPARATOR))

    def is_child_of(self, other: str) -> bool:
        """Report whether this account is ``other`` or a descendant of it.

        Args:
            other: A candidate ancestor account name.

        Returns:
            ``True`` if this account equals ``other`` or lies beneath it.

        """
        return self.name == other or self.name.startswith(other + _ACCOUNT_SEPARATOR)
