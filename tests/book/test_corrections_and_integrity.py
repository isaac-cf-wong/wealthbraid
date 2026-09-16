"""Tests for corrections, statement-line matching, account lifecycle, and verification."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, entry, open_accounts

from wealthbraid.book.book import Book
from wealthbraid.book.verify import verify_book
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import ValidationError
from wealthbraid.store.records import Record, RecordKind

EUR = Commodity("EUR")


def _apply(book: Book, *changes: dict):
    return book.propose(
        actor=HUMAN, tool="test", summary="s", changes=list(changes), reasoning="r", confidence=1.0, approve=True
    )


def _correction(target: str, replacement: dict | None = None, reason: str = "fix") -> dict:
    data = {"target": target, "reason": reason}
    if replacement is not None:
        data["replacement"] = replacement
    return {"kind": "correction", "data": data}


def test_correction_replaces_entry_and_keeps_history(funded_book):
    """A correction supersedes the entry; the original stays in the log."""
    original = funded_book.state().sorted_entries()[1]
    fixed = entry("2026-02-03", ("Expenses:Food", "54.20"), ("Assets:Bank:Checking", "-54.20"), payee="Grocer")
    (correction_id,) = _apply(funded_book, _correction(original.id, fixed, "typo in amount")).results

    state = funded_book.state()
    assert original.id in state.by_id
    assert original.id not in state.entries
    version = state.entries[correction_id]
    assert version.origin == original.id
    assert version.history == [original.id, correction_id]
    assert state.current_version(original.id) == correction_id
    assert state.ledger().balance("Expenses:Food").get(EUR) == Decimal("54.20")


def test_stale_correction_is_rejected(funded_book):
    """Correcting a superseded version names the version that replaced it."""
    original = funded_book.state().sorted_entries()[1]
    fixed = entry("2026-02-03", ("Expenses:Food", "50"), ("Assets:Bank:Checking", "-50"))
    (correction_id,) = _apply(funded_book, _correction(original.id, fixed)).results
    with pytest.raises(ValidationError, match=f"superseded by {correction_id}"):
        _apply(funded_book, _correction(original.id, fixed))


def test_void_removes_entry_from_ledger(funded_book):
    """A correction without replacement voids the entry."""
    target = funded_book.state().sorted_entries()[1]
    _apply(funded_book, _correction(target.id))
    state = funded_book.state()
    assert state.current_version(target.id) is None
    assert state.ledger().balance("Expenses:Food").is_empty()
    with pytest.raises(ValidationError, match="voided"):
        _apply(funded_book, _correction(target.id))


def _import_line(book: Book, amount: str = "-45.20", fingerprint: str = "fp1") -> str:
    evidence_id, _ = book.add_evidence(b"statement " + fingerprint.encode(), filename="s.csv", actor=AGENT)
    operation = book.propose(
        actor=AGENT,
        tool="import",
        summary="s",
        changes=[
            {
                "kind": "line",
                "data": {
                    "evidence": evidence_id,
                    "account": "Assets:Bank:Checking",
                    "date": "2026-02-03",
                    "amount": amount,
                    "commodity": "EUR",
                    "description": "GROCER 123",
                    "fingerprint": fingerprint,
                    "row": 2,
                },
            }
        ],
        reasoning="parsed",
        confidence=1.0,
    )
    return book.decide(operation.id, actor=HUMAN, verdict="approve").results[0]


def test_entry_matching_a_line_must_post_its_amount(funded_book):
    """A line link requires a posting of exactly the line's amount to the line's account."""
    line_id = _import_line(funded_book)
    wrong = entry("2026-02-03", ("Expenses:Food", "40"), ("Assets:Bank:Checking", "-40"), lines=[line_id])
    with pytest.raises(ValidationError, match="statement lines total"):
        _apply(funded_book, {"kind": "entry", "data": wrong})
    right = entry("2026-02-03", ("Expenses:Food", "45.2"), ("Assets:Bank:Checking", "-45.20"), lines=[line_id])
    (entry_id,) = _apply(funded_book, {"kind": "entry", "data": right}).results
    state = funded_book.state()
    assert state.line_matches[line_id] == entry_id
    assert state.unmatched_lines() == []


def test_line_cannot_be_matched_twice_and_void_releases_it(funded_book):
    """Double matching is refused; voiding the match makes the line unmatched again."""
    line_id = _import_line(funded_book)
    data = entry("2026-02-03", ("Expenses:Food", "45.20"), ("Assets:Bank:Checking", "-45.20"), lines=[line_id])
    (entry_id,) = _apply(funded_book, {"kind": "entry", "data": data}).results
    with pytest.raises(ValidationError, match="already matched"):
        _apply(funded_book, {"kind": "entry", "data": data})
    _apply(funded_book, _correction(entry_id))
    assert funded_book.state().unmatched_lines() == [line_id]


def test_correction_may_keep_its_own_line(funded_book):
    """Replacing a matched entry may keep matching the same line."""
    line_id = _import_line(funded_book)
    data = entry("2026-02-03", ("Expenses:Food", "45.20"), ("Assets:Bank:Checking", "-45.20"), lines=[line_id])
    (entry_id,) = _apply(funded_book, {"kind": "entry", "data": data}).results
    fixed = {**data, "payee": "Grocer"}
    (correction_id,) = _apply(funded_book, _correction(entry_id, fixed)).results
    assert funded_book.state().line_matches[line_id] == correction_id


def test_duplicate_line_fingerprint_is_refused(funded_book):
    """Re-importing the same statement line is caught by its fingerprint."""
    _import_line(funded_book, fingerprint="same")
    with pytest.raises(ValidationError, match="duplicate statement line"):
        _import_line(funded_book, fingerprint="same")


def test_account_dates_are_enforced(book):
    """Postings before opening or after closing are refused, and closing respects later entries."""
    open_accounts(book, "Assets:Cash", "Expenses:Misc", date="2026-03-01")
    with pytest.raises(ValidationError, match="not open"):
        _apply(book, {"kind": "entry", "data": entry("2026-02-28", ("Expenses:Misc", "1"), ("Assets:Cash", "-1"))})
    _apply(book, {"kind": "entry", "data": entry("2026-03-05", ("Expenses:Misc", "1"), ("Assets:Cash", "-1"))})
    with pytest.raises(ValidationError, match="has entries after"):
        _apply(book, {"kind": "account.close", "data": {"account": "Expenses:Misc", "date": "2026-03-04"}})
    _apply(book, {"kind": "account.close", "data": {"account": "Expenses:Misc", "date": "2026-03-31"}})
    with pytest.raises(ValidationError, match="closed"):
        _apply(book, {"kind": "entry", "data": entry("2026-04-01", ("Expenses:Misc", "1"), ("Assets:Cash", "-1"))})


# -- verification ---------------------------------------------------------------


def test_clean_book_verifies(funded_book):
    """A book written only through operations passes every check."""
    funded_book.add_evidence(b"doc", filename="d.pdf", actor=AGENT)
    report = verify_book(funded_book)
    assert report.ok, report.problems
    assert report.records == len(funded_book.store.read_records())


def _rewrite_segment(book: Book, transform) -> None:
    segment = book.store.segments()[0]
    lines = segment.read_text(encoding="utf-8").splitlines()
    segment.write_text("\n".join(transform(lines)) + "\n", encoding="utf-8")


def test_edited_record_is_detected(funded_book):
    """Changing an amount in place breaks the record's content id."""

    def tamper(lines):
        return [line.replace('"-45.20"', '"-4.20"').replace('"45.20"', '"4.20"') for line in lines]

    _rewrite_segment(funded_book, tamper)
    checks = {problem.check for problem in verify_book(funded_book).problems}
    assert "id" in checks


def test_deleted_record_is_detected(funded_book):
    """Removing a line breaks the sequence and prev chain."""
    _rewrite_segment(funded_book, lambda lines: lines[:2] + lines[3:])
    checks = {problem.check for problem in verify_book(funded_book).problems}
    assert "chain" in checks


def test_forged_record_without_approval_is_detected(funded_book):
    """A correctly hashed entry appended outside the workflow fails provenance checks."""
    head = funded_book.store.head()
    forged = Record.create(
        seq=head.seq + 1,
        prev=head.id,
        kind=RecordKind.ENTRY,
        recorded_at="2026-09-02T00:00:00Z",
        actor=AGENT,
        operation=None,
        data=entry("2026-02-04", ("Expenses:Food", "999"), ("Assets:Bank:Checking", "-999")),
    )
    segment = funded_book.store.segments()[-1]
    segment.write_text(segment.read_text() + forged.to_line() + "\n", encoding="utf-8")
    report = verify_book(funded_book)
    assert [p.message for p in report.problems if p.record == forged.id and p.check == "ledger"] == [
        "record has no originating operation"
    ]
    assert funded_book.state().ledger().balance("Expenses:Food").get(EUR) == Decimal("45.20")


def test_agent_forged_decision_is_detected(funded_book):
    """An approval written by an agent directly into the log is not honoured."""
    operation = funded_book.propose(
        actor=AGENT,
        tool="t",
        summary="s",
        reasoning="r",
        confidence=0.5,
        changes=[
            {"kind": "entry", "data": entry("2026-02-05", ("Expenses:Food", "5"), ("Assets:Bank:Checking", "-5"))}
        ],
    )
    head = funded_book.store.head()
    decision = Record.create(
        seq=head.seq + 1,
        prev=head.id,
        kind=RecordKind.DECISION,
        recorded_at="2026-09-02T00:00:00Z",
        actor=AGENT,
        operation=None,
        data={"operation": operation.id, "verdict": "approve"},
    )
    segment = funded_book.store.segments()[-1]
    segment.write_text(segment.read_text() + decision.to_line() + "\n", encoding="utf-8")
    report = verify_book(funded_book)
    assert any("only a human may decide" in p.message for p in report.problems)
    assert funded_book.state().operations[operation.id].status == "pending"


def test_missing_or_altered_evidence_is_detected(funded_book):
    """Evidence blobs must exist and match their digest."""
    evidence_id, _ = funded_book.add_evidence(b"statement", filename="s.csv", actor=AGENT)
    digest = funded_book.state().evidence[evidence_id].sha256
    path = funded_book.store.evidence_path(digest)
    path.chmod(0o644)
    path.write_bytes(b"changed")
    assert [p.check for p in verify_book(funded_book).problems] == ["evidence"]
    path.unlink()
    assert "blob missing" in verify_book(funded_book).problems[0].message


def test_records_are_canonical_json_lines(funded_book):
    """Each line on disk is the record's canonical JSON."""
    for line in funded_book.store.segments()[0].read_text(encoding="utf-8").splitlines():
        record = Record.from_json(json.loads(line))
        assert record.to_line() == line
