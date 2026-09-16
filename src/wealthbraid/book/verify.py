"""Whole-book verification: the audit a human or agent runs to trust a book.

Verification rebuilds everything from the files on disk and checks that:

* every line parses and every record's id matches its content;
* sequence numbers are consecutive and each ``prev`` links to the record before it;
* every evidence blob exists and still hashes to its recorded digest;
* the projection raises no invariant issues (balancing, accounts, provenance);
* no operation was approved without its changes being applied.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from wealthbraid.book.book import Book
from wealthbraid.book.state import BookState
from wealthbraid.errors import IntegrityError
from wealthbraid.store.records import Record


@dataclass(frozen=True)
class Problem:
    """A verification finding.

    Attributes:
        check: The check that failed (``chain``, ``id``, ``evidence``, ``ledger``, ``operation``, ``file``).
        record: The record concerned, if any.
        message: What is wrong.

    """

    check: str
    record: str | None
    message: str


@dataclass
class VerifyReport:
    """The outcome of :func:`verify_book`."""

    records: int = 0
    head: str | None = None
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Report whether the book passed every check.

        Returns:
            ``True`` if no problems were found.

        """
        return not self.problems


def verify_records(records: list[Record]) -> list[Problem]:
    """Check identifiers, sequence numbers, and the hash chain.

    Args:
        records: The records in file order.

    Returns:
        The problems found.

    """
    problems: list[Problem] = []
    previous: Record | None = None
    for index, record in enumerate(records, start=1):
        expected = record.expected_id()
        if record.id != expected:
            problems.append(Problem("id", record.id, f"content does not match id (content hashes to {expected})"))
        if record.seq != index:
            problems.append(Problem("chain", record.id, f"sequence number {record.seq}, expected {index}"))
        expected_prev = previous.id if previous else None
        if record.prev != expected_prev:
            problems.append(Problem("chain", record.id, f"prev is {record.prev}, expected {expected_prev}"))
        if previous is not None and record.recorded_at < previous.recorded_at:
            problems.append(Problem("chain", record.id, "recorded_at is earlier than the previous record"))
        previous = record
    return problems


def verify_book(book: Book) -> VerifyReport:
    """Run every check against a book.

    Args:
        book: The book to verify.

    Returns:
        The :class:`VerifyReport`.

    """
    report = VerifyReport()
    try:
        records = book.store.read_records()
    except IntegrityError as exc:
        report.problems.append(Problem("file", None, str(exc)))
        return report

    report.records = len(records)
    report.head = records[-1].id if records else None
    report.problems.extend(verify_records(records))

    state = BookState.from_records(records)
    report.problems.extend(Problem("ledger", issue.record, issue.message) for issue in state.issues)

    for record_id, evidence in state.evidence.items():
        path = book.store.evidence_path(evidence.sha256)
        if not path.is_file():
            report.problems.append(Problem("evidence", record_id, f"blob missing: {path.relative_to(book.root)}"))
        elif hashlib.sha256(path.read_bytes()).hexdigest() != evidence.sha256:
            report.problems.append(Problem("evidence", record_id, "blob content does not match its digest"))

    for operation in state.operations.values():
        if operation.status == "approved":
            report.problems.append(Problem("operation", operation.id, "approved but its changes were never applied"))
    return report
