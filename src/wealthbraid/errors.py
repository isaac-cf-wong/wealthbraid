"""Application error taxonomy with stable codes and exit statuses.

Engine errors describe accounting-invariant violations; these errors describe
what a caller of wealthbraid (a human, an agent, or the web UI) did wrong or
what went wrong with the book. Each carries a machine-readable ``code`` and a
process ``exit_code`` so the CLI can report failures consistently.
"""

from __future__ import annotations


class WealthbraidError(Exception):
    """Base class for all wealthbraid application errors."""

    code = "error"
    exit_code = 1


class UsageError(WealthbraidError):
    """The request was malformed or inconsistent."""

    code = "usage"
    exit_code = 2


class NotFoundError(WealthbraidError):
    """A referenced book, record, or file does not exist."""

    code = "not_found"
    exit_code = 3


class ValidationError(WealthbraidError):
    """Proposed changes violate accounting or book invariants."""

    code = "validation"
    exit_code = 4


class IntegrityError(WealthbraidError):
    """The on-disk book is corrupt, tampered with, or inconsistent."""

    code = "integrity"
    exit_code = 5


class PolicyError(WealthbraidError):
    """The action is not permitted for this actor (for example, approval by an agent)."""

    code = "policy"
    exit_code = 6


class ConflictError(WealthbraidError):
    """The action conflicts with the book's current state (for example, a stale correction)."""

    code = "conflict"
    exit_code = 7
