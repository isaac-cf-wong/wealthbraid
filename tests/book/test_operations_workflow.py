"""Tests for the propose → decide → apply workflow and the approval policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import AGENT, HUMAN, StepClock, entry, open_accounts

from wealthbraid.book.book import Book
from wealthbraid.book.state import POLICY_ACTOR
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import ConflictError, PolicyError, ValidationError
from wealthbraid.store.records import RecordKind

EUR = Commodity("EUR")


def _propose_entry(book: Book, actor: str = AGENT, **kwargs):
    data = kwargs.pop("data", entry("2026-02-10", ("Expenses:Food", "12.00"), ("Assets:Bank:Checking", "-12.00")))
    return book.propose(
        actor=actor,
        tool="entry.add",
        summary="lunch",
        changes=[{"kind": "entry", "data": data, "rationale": "receipt"}],
        reasoning="Card payment at a cafe.",
        confidence=0.7,
        **kwargs,
    )


def _checking(book: Book) -> Decimal:
    return book.state().ledger().balance("Assets:Bank:Checking").get(EUR)


def test_agent_proposal_waits_for_human_approval(funded_book):
    """A sensitive agent proposal is stored but does not touch balances until approved."""
    before = _checking(funded_book)
    operation = _propose_entry(funded_book)
    assert operation.status == "pending"
    assert operation.data.confidence == 0.7
    assert operation.record.actor == AGENT
    assert _checking(funded_book) == before

    decided = funded_book.decide(operation.id, actor=HUMAN, verdict="approve", note="looks right")
    assert decided.status == "applied"
    assert _checking(funded_book) == before - Decimal("12.00")
    (result_id,) = decided.results
    result = funded_book.state().by_id[result_id]
    assert result.kind is RecordKind.ENTRY
    assert result.operation == operation.id
    assert result.actor == AGENT
    assert decided.decision.actor == HUMAN
    assert decided.decision.data["note"] == "looks right"


def test_proposal_records_base_head(funded_book):
    """The operation remembers which head it was proposed against."""
    head = funded_book.state().head.id
    assert _propose_entry(funded_book).data.base == head


def test_staleness_counts_only_content_changes(funded_book):
    """Other proposals do not make an operation stale; applied changes do."""
    operation = _propose_entry(funded_book)
    other = _propose_entry(funded_book)
    assert funded_book.state().changed_since(operation) == []
    funded_book.decide(other.id, actor=HUMAN, verdict="reject")
    assert funded_book.state().changed_since(operation) == []
    applied = funded_book.decide(_propose_entry(funded_book).id, actor=HUMAN, verdict="approve")
    assert funded_book.state().changed_since(operation) == applied.results


@pytest.mark.parametrize("actor", [AGENT, "system:policy", "alice", "human:"])
def test_non_humans_cannot_approve(funded_book, actor):
    """Agents, reserved system actors, and malformed actors cannot approve."""
    operation = _propose_entry(funded_book)
    with pytest.raises(PolicyError):
        funded_book.decide(operation.id, actor=actor, verdict="approve")
    assert funded_book.state().operations[operation.id].status == "pending"


def test_agent_cannot_self_approve_at_proposal(funded_book):
    """Asking to approve on proposal is a policy error for agents and writes nothing."""
    count = len(funded_book.store.read_records())
    with pytest.raises(PolicyError):
        _propose_entry(funded_book, approve=True)
    assert len(funded_book.store.read_records()) == count


def test_non_sensitive_changes_are_auto_applied_by_policy(funded_book):
    """Notes do not change balances, so policy applies them and says so."""
    target = funded_book.state().sorted_entries()[0].id
    operation = funded_book.propose(
        actor=AGENT,
        tool="explain",
        summary="explain salary",
        changes=[{"kind": "note", "data": {"subjects": [target], "text": "Monthly salary.", "confidence": 0.9}}],
        reasoning="Payee is the employer.",
        confidence=0.9,
    )
    assert operation.status == "applied"
    assert operation.decision.actor == POLICY_ACTOR
    assert funded_book.state().notes[target] == operation.results


def test_auto_apply_can_be_disabled(book_root):
    """With auto-apply off, even notes wait for a human."""
    config = book_root / "wealthbraid.toml"
    config.write_text(config.read_text().replace("auto_apply_non_sensitive = true", "auto_apply_non_sensitive = false"))
    book = Book(book_root, clock=StepClock())
    open_accounts(book, "Assets:Cash")
    subject = book.state().head.id
    operation = book.propose(
        actor=AGENT,
        tool="explain",
        summary="s",
        changes=[{"kind": "note", "data": {"subjects": [subject], "text": "t"}}],
        reasoning="r",
        confidence=0.5,
    )
    assert operation.status == "pending"


def test_policy_never_auto_applies_mixed_operations(funded_book):
    """One sensitive change makes the whole operation need approval."""
    target = funded_book.state().sorted_entries()[0].id
    operation = funded_book.propose(
        actor=AGENT,
        tool="mixed",
        summary="s",
        changes=[
            {"kind": "note", "data": {"subjects": [target], "text": "t"}},
            {"kind": "entry", "data": entry("2026-02-11", ("Expenses:Food", "1"), ("Assets:Bank:Checking", "-1"))},
        ],
        reasoning="r",
        confidence=0.5,
    )
    assert operation.status == "pending"


def test_invalid_proposal_is_refused_and_writes_nothing(funded_book):
    """Unbalanced or ill-typed changes fail at proposal time."""
    count = len(funded_book.store.read_records())
    with pytest.raises(ValidationError, match="does not balance"):
        _propose_entry(
            funded_book, data=entry("2026-02-10", ("Expenses:Food", "12.00"), ("Assets:Bank:Checking", "-11.00"))
        )
    with pytest.raises(ValidationError, match="floats are not accepted"):
        _propose_entry(funded_book, data=entry("2026-02-10", ("Expenses:Food", 12.0), ("Assets:Bank:Checking", "-12")))
    with pytest.raises(ValidationError, match="account not opened"):
        _propose_entry(funded_book, data=entry("2026-02-10", ("Expenses:Travel", "1"), ("Assets:Bank:Checking", "-1")))
    with pytest.raises(ValidationError, match="confidence"):
        funded_book.propose(
            actor=AGENT,
            tool="t",
            summary="s",
            reasoning="r",
            confidence=1.5,
            changes=[
                {"kind": "entry", "data": entry("2026-02-10", ("Expenses:Food", "1"), ("Assets:Bank:Checking", "-1"))}
            ],
        )
    assert len(funded_book.store.read_records()) == count


def test_rejection_records_decision_only(funded_book):
    """A rejected operation keeps its proposal and decision but produces no records."""
    operation = _propose_entry(funded_book)
    rejected = funded_book.decide(operation.id, actor=HUMAN, verdict="reject", note="duplicate")
    assert rejected.status == "rejected"
    assert rejected.results == []
    with pytest.raises(ConflictError):
        funded_book.decide(operation.id, actor=HUMAN, verdict="approve")


def test_approval_revalidates_against_current_book(funded_book):
    """Two proposals voiding the same entry: after the first is applied, the second cannot be."""
    target = funded_book.state().sorted_entries()[1].id
    void = {"kind": "correction", "data": {"target": target, "reason": "duplicate charge"}}
    first = funded_book.propose(actor=AGENT, tool="c", summary="s", changes=[void], reasoning="r", confidence=0.8)
    second = funded_book.propose(actor=AGENT, tool="c", summary="s", changes=[void], reasoning="r", confidence=0.8)
    funded_book.decide(first.id, actor=HUMAN, verdict="approve")
    count = len(funded_book.store.read_records())
    with pytest.raises(ValidationError, match="no longer apply"):
        funded_book.decide(second.id, actor=HUMAN, verdict="approve")
    assert len(funded_book.store.read_records()) == count
    assert funded_book.state().operations[second.id].status == "pending"


def test_change_references_resolve_to_produced_ids(funded_book):
    """ "$0" in a later change becomes the id of the record produced by change 0."""
    digest = funded_book.store.put_evidence(b"receipt")
    operation = funded_book.propose(
        actor=AGENT,
        tool="receipt",
        summary="s",
        changes=[
            {"kind": "evidence", "data": {"sha256": digest, "filename": "r.txt", "size": 7}},
            {"kind": "note", "data": {"subjects": ["$0"], "text": "Receipt for lunch."}},
        ],
        reasoning="r",
        confidence=1.0,
    )
    evidence_id, note_id = operation.results
    state = funded_book.state()
    assert evidence_id.startswith("evd_")
    assert state.by_id[note_id].data["subjects"] == [evidence_id]


def test_add_evidence_is_idempotent(book):
    """The same document added twice yields one evidence record."""
    first, operation = book.add_evidence(b"a,b\n1,2\n", filename="s.csv", actor=AGENT, source="Bank")
    again, none = book.add_evidence(b"a,b\n1,2\n", filename="other.csv", actor=AGENT)
    assert first == again
    assert operation.status == "applied"
    assert none is None
    assert book.state().evidence[first].media_type == "text/csv"


def test_state_at_reproduces_history(funded_book):
    """Projecting up to an earlier record reproduces the balances seen then."""
    head_before = funded_book.state().head.id
    balance_before = _checking(funded_book)
    operation = _propose_entry(funded_book, actor=HUMAN, approve=True)
    assert operation.status == "applied"
    assert funded_book.state(at=head_before).ledger().balance("Assets:Bank:Checking").get(EUR) == balance_before
