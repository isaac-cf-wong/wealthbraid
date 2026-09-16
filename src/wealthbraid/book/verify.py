"""Whole-book verification: the audit a human or agent runs to trust a book.

Verification rebuilds everything from the files on disk and checks that:

* every line parses and every record's id matches its content;
* sequence numbers are consecutive and each ``prev`` links to the record before it;
* the head anchor names the last record, so records deleted from the end of
  the log are noticed, and no unexpected files sit under ``records/``;
* every evidence blob exists and still hashes to its recorded digest, and no
  blob is stored without an evidence record;
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


def _verify_anchor(book: Book, records: list[Record]) -> list[Problem]:
    try:
        anchor = book.store.read_anchor()
    except IntegrityError as exc:
        return [Problem("anchor", None, str(exc))]
    if anchor is None:
        return [Problem("anchor", None, "records exist but the head anchor records/HEAD is missing")] if records else []
    seq, anchor_id = anchor["seq"], anchor["id"]
    if seq > len(records):
        return [
            Problem(
                "anchor",
                anchor_id,
                f"the log was truncated: the anchor names record {seq} ({anchor_id}) but only {len(records)} remain",
            )
        ]
    if seq < 1 or records[seq - 1].id != anchor_id:
        return [Problem("anchor", anchor_id, f"record {seq} is not the anchored record {anchor_id}")]
    if seq < len(records):
        return [
            Problem(
                "anchor",
                records[-1].id,
                f"{len(records) - seq} record(s) follow the anchored head {anchor_id}; "
                "a write was interrupted or records were appended outside wealthbraid",
            )
        ]
    return []


def verify_book(book: Book) -> VerifyReport:
    """Run every check against a book.

    Args:
        book: The book to verify.

    Returns:
        The :class:`VerifyReport`.

    """
    report = VerifyReport()
    records: list[Record] = []
    try:
        for record in book.store.iter_records():
            records.append(record)  # noqa: PERF402 - keeps the records read before a torn line
    except IntegrityError as exc:
        report.records = len(records)
        report.head = records[-1].id if records else None
        report.problems.append(
            Problem("file", None, f"{exc} ({len(records)} intact record(s) precede it; nothing after it can be read)")
        )
        return report

    report.records = len(records)
    report.head = records[-1].id if records else None
    report.problems.extend(verify_records(records))
    report.problems.extend(_verify_anchor(book, records))
    report.problems.extend(
        Problem("file", None, f"unexpected file in the record log: {path}") for path in book.store.stray_files()
    )

    state = BookState.from_records(records)
    integrity = set(state.integrity_issues)
    report.problems.extend(
        Problem("ledger", issue.record, issue.message) for issue in state.issues if issue not in integrity
    )

    for record_id, evidence in state.evidence.items():
        path = book.store.evidence_path(evidence.sha256)
        if not path.is_file():
            report.problems.append(Problem("evidence", record_id, f"blob missing: {path.relative_to(book.root)}"))
        elif hashlib.sha256(path.read_bytes()).hexdigest() != evidence.sha256:
            report.problems.append(Problem("evidence", record_id, "blob content does not match its digest"))

    proposed = {
        change.data.get("sha256")
        for operation in state.operations.values()
        if operation.status == "pending"
        for change in operation.data.changes
        if change.kind == "evidence"
    }
    known = set(state.evidence_by_sha) | proposed
    blobs = book.store.evidence_dir / "sha256"
    if blobs.is_dir():
        for path in sorted(blobs.rglob("*")):
            if path.is_file() and path.name not in known:
                report.problems.append(
                    Problem("evidence", None, f"blob without an evidence record: {path.relative_to(book.root)}")
                )

    for operation in state.operations.values():
        if operation.status == "approved":
            report.problems.append(Problem("operation", operation.id, "approved but its changes were never applied"))
    return report
