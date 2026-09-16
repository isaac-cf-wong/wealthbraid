"""Hardening of the approval workflow, corrections, and integrity guarantees."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, entry, open_accounts

from wealthbraid.book.book import Book, check_actor
from wealthbraid.book.verify import verify_book
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import PolicyError, ValidationError
from wealthbraid.store.records import RecordKind

EUR = Commodity("EUR")


def _apply(book: Book, *changes: dict, actor: str = HUMAN):
    return book.propose(
        actor=actor, tool="t", summary="s", reasoning="r", confidence=1.0, approve=True, changes=list(changes)
    )


def _line(
    evidence: str, fingerprint: str, amount: str = "-3.50", description: str = "COFFEE BAR", date: str = "2026-02-03"
):
    return {
        "kind": "line",
        "data": {
            "evidence": evidence,
            "account": "Assets:Bank:Checking",
            "date": date,
            "amount": amount,
            "commodity": "EUR",
            "description": description,
            "fingerprint": fingerprint,
            "row": 2,
        },
    }


# -- actors -----------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["human:alice\n", "human:alice ", " human:alice", "human:ali\tce"])
def test_actor_must_match_exactly(actor):
    """Trailing newlines and whitespace do not pass as a human identity."""
    with pytest.raises(PolicyError):
        check_actor(actor)


def test_near_miss_human_cannot_decide(funded_book):
    """A decision under 'human:alice\\n' is refused and nothing is written."""
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
    count = len(funded_book.store.read_records())
    with pytest.raises(PolicyError):
        funded_book.decide(operation.id, actor="human:alice\n", verdict="approve")
    assert len(funded_book.store.read_records()) == count


# -- statement lines are sensitive --------------------------------------------------


def test_agent_cannot_add_statement_lines_without_human_approval(funded_book):
    """Lines gate what enters the book, so policy never auto-applies them."""
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    operation = funded_book.propose(
        actor=AGENT, tool="squat", summary="s", reasoning="r", confidence=1.0, changes=[_line(evidence_id, "fp")]
    )
    assert operation.status == "pending"
    assert funded_book.state().lines == {}


def test_statement_line_can_be_voided_and_its_fingerprint_released(funded_book):
    """A wrong line is corrected by a new record; the real row can then be recorded."""
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    (fake,) = _apply(funded_book, _line(evidence_id, "fp", amount="-0.01", description="NOTHING")).results
    with pytest.raises(ValidationError, match="duplicate statement line"):
        _apply(funded_book, _line(evidence_id, "fp"))
    _apply(funded_book, {"kind": "correction", "data": {"target": fake, "reason": "fabricated"}})
    (real,) = _apply(funded_book, _line(evidence_id, "fp")).results
    state = funded_book.state()
    assert list(state.lines) == [real]
    assert state.voided[fake]


def test_statement_line_replacement_keeps_history(funded_book):
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    (line,) = _apply(funded_book, _line(evidence_id, "fp", amount="-350")).results
    replacement = _line(evidence_id, "fp", amount="-3.50")["data"]
    (fixed,) = _apply(
        funded_book, {"kind": "correction", "data": {"target": line, "replacement": replacement, "reason": "decimal"}}
    ).results
    state = funded_book.state()
    assert state.lines[fixed].amount == "-3.50"
    assert state.superseded[line] == fixed


def test_matched_line_cannot_be_corrected(funded_book):
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    (line,) = _apply(funded_book, _line(evidence_id, "fp")).results
    _apply(
        funded_book,
        {
            "kind": "entry",
            "data": entry("2026-02-03", ("Expenses:Food", "3.50"), ("Assets:Bank:Checking", "-3.50"), lines=[line]),
        },
    )
    with pytest.raises(ValidationError, match="matched by"):
        _apply(funded_book, {"kind": "correction", "data": {"target": line, "reason": "x"}})


# -- line matching is aggregate ------------------------------------------------------


def test_one_posting_cannot_match_two_lines(funded_book):
    """Two -3.50 lines need -7.00 posted to the statement account."""
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    first, second = _apply(funded_book, _line(evidence_id, "a"), _line(evidence_id, "b")).results
    once = entry("2026-02-03", ("Expenses:Food", "3.50"), ("Assets:Bank:Checking", "-3.50"), lines=[first, second])
    with pytest.raises(ValidationError, match="statement lines"):
        _apply(funded_book, {"kind": "entry", "data": once})
    both = entry("2026-02-03", ("Expenses:Food", "7.00"), ("Assets:Bank:Checking", "-7.00"), lines=[first, second])
    _apply(funded_book, {"kind": "entry", "data": both})
    assert funded_book.state().unmatched_lines() == []


# -- references only in reference fields -----------------------------------------------


def test_dollar_text_in_free_text_fields_is_stored_verbatim(funded_book):
    """ "$0" in a description is data, not a reference; "$1" does not break the proposal."""
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    operation = _apply(
        funded_book,
        _line(evidence_id, "a", description="COFFEE"),
        _line(evidence_id, "b", description="$0"),
        _line(evidence_id, "c", description="$1"),
    )
    state = funded_book.state()
    assert [state.lines[i].description for i in operation.results] == ["COFFEE", "$0", "$1"]
    assert verify_book(funded_book).ok


def test_references_still_resolve_in_reference_fields(funded_book):
    digest = funded_book.store.put_evidence(b"receipt")
    operation = _apply(
        funded_book,
        {"kind": "evidence", "data": {"sha256": digest, "filename": "r.txt", "size": 7}},
        {"kind": "note", "data": {"subjects": ["$0"], "text": "costs $0 extra"}},
    )
    note = funded_book.state().by_id[operation.results[1]]
    assert note.data == {"subjects": [operation.results[0]], "text": "costs $0 extra"}


# -- chart of accounts can be corrected -----------------------------------------------


def test_account_open_date_can_be_corrected(book):
    open_accounts(book, "Assets:Bank:Checking", date="2026-06-01")
    open_accounts(book, "Expenses:Food", date="2026-01-01")
    january = {"kind": "entry", "data": entry("2026-01-15", ("Expenses:Food", "10"), ("Assets:Bank:Checking", "-10"))}
    with pytest.raises(ValidationError, match="not open"):
        _apply(book, january)
    open_record = book.state().accounts["Assets:Bank:Checking"].open_record
    _apply(
        book,
        {
            "kind": "correction",
            "data": {
                "target": open_record,
                "reason": "typo",
                "replacement": {"account": "Assets:Bank:Checking", "date": "2026-01-01"},
            },
        },
    )
    _apply(book, january)
    assert book.state().accounts["Assets:Bank:Checking"].opened.isoformat() == "2026-01-01"


def test_account_open_correction_cannot_strand_entries_or_rename(funded_book):
    record = funded_book.state().accounts["Assets:Bank:Checking"].open_record
    with pytest.raises(ValidationError, match="entries before"):
        _apply(
            funded_book,
            {
                "kind": "correction",
                "data": {
                    "target": record,
                    "reason": "x",
                    "replacement": {"account": "Assets:Bank:Checking", "date": "2026-02-01"},
                },
            },
        )
    with pytest.raises(ValidationError, match="same account"):
        _apply(
            funded_book,
            {
                "kind": "correction",
                "data": {
                    "target": record,
                    "reason": "x",
                    "replacement": {"account": "Assets:Bank:Savings", "date": "2026-01-01"},
                },
            },
        )


def test_mistaken_account_close_can_be_voided(funded_book):
    (close,) = _apply(
        funded_book, {"kind": "account.close", "data": {"account": "Expenses:Food", "date": "2026-06-30"}}
    ).results
    later = {"kind": "entry", "data": entry("2026-07-01", ("Expenses:Food", "1"), ("Assets:Bank:Checking", "-1"))}
    with pytest.raises(ValidationError, match="closed"):
        _apply(funded_book, later)
    _apply(funded_book, {"kind": "correction", "data": {"target": close, "reason": "closed the wrong account"}})
    _apply(funded_book, later)
    assert funded_book.state().accounts["Expenses:Food"].closed is None


# -- tampering is excluded, not just reported ----------------------------------------------


def _rewrite(book: Book, transform) -> None:
    segment = book.store.segments()[0]
    lines = segment.read_text(encoding="utf-8").splitlines()
    segment.write_text("\n".join(transform(lines)) + "\n", encoding="utf-8")


def test_edited_record_is_excluded_from_the_ledger(funded_book):
    """A balanced in-place edit is caught by its id and its amounts never reach reports."""

    def tamper(lines):
        return [line.replace('"-45.20"', '"-4.20"').replace('"45.20"', '"4.20"') for line in lines]

    _rewrite(funded_book, tamper)
    state = funded_book.state()
    assert state.ledger().balance("Expenses:Food").get(EUR) != Decimal("4.20")
    assert any("does not match its id" in issue.message for issue in state.issues)
    assert not verify_book(funded_book).ok


def test_writes_are_refused_on_a_book_that_fails_integrity(funded_book):
    _rewrite(funded_book, lambda lines: [line.replace('"45.20"', '"4.20"') for line in lines])
    from wealthbraid.errors import IntegrityError

    with pytest.raises(IntegrityError):
        _apply(
            funded_book,
            {"kind": "entry", "data": entry("2026-02-05", ("Expenses:Food", "1"), ("Assets:Bank:Checking", "-1"))},
        )


def test_truncated_log_tail_is_detected(funded_book):
    """Deleting the newest records is caught against the head anchor."""
    count = len(funded_book.store.read_records())
    _rewrite(funded_book, lambda lines: lines[:-4])
    report = verify_book(funded_book)
    assert not report.ok
    assert any(p.check == "anchor" and str(count) in p.message for p in report.problems)


def test_unexpected_files_under_records_are_reported(funded_book):
    stray = funded_book.store.records_dir / "2026" / "09x.jsonl"
    stray.write_text(json.dumps({"hidden": True}) + "\n")
    assert any(p.check == "file" and "09x.jsonl" in p.message for p in verify_book(funded_book).problems)


def test_forged_policy_decision_is_refused_by_the_projection(funded_book):
    """Policy can never approve a line, even written directly into the log."""
    from wealthbraid.store.records import Record

    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    operation = funded_book.propose(
        actor=AGENT, tool="t", summary="s", reasoning="r", confidence=1.0, changes=[_line(evidence_id, "fp")]
    )
    head = funded_book.store.head()
    decision = Record.create(
        seq=head.seq + 1,
        prev=head.id,
        kind=RecordKind.DECISION,
        recorded_at="2026-09-02T00:00:00Z",
        actor="system:policy",
        operation=None,
        data={"operation": operation.id, "verdict": "approve"},
    )
    segment = funded_book.store.segments()[-1]
    segment.write_text(segment.read_text() + decision.to_line() + "\n")
    assert funded_book.state().operations[operation.id].status == "pending"


def test_orphan_evidence_blob_is_reported(funded_book):
    digest = funded_book.store.put_evidence(b"never recorded")
    problems = verify_book(funded_book).problems
    assert [p.check for p in problems] == ["evidence"]
    assert digest in problems[0].message


def test_each_integrity_problem_is_reported_once(funded_book):
    _rewrite(funded_book, lambda lines: [line.replace('"45.20"', '"4.20"') for line in lines])
    problems = verify_book(funded_book).problems
    ids = [p for p in problems if p.check == "id"]
    assert len(ids) == len({p.record for p in ids})
    assert not any("does not match its id" in p.message for p in problems if p.check == "ledger")


def test_verify_counts_records_before_a_torn_tail(funded_book):
    segment = funded_book.store.segments()[-1]
    count = len(funded_book.store.read_records())
    segment.write_text(segment.read_text() + '{"id": "torn')
    report = verify_book(funded_book)
    assert report.records == count
    assert "truncated" in report.problems[0].message
