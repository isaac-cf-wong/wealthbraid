"""Engine error taxonomy.

These exceptions are raised by the pure accounting engine when an invariant is
violated. They are intentionally independent of the CLI and its exit codes: the
application layer catches them and maps them onto user-facing errors. Keeping
them here preserves the engine's independence from higher layers.
"""

from __future__ import annotations


class EngineError(Exception):
    """Base class for all accounting-engine errors."""


class InvalidAmountError(EngineError):
    """An amount was constructed from an invalid quantity or commodity."""


class InvalidCommodityError(EngineError):
    """A commodity code does not satisfy the commodity grammar."""


class CommodityMismatchError(EngineError):
    """An operation combined amounts of different commodities."""


class InvalidAccountNameError(EngineError):
    """An account name does not satisfy the account-name grammar."""


class UndeclaredAccountError(EngineError):
    """A transaction referenced an account that was never declared."""


class DuplicateAccountError(EngineError):
    """An account was declared more than once."""


class BalanceError(EngineError):
    """A transaction's postings do not sum to zero within tolerance."""


class AmbiguousBalanceError(EngineError):
    """A transaction has more than one posting with an elided amount."""


class UnresolvedElidedAmountError(EngineError):
    """An elided posting amount could not be inferred (no residual to absorb)."""


class InvalidTransactionError(EngineError):
    """A transaction is structurally invalid (for example, it has no postings)."""
