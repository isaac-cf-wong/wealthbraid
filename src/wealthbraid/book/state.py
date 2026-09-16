"""The projection: folding the record log into queryable book state.

:class:`BookState` is a pure function of the record sequence. It never reads
files; give it records and it rebuilds accounts, the current version of every
entry (following corrections), statement lines and their matches, prices,
reconciliations, notes, and the status of every operation. Any record that
violates an invariant, including a record whose content no longer matches its
id or whose chain link is broken, is kept in the log but excluded from the
ledger and reported as an :class:`Issue`, so a damaged book still loads and can
be diagnosed without serving tampered numbers.
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

# Changes of these kinds neither alter balances nor decide what enters the book, so policy
# may apply them without review. Statement lines are deliberately excluded: a line
# occupies a fingerprint that decides whether a bank row can ever be imported.
NON_SENSITIVE_KINDS = frozenset({RecordKind.EVIDENCE, RecordKind.NOTE})

# Payload keys whose values are record ids; only these may carry "$N" references.
REFERENCE_KEYS = frozenset({"evidence", "lines", "subjects", "target"})


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
    account_records: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    integrity_issues: list[Issue] = field(default_factory=list)

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
        previous = self.records[-1] if self.records else None
        self.records.append(record)
        before = len(self.issues)
        broken = self._integrity_problem(record, previous)
        if broken is not None:
            issue = Issue(record.id, broken)
            self.issues.append(issue)
            self.integrity_issues.append(issue)
            return self.issues[before:]
        self.by_id[record.id] = record
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

    @staticmethod
    def _integrity_problem(record: Record, previous: Record | None) -> str | None:
        expected = record.expected_id()
        if record.id != expected:
            return f"record content does not match its id (content hashes to {expected}); excluded"
        expected_seq = previous.seq + 1 if previous else 1
        if record.seq != expected_seq:
            return f"sequence number {record.seq}, expected {expected_seq}; excluded"
        expected_prev = previous.id if previous else None
        if record.prev != expected_prev:
            return f"prev is {record.prev}, expected {expected_prev}; excluded"
        return None

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
        self.account_records[record.id] = data.account

    def _apply_account_close(self, record: Record, data: AccountCloseData) -> None:
        account = self._account(data.account)
        if account.closed is not None:
            raise ValidationError(f"account already closed: {data.account}")
        self._check_close(account, data)
        account.closed = data.date
        account.close_record = record.id
        self.account_records[record.id] = data.account

    def _check_close(self, account: AccountState, data: AccountCloseData) -> None:
        if data.date < account.opened:
            raise ValidationError(f"cannot close {data.account} before it was opened ({account.opened})")
        later = [
            version.id
            for version in self.entries.values()
            if version.data.date > data.date and any(p.account == data.account for p in version.data.postings)
        ]
        if later:
            raise ValidationError(f"{data.account} has entries after {data.date}: {', '.join(sorted(later))}")

    def _apply_evidence(self, record: Record, data: EvidenceData) -> None:
        self.evidence[record.id] = data
        self.evidence_by_sha.setdefault(data.sha256, record.id)

    def _apply_line(self, record: Record, data: LineData) -> None:
        if data.evidence not in self.evidence:
            raise ValidationError(f"unknown evidence {data.evidence}")
        self._account(data.account)
        self._check_fingerprint(data.fingerprint)
        self.lines[record.id] = data
        self.line_fingerprints[data.fingerprint] = record.id

    def _check_fingerprint(self, fingerprint: str) -> None:
        if fingerprint in self.line_fingerprints:
            raise ValidationError(f"duplicate statement line (same as {self.line_fingerprints[fingerprint]})")

    def _apply_entry(self, record: Record, data: EntryData) -> None:
        transaction = self._validate_entry(record.id, data, releasing=None)
        self._link_lines(record.id, data)
        self.entries[record.id] = EntryVersion(record.id, record.id, data, transaction, [record.id])

    def _apply_correction(self, record: Record, data: CorrectionData) -> None:
        target = data.target
        if target in self.entries:
            self._correct_entry(record, data)
        elif target in self.lines:
            self._correct_line(record, data)
        elif target in self.account_records:
            self._correct_account(record, data)
        elif target in self.superseded:
            raise ValidationError(f"stale correction: {target} was already superseded by {self.superseded[target]}")
        elif target in self.voided:
            raise ValidationError(f"cannot correct {target}: voided by {self.voided[target]}")
        else:
            raise ValidationError(f"no current entry, statement line, or account record {target}")

    def _replace(self, target: str, record: Record, replacement: bool) -> None:
        if replacement:
            self.superseded[target] = record.id
        else:
            self.voided[target] = record.id

    def _correct_entry(self, record: Record, data: CorrectionData) -> None:
        current = self.entries[data.target]
        if data.replacement is None:
            del self.entries[data.target]
            self._unlink_lines(current)
            self._replace(data.target, record, replacement=False)
            return
        replacement = parse_data(RecordKind.ENTRY, data.replacement)
        transaction = self._validate_entry(record.id, replacement, releasing=current)
        del self.entries[data.target]
        self._unlink_lines(current)
        self._link_lines(record.id, replacement)
        self._replace(data.target, record, replacement=True)
        self.entries[record.id] = EntryVersion(
            record.id, current.origin, replacement, transaction, [*current.history, record.id]
        )

    def _correct_line(self, record: Record, data: CorrectionData) -> None:
        current = self.lines[data.target]
        owner = self.line_matches.get(data.target)
        if owner is not None:
            raise ValidationError(
                f"statement line {data.target} is matched by {owner}; correct or void that entry first"
            )
        replacement = None
        if data.replacement is not None:
            replacement = parse_data(RecordKind.LINE, data.replacement)
            self._require_evidence(replacement.evidence)
            self._account(replacement.account)
            if replacement.fingerprint != current.fingerprint:
                self._check_fingerprint(replacement.fingerprint)
        del self.lines[data.target]
        del self.line_fingerprints[current.fingerprint]
        self._replace(data.target, record, replacement=replacement is not None)
        if replacement is not None:
            self.lines[record.id] = replacement
            self.line_fingerprints[replacement.fingerprint] = record.id

    def _correct_account(self, record: Record, data: CorrectionData) -> None:
        name = self.account_records[data.target]
        account = self.accounts[name]
        if data.target == account.open_record:
            self._correct_open(record, data, account)
        elif data.target == account.close_record:
            self._correct_close(record, data, account)
        else:
            raise ValidationError(f"{data.target} is not the current open or close record of {name}")
        del self.account_records[data.target]

    def _postings_to(self, name: str) -> list[EntryVersion]:
        return [v for v in self.entries.values() if any(p.account == name for p in v.data.postings)]

    def _correct_open(self, record: Record, data: CorrectionData, account: AccountState) -> None:
        name = account.name
        if data.replacement is None:
            if self._postings_to(name) or any(line.account == name for line in self.lines.values()):
                raise ValidationError(f"cannot void the opening of {name}: entries or statement lines use it")
            del self.accounts[name]
            self._replace(data.target, record, replacement=False)
            return
        replacement = parse_data(RecordKind.ACCOUNT_OPEN, data.replacement)
        if replacement.account != name:
            raise ValidationError(f"an account correction must keep the same account ({name})")
        stranded = sorted(v.id for v in self._postings_to(name) if v.data.date < replacement.date)
        if stranded:
            raise ValidationError(f"{name} has entries before {replacement.date}: {', '.join(stranded)}")
        if account.closed is not None and account.closed < replacement.date:
            raise ValidationError(f"{name} was closed on {account.closed}, before {replacement.date}")
        if replacement.commodities:
            for version in self._postings_to(name):
                for posting in version.data.postings:
                    if posting.account == name and posting.commodity not in replacement.commodities:
                        raise ValidationError(f"{version.id} posts {posting.commodity} to {name}")
        account.opened = replacement.date
        account.commodities = tuple(replacement.commodities)
        account.description = replacement.description
        account.open_record = record.id
        self.account_records[record.id] = name
        self._replace(data.target, record, replacement=True)

    def _correct_close(self, record: Record, data: CorrectionData, account: AccountState) -> None:
        if data.replacement is None:
            account.closed = None
            account.close_record = None
            self._replace(data.target, record, replacement=False)
            return
        replacement = parse_data(RecordKind.ACCOUNT_CLOSE, data.replacement)
        if replacement.account != account.name:
            raise ValidationError(f"an account correction must keep the same account ({account.name})")
        self._check_close(account, replacement)
        account.closed = replacement.date
        account.close_record = record.id
        self.account_records[record.id] = account.name
        self._replace(data.target, record, replacement=True)

    def _apply_price(self, record: Record, data: PriceData) -> None:
        if Decimal(data.rate) <= 0:
            raise ValidationError("price rate must be positive")
        if data.base == data.quote:
            raise ValidationError("price base and quote must differ")
        self.prices.append((record.id, data))

    def _apply_reconciliation(self, record: Record, data: ReconciliationData) -> None:
        if not any(name == data.account or name.startswith(data.account + ":") for name in self.accounts):
            raise ValidationError(f"no account {data.account} or sub-account is open")
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
        if len(set(data.lines)) != len(data.lines):
            raise ValidationError("an entry cites the same statement line more than once")
        cited: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for line_id in data.lines:
            line = self.lines.get(line_id)
            if line is None:
                raise ValidationError(f"unknown statement line {line_id}")
            owner = self.line_matches.get(line_id)
            if owner is not None and line_id not in released:
                raise ValidationError(f"statement line {line_id} is already matched by {owner}")
            cited[(line.account, line.commodity)] += Decimal(line.amount)
        for (account, commodity), total in cited.items():
            posted = sum(
                (Decimal(p.amount) for p in data.postings if p.account == account and p.commodity == commodity),
                Decimal(0),
            )
            if posted != total:
                raise ValidationError(
                    f"entry posts {posted} {commodity} to {account} but its statement lines total {total}"
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

    def current_version(self, record_id: str) -> str | None:
        """Follow corrections from any version id to the current one.

        Args:
            record_id: An entry, statement line, account record, or correction id.

        Returns:
            The current version id, or ``None`` if the record was voided or is unknown.

        """
        seen = set()
        current = record_id
        while current in self.superseded and current not in seen:
            seen.add(current)
            current = self.superseded[current]
        return current if current in self.entries or current in self.lines or current in self.account_records else None

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


def _is_ref(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 1 and value[0] == "$" and value[1:].isdigit()


def has_refs(value: Any) -> bool:
    """Report whether a payload carries ``"$N"`` references in reference fields.

    Args:
        value: A JSON-compatible payload.

    Returns:
        ``True`` if any reference field holds a ``"$N"`` placeholder.

    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in REFERENCE_KEYS and (_is_ref(item) or (isinstance(item, list) and any(_is_ref(i) for i in item))):
                return True
            if isinstance(item, (Mapping, list)) and has_refs(item):
                return True
    elif isinstance(value, list):
        return any(has_refs(item) for item in value)
    return False


def resolve_refs(value: Any, produced: list[str]) -> Any:
    """Replace ``"$N"`` placeholders in reference fields with the id of the N-th produced record.

    Only the values of :data:`REFERENCE_KEYS` (and the items of lists held there)
    are resolved; free text such as descriptions is never rewritten.

    Args:
        value: A JSON-compatible payload.
        produced: Ids of the records produced so far, in change order.

    Returns:
        A copy of ``value`` with placeholders resolved.

    Raises:
        ValidationError: If a placeholder points at a change not yet produced.

    """

    def resolve(item: Any) -> Any:
        if not _is_ref(item):
            return item
        index = int(item[1:])
        if index >= len(produced):
            raise ValidationError(f"reference {item} points at a change that has not been produced yet")
        return produced[index]

    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if key in REFERENCE_KEYS:
                out[key] = [resolve(i) for i in item] if isinstance(item, list) else resolve(item)
            else:
                out[key] = resolve_refs(item, produced) if isinstance(item, (Mapping, list)) else item
        return out
    if isinstance(value, list):
        return [resolve_refs(item, produced) if isinstance(item, (Mapping, list)) else item for item in value]
    return value
