"""The projection: folding the record log into queryable book state.

:class:`BookState` is a pure function of the record sequence. It never reads
files; give it records and it rebuilds accounts, the current version of every
entry (following corrections), statement lines and their matches, prices,
reconciliations, notes, and the status of every operation. Any record that
violates an invariant is kept in the log but excluded from the ledger and
reported as an :class:`Issue`, so a damaged book still loads and can be
diagnosed.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wealthbraid.book.schema import (
    AccountCloseData,
    AccountOpenData,
    AppliedData,
    CorrectionData,
    DecisionData,
    EntryData,
    EvidenceData,
    LineData,
    NoteData,
    OperationData,
    PriceData,
    ReconciliationData,
    parse_data,
)
from wealthbraid.engine.account import Account
from wealthbraid.engine.balancing import balance_transaction
from wealthbraid.engine.errors import EngineError
from wealthbraid.engine.ledger import Ledger
from wealthbraid.engine.money import Amount, Commodity
from wealthbraid.engine.prices import Price
from wealthbraid.engine.transaction import Posting, Transaction
from wealthbraid.errors import ValidationError
from wealthbraid.store.records import Record, RecordKind, canonical_json

HUMAN_PREFIX = "human:"
POLICY_ACTOR = "system:policy"

# Changes of these kinds never alter balances, so policy may apply them without review.
NON_SENSITIVE_KINDS = frozenset({RecordKind.EVIDENCE, RecordKind.LINE, RecordKind.NOTE})


@dataclass(frozen=True)
class Issue:
    """An invariant violation attributed to a record.

    Attributes:
        record: The offending record id.
        message: What is wrong.

    """

    record: str
    message: str


@dataclass
class AccountState:
    """The lifecycle of one account."""

    name: str
    opened: dt.date
    open_record: str
    commodities: tuple[str, ...] = ()
    description: str | None = None
    closed: dt.date | None = None
    close_record: str | None = None


@dataclass
class EntryVersion:
    """The current version of an entry after applying corrections.

    Attributes:
        id: The record id providing this version (an entry or correction).
        origin: The id of the original entry record.
        data: The entry payload.
        transaction: The balanced engine transaction.
        history: Every version id from the original entry to this one.

    """

    id: str
    origin: str
    data: EntryData
    transaction: Transaction
    history: list[str]


@dataclass
class OperationState:
    """An operation and its derived approval status."""

    record: Record
    data: OperationData
    decision: Record | None = None
    applied: Record | None = None

    @property
    def id(self) -> str:
        """Return the operation id.

        Returns:
            The operation record id.

        """
        return self.record.id

    @property
    def status(self) -> str:
        """Return ``pending``, ``rejected``, ``approved`` (not yet applied), or ``applied``.

        Returns:
            The status slug.

        """
        if self.decision is None:
            return "pending"
        if self.decision.data.get("verdict") == "reject":
            return "rejected"
        return "applied" if self.applied is not None else "approved"

    @property
    def results(self) -> list[str]:
        """Return the ids of records produced by applying the operation.

        Returns:
            The result ids (empty unless applied).

        """
        return list(self.applied.data.get("results", [])) if self.applied else []

    @property
    def sensitive(self) -> bool:
        """Report whether any proposed change needs explicit approval.

        Returns:
            ``True`` if a change could alter balances or accounts.

        """
        return any(RecordKind(change.kind) not in NON_SENSITIVE_KINDS for change in self.data.changes)


@dataclass
class BookState:
    """Everything derivable from a record log."""

    records: list[Record] = field(default_factory=list)
    by_id: dict[str, Record] = field(default_factory=dict)
    accounts: dict[str, AccountState] = field(default_factory=dict)
    evidence: dict[str, EvidenceData] = field(default_factory=dict)
    evidence_by_sha: dict[str, str] = field(default_factory=dict)
    lines: dict[str, LineData] = field(default_factory=dict)
    line_fingerprints: dict[str, str] = field(default_factory=dict)
    line_matches: dict[str, str] = field(default_factory=dict)
    entries: dict[str, EntryVersion] = field(default_factory=dict)
    superseded: dict[str, str] = field(default_factory=dict)
    voided: dict[str, str] = field(default_factory=dict)
    prices: list[tuple[str, PriceData]] = field(default_factory=list)
    reconciliations: list[tuple[str, ReconciliationData]] = field(default_factory=list)
    notes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    operations: dict[str, OperationState] = field(default_factory=dict)
    produced: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    issues: list[Issue] = field(default_factory=list)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_records(cls, records: Iterable[Record]) -> BookState:
        """Fold records, in order, into a new state.

        Args:
            records: The record log in chain order.

        Returns:
            The resulting :class:`BookState`.

        """
        state = cls()
        for record in records:
            state.apply(record)
        return state

    @property
    def head(self) -> Record | None:
        """Return the last record folded in.

        Returns:
            The head record, or ``None`` for an empty book.

        """
        return self.records[-1] if self.records else None

    def apply(self, record: Record) -> list[Issue]:
        """Fold one record into the state.

        Args:
            record: The next record in chain order.

        Returns:
            The issues this record raised (also appended to :attr:`issues`).

        """
        self.records.append(record)
        self.by_id[record.id] = record
        before = len(self.issues)
        try:
            data = parse_data(record.kind, record.data)
            self._check_provenance(record)
            if record.operation is not None:
                self.produced[record.operation].append(record.id)
            handler = getattr(self, f"_apply_{record.kind.name.lower()}")
            handler(record, data)
        except (ValidationError, EngineError) as exc:
            self.issues.append(Issue(record.id, str(exc)))
        return self.issues[before:]

    # -- provenance ---------------------------------------------------------

    def _check_provenance(self, record: Record) -> None:
        if record.kind in (RecordKind.OPERATION, RecordKind.DECISION, RecordKind.APPLIED):
            return
        if record.operation is None:
            raise ValidationError("record has no originating operation")
        operation = self.operations.get(record.operation)
        if operation is None:
            raise ValidationError(f"unknown operation {record.operation}")
        if operation.decision is None or operation.decision.data.get("verdict") != "approve":
            raise ValidationError(f"operation {record.operation} was not approved")
        if operation.applied is not None:
            raise ValidationError(f"operation {record.operation} was already applied")

    # -- handlers -----------------------------------------------------------

    def _apply_account_open(self, record: Record, data: AccountOpenData) -> None:
        if data.account in self.accounts:
            raise ValidationError(f"account already opened: {data.account}")
        self.accounts[data.account] = AccountState(
            name=data.account,
            opened=data.date,
            open_record=record.id,
            commodities=tuple(data.commodities),
            description=data.description,
        )

    def _apply_account_close(self, record: Record, data: AccountCloseData) -> None:
        account = self._account(data.account)
        if account.closed is not None:
            raise ValidationError(f"account already closed: {data.account}")
        if data.date < account.opened:
            raise ValidationError(f"cannot close {data.account} before it was opened ({account.opened})")
        later = [
            version.id
            for version in self.entries.values()
            if version.data.date > data.date and any(p.account == data.account for p in version.data.postings)
        ]
        if later:
            raise ValidationError(f"{data.account} has entries after {data.date}: {', '.join(sorted(later))}")
        account.closed = data.date
        account.close_record = record.id

    def _apply_evidence(self, record: Record, data: EvidenceData) -> None:
        self.evidence[record.id] = data
        self.evidence_by_sha.setdefault(data.sha256, record.id)

    def _apply_line(self, record: Record, data: LineData) -> None:
        if data.evidence not in self.evidence:
            raise ValidationError(f"unknown evidence {data.evidence}")
        self._account(data.account)
        if data.fingerprint in self.line_fingerprints:
            raise ValidationError(f"duplicate statement line (same as {self.line_fingerprints[data.fingerprint]})")
        self.lines[record.id] = data
        self.line_fingerprints[data.fingerprint] = record.id

    def _apply_entry(self, record: Record, data: EntryData) -> None:
        transaction = self._validate_entry(record.id, data, releasing=None)
        self._link_lines(record.id, data)
        self.entries[record.id] = EntryVersion(record.id, record.id, data, transaction, [record.id])

    def _apply_correction(self, record: Record, data: CorrectionData) -> None:
        current = self.entries.get(data.target)
        if current is None:
            if data.target in self.superseded:
                raise ValidationError(
                    f"stale correction: {data.target} was already superseded by {self.superseded[data.target]}"
                )
            if data.target in self.voided:
                raise ValidationError(f"cannot correct {data.target}: voided by {self.voided[data.target]}")
            raise ValidationError(f"unknown entry {data.target}")
        if data.replacement is None:
            del self.entries[data.target]
            self._unlink_lines(current)
            self.voided[data.target] = record.id
            return
        transaction = self._validate_entry(record.id, data.replacement, releasing=current)
        del self.entries[data.target]
        self._unlink_lines(current)
        self._link_lines(record.id, data.replacement)
        self.superseded[data.target] = record.id
        self.entries[record.id] = EntryVersion(
            record.id, current.origin, data.replacement, transaction, [*current.history, record.id]
        )

    def _apply_price(self, record: Record, data: PriceData) -> None:
        if Decimal(data.rate) <= 0:
            raise ValidationError("price rate must be positive")
        if data.base == data.quote:
            raise ValidationError("price base and quote must differ")
        self.prices.append((record.id, data))

    def _apply_reconciliation(self, record: Record, data: ReconciliationData) -> None:
        self._account(data.account)
        for ref in data.evidence:
            self._require_evidence(ref)
        self.reconciliations.append((record.id, data))

    def _apply_note(self, record: Record, data: NoteData) -> None:
        for subject in data.subjects:
            if subject not in self.by_id:
                raise ValidationError(f"note subject not found: {subject}")
        for subject in data.subjects:
            self.notes[subject].append(record.id)

    def _apply_operation(self, record: Record, data: OperationData) -> None:
        for ref in data.evidence:
            self._require_evidence(ref)
        self.operations[record.id] = OperationState(record, data)

    def _apply_decision(self, record: Record, data: DecisionData) -> None:
        operation = self.operations.get(data.operation)
        if operation is None:
            raise ValidationError(f"unknown operation {data.operation}")
        if operation.decision is not None:
            raise ValidationError(f"operation {data.operation} was already decided by {operation.decision.id}")
        if record.actor == POLICY_ACTOR:
            if data.verdict != "approve" or operation.sensitive:
                raise ValidationError("policy may only auto-approve operations without sensitive changes")
        elif not record.actor.startswith(HUMAN_PREFIX):
            raise ValidationError(f"only a human may decide an operation, not {record.actor}")
        operation.decision = record

    def _apply_applied(self, record: Record, data: AppliedData) -> None:
        operation = self.operations.get(data.operation)
        if operation is None:
            raise ValidationError(f"unknown operation {data.operation}")
        if operation.status != "approved":
            raise ValidationError(f"operation {data.operation} is {operation.status}, not approved")
        produced = self.produced.get(data.operation, [])
        if produced != data.results:
            raise ValidationError(f"applied results {data.results} do not match produced records {produced}")
        if len(produced) != len(operation.data.changes):
            raise ValidationError(
                f"operation proposed {len(operation.data.changes)} changes but produced {len(produced)}"
            )
        for index, (change, result_id) in enumerate(zip(operation.data.changes, produced, strict=True)):
            result = self.by_id[result_id]
            expected = resolve_refs(change.data, produced[:index])
            if change.kind != result.kind.value or canonical_json(expected) != canonical_json(result.data):
                raise ValidationError(f"record {result_id} does not match proposed change {index}")
        operation.applied = record

    # -- validation helpers -------------------------------------------------

    def _account(self, name: str) -> AccountState:
        try:
            return self.accounts[name]
        except KeyError as exc:
            raise ValidationError(f"account not opened: {name}") from exc

    def _require_evidence(self, ref: str) -> None:
        if ref not in self.evidence:
            raise ValidationError(f"unknown evidence {ref}")

    def _validate_entry(self, record_id: str, data: EntryData, *, releasing: EntryVersion | None) -> Transaction:
        for posting in data.postings:
            account = self._account(posting.account)
            if data.date < account.opened:
                raise ValidationError(f"{posting.account} is not open on {data.date} (opened {account.opened})")
            if account.closed is not None and data.date > account.closed:
                raise ValidationError(f"{posting.account} was closed on {account.closed}")
            if account.commodities and posting.commodity not in account.commodities:
                raise ValidationError(f"{posting.account} does not accept {posting.commodity}")
        for ref in data.evidence:
            self._require_evidence(ref)
        released = set(releasing.data.lines) if releasing else set()
        for line_id in data.lines:
            line = self.lines.get(line_id)
            if line is None:
                raise ValidationError(f"unknown statement line {line_id}")
            owner = self.line_matches.get(line_id)
            if owner is not None and line_id not in released:
                raise ValidationError(f"statement line {line_id} is already matched by {owner}")
            if not any(
                p.account == line.account
                and p.commodity == line.commodity
                and Decimal(p.amount) == Decimal(line.amount)
                for p in data.postings
            ):
                raise ValidationError(
                    f"entry has no posting of {line.amount} {line.commodity} to {line.account} "
                    f"matching statement line {line_id}"
                )
        transaction = to_transaction(record_id, data)
        return balance_transaction(transaction)

    def _link_lines(self, entry_id: str, data: EntryData) -> None:
        for line_id in data.lines:
            self.line_matches[line_id] = entry_id

    def _unlink_lines(self, version: EntryVersion) -> None:
        for line_id in version.data.lines:
            if self.line_matches.get(line_id) == version.id:
                del self.line_matches[line_id]

    # -- queries ------------------------------------------------------------

    def current_version(self, entry_id: str) -> str | None:
        """Follow corrections from any version id to the current one.

        Args:
            entry_id: An entry or correction id.

        Returns:
            The current version id, or ``None`` if the entry was voided or is unknown.

        """
        seen = set()
        current = entry_id
        while current in self.superseded and current not in seen:
            seen.add(current)
            current = self.superseded[current]
        return current if current in self.entries else None

    def unmatched_lines(self) -> list[str]:
        """Return statement line ids not accounted for by any current entry.

        Returns:
            Line ids sorted by date then id.

        """
        pending = [line_id for line_id in self.lines if line_id not in self.line_matches]
        return sorted(pending, key=lambda line_id: (self.lines[line_id].date, line_id))

    def changed_since(self, operation: OperationState) -> list[str]:
        """Return ids of records that changed the book after an operation was proposed.

        Proposals, decisions, and applied markers do not change balances by
        themselves; the records an applied operation produced do.

        Args:
            operation: The operation.

        Returns:
            Ids of later content records (empty if the book is as it was).

        """
        workflow = (RecordKind.OPERATION, RecordKind.DECISION, RecordKind.APPLIED)
        return [r.id for r in self.records[operation.record.seq :] if r.kind not in workflow]

    def pending_line_proposals(self) -> dict[str, list[str]]:
        """Map statement line ids to the pending operations proposing entries for them.

        Returns:
            Line id to the ids of pending operations whose entry changes cite it.

        """
        claims: dict[str, list[str]] = defaultdict(list)
        for operation in self.operations.values():
            if operation.status != "pending":
                continue
            for change in operation.data.changes:
                if change.kind in ("entry", "correction"):
                    payload = change.data.get("replacement") if change.kind == "correction" else change.data
                    for line_id in (payload or {}).get("lines", []):
                        if operation.id not in claims[line_id]:
                            claims[line_id].append(operation.id)
        return dict(claims)

    def sorted_entries(self) -> list[EntryVersion]:
        """Return current entry versions in date order (ties by log order).

        Returns:
            The entry versions.

        """
        order = {record.id: index for index, record in enumerate(self.records)}
        return sorted(self.entries.values(), key=lambda v: (v.data.date, order[v.origin]))

    def ledger(self, *, start: dt.date | None = None, end: dt.date | None = None) -> Ledger:
        """Build an engine ledger from current entries within a date range.

        Args:
            start: Include entries on or after this date, if given.
            end: Include entries on or before this date, if given.

        Returns:
            A populated :class:`~wealthbraid.engine.ledger.Ledger` with every
            account declared and every price recorded.

        """
        ledger = Ledger()
        for name in sorted(self.accounts):
            ledger.declare_account(Account(name))
        for version in self.sorted_entries():
            if start is not None and version.data.date < start:
                continue
            if end is not None and version.data.date > end:
                continue
            ledger.add_transaction(version.transaction)
        for _, price in self.prices:
            ledger.add_price(Price(price.date, Commodity(price.base), Amount.of(price.rate, price.quote)))
        return ledger

    def issues_for(self, record_ids: Iterable[str]) -> list[Issue]:
        """Return the issues raised by specific records.

        Args:
            record_ids: The record ids of interest.

        Returns:
            The matching issues.

        """
        wanted = set(record_ids)
        return [issue for issue in self.issues if issue.record in wanted]


def to_transaction(record_id: str, data: EntryData) -> Transaction:
    """Convert an entry payload to an engine transaction.

    Args:
        record_id: The version id to carry as the transaction id.
        data: The entry payload.

    Returns:
        The unbalanced engine :class:`Transaction`.

    """
    return Transaction(
        date=data.date,
        postings=tuple(Posting(p.account, Amount.of(p.amount, p.commodity)) for p in data.postings),
        payee=data.payee,
        description=data.narration,
        tags=frozenset(data.tags),
        metadata=dict(data.metadata),
        id=record_id,
    )


def resolve_refs(value: Any, produced: list[str]) -> Any:
    """Replace ``"$N"`` placeholders with the id of the N-th produced record.

    Args:
        value: A JSON-compatible value.
        produced: Ids of the records produced so far, in change order.

    Returns:
        A copy of ``value`` with placeholders resolved.

    Raises:
        ValidationError: If a placeholder points at a change not yet produced.

    """
    if isinstance(value, str) and len(value) > 1 and value[0] == "$" and value[1:].isdigit():
        index = int(value[1:])
        if index >= len(produced):
            raise ValidationError(f"reference {value} points at a change that has not been produced yet")
        return produced[index]
    if isinstance(value, list):
        return [resolve_refs(item, produced) for item in value]
    if isinstance(value, Mapping):
        return {key: resolve_refs(item, produced) for key, item in value.items()}
    return value
