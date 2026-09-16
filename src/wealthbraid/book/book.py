"""The :class:`Book` facade: the only way records enter a book.

Every mutation is an *operation*. An operation records who proposed it, the
inputs and evidence it used, a reasoning summary, a confidence, and the exact
records it would add. A proposal is validated against the current book before it
is stored, so an agent learns immediately whether its changes could apply.

An operation's changes are written only after a decision:

* a human approves or rejects it (``decide``), or
* policy auto-approves it, which is allowed only when every change is
  non-sensitive (evidence, statement lines, notes) and the book allows it.

Approval re-validates against the book as it is *now*; if the book moved on and
the changes no longer apply, nothing is written. Agents can never approve.
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from wealthbraid.book.config import BookConfig, find_book, load_config
from wealthbraid.book.schema import (
    ChangeData,
    DecisionData,
    EvidenceData,
    OperationData,
    dump_data,
    format_validation_error,
    parse_data,
)
from wealthbraid.book.state import (
    HUMAN_PREFIX,
    NON_SENSITIVE_KINDS,
    POLICY_ACTOR,
    BookState,
    OperationState,
    resolve_refs,
)
from wealthbraid.errors import ConflictError, NotFoundError, PolicyError, ValidationError
from wealthbraid.store.records import Record, RecordKind
from wealthbraid.store.store import PendingAppend, RecordStore, format_timestamp, utc_now

_ACTOR_RE = re.compile(r"^(human|agent):[A-Za-z0-9._-]+$")
_DRY_RUN_APPROVER = "human:dry-run"


def check_actor(actor: str) -> str:
    """Validate a caller-supplied actor string.

    Args:
        actor: The actor, ``human:<name>`` or ``agent:<name>``.

    Returns:
        The actor unchanged.

    Raises:
        PolicyError: If the actor is malformed or claims a reserved ``system:`` identity.

    """
    if not _ACTOR_RE.match(actor):
        raise PolicyError(f"invalid actor {actor!r}; use human:<name> or agent:<name>")
    return actor


def _has_refs(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) > 1 and value[0] == "$" and value[1:].isdigit()
    if isinstance(value, list):
        return any(_has_refs(item) for item in value)
    if isinstance(value, Mapping):
        return any(_has_refs(item) for item in value.values())
    return False


class Book:
    """A wealthbraid book on disk."""

    def __init__(self, root: Path, *, clock: Callable[[], dt.datetime] = utc_now) -> None:
        """Open a book directory.

        Args:
            root: The book directory (must contain ``wealthbraid.toml``).
            clock: Source of write timestamps; injectable for deterministic tests.

        """
        self.root = Path(root)
        self.config: BookConfig = load_config(self.root)
        self.store = RecordStore(self.root, clock=clock)
        self._clock = clock

    @classmethod
    def discover(cls, explicit: Path | None = None, **kwargs: Any) -> Book:
        """Open the book found by :func:`~wealthbraid.book.config.find_book`.

        Args:
            explicit: An explicit book path, if given.
            **kwargs: Passed to :class:`Book`.

        Returns:
            The opened book.

        """
        return cls(find_book(explicit=explicit), **kwargs)

    # -- reading ------------------------------------------------------------

    def state(self, *, at: str | None = None) -> BookState:
        """Project the book's records into state.

        Args:
            at: Stop after this record id, reproducing the book as it was then.

        Returns:
            The :class:`BookState`.

        Raises:
            NotFoundError: If ``at`` is not a record in the book.

        """
        state = BookState()
        for record in self.store.iter_records():
            state.apply(record)
            if record.id == at:
                return state
        if at is not None:
            raise NotFoundError(f"record not found: {at}")
        return state

    # -- operations ---------------------------------------------------------

    def propose(  # noqa: PLR0913 - keyword-only; mirrors the operation record's fields
        self,
        *,
        actor: str,
        tool: str,
        summary: str,
        changes: Sequence[Mapping[str, Any]],
        reasoning: str,
        confidence: float,
        evidence: Sequence[str] = (),
        inputs: Mapping[str, Any] | None = None,
        approve: bool = False,
        note: str | None = None,
    ) -> OperationState:
        """Record an operation, applying it at once when allowed.

        Args:
            actor: The proposer (``human:<name>`` or ``agent:<name>``).
            tool: The operation name, e.g. ``"categorize"``.
            summary: A one-line description of the proposal.
            changes: Proposed records as ``{"kind", "data", "rationale"?}`` mappings.
            reasoning: Why these changes are proposed.
            confidence: The proposer's confidence in ``[0, 1]``.
            evidence: Evidence record ids supporting the proposal.
            inputs: The parameters and settings the operation used.
            approve: Approve immediately; only a human proposer may do this.
            note: The approval note, when ``approve`` is set.

        Returns:
            The stored operation with its resulting status.

        Raises:
            PolicyError: If a non-human asks to approve.
            ValidationError: If the proposal is malformed or its changes cannot apply.

        """
        check_actor(actor)
        if approve and not actor.startswith(HUMAN_PREFIX):
            raise PolicyError(f"{actor} cannot approve operations; only a human can")
        operation = self._build_operation(
            tool=tool,
            summary=summary,
            changes=changes,
            reasoning=reasoning,
            confidence=confidence,
            evidence=evidence,
            inputs=inputs,
        )
        with self.store.lock():
            state = self.state()
            operation_data = dump_data(operation.model_copy(update={"base": state.head.id if state.head else None}))
            sensitive = any(RecordKind(change.kind) not in NON_SENSITIVE_KINDS for change in operation.changes)
            if approve:
                decider = actor
            elif self.config.auto_apply and not sensitive:
                decider = POLICY_ACTOR
            else:
                decider = None
            timestamp = format_timestamp(self._clock())

            staged = self.store.begin(recorded_at=timestamp)
            op_record = staged.add(RecordKind.OPERATION, operation_data, actor=actor)
            self._stage_decision(
                staged,
                op_record=op_record,
                operation=operation,
                actor=decider or _DRY_RUN_APPROVER,
                verdict="approve",
                note=note,
            )
            self._check(state, staged.records, "proposal cannot be applied")

            if decider is None:
                final = self.store.begin(recorded_at=timestamp)
                final.add(RecordKind.OPERATION, operation_data, actor=actor)
                final.commit()
            else:
                staged.commit()
        return self._operation(op_record.id)

    def decide(self, operation_id: str, *, actor: str, verdict: str, note: str | None = None) -> OperationState:
        """Approve or reject a pending operation.

        Args:
            operation_id: The operation id.
            actor: The deciding human.
            verdict: ``"approve"`` or ``"reject"``.
            note: An optional note explaining the decision.

        Returns:
            The operation with its new status.

        Raises:
            PolicyError: If the actor is not a human.
            NotFoundError: If the operation does not exist.
            ConflictError: If the operation was already decided.
            ValidationError: If approved changes no longer apply to the book.

        """
        check_actor(actor)
        if not actor.startswith(HUMAN_PREFIX):
            raise PolicyError(f"{actor} cannot decide operations; only a human can")
        if verdict not in ("approve", "reject"):
            raise ValidationError(f"verdict must be 'approve' or 'reject', not {verdict!r}")
        with self.store.lock():
            state = self.state()
            operation = state.operations.get(operation_id)
            if operation is None:
                raise NotFoundError(f"operation not found: {operation_id}")
            if operation.status != "pending":
                raise ConflictError(f"operation {operation_id} is already {operation.status}")
            staged = self.store.begin()
            self._stage_decision(
                staged, op_record=operation.record, operation=operation.data, actor=actor, verdict=verdict, note=note
            )
            self._check(state, staged.records, "cannot approve: the changes no longer apply")
            staged.commit()
        return self._operation(operation_id)

    def add_evidence(
        self,
        content: bytes,
        *,
        filename: str,
        actor: str,
        source: str | None = None,
        description: str | None = None,
    ) -> tuple[str, OperationState | None]:
        """Store a source document and record it as evidence.

        Adding identical bytes again returns the existing evidence record.

        Args:
            content: The document bytes.
            filename: The original file name.
            actor: Who is adding the document.
            source: Where it came from, e.g. a bank name.
            description: A free-text description.

        Returns:
            The evidence record id and the operation that created it (``None`` if it already existed).

        Raises:
            ConflictError: If the book requires approval even for evidence.

        """
        check_actor(actor)
        digest = self.store.put_evidence(content)
        existing = self.state().evidence_by_sha.get(digest)
        if existing is not None:
            return existing, None
        data = EvidenceData(
            sha256=digest,
            filename=Path(filename).name,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            size=len(content),
            source=source,
            description=description,
        )
        operation = self.propose(
            actor=actor,
            tool="evidence.add",
            summary=f"Add evidence {data.filename}",
            changes=[{"kind": "evidence", "data": dump_data(data)}],
            reasoning="Store the source document so later records can cite it.",
            confidence=1.0,
            inputs={"filename": data.filename},
        )
        if operation.status != "applied":
            raise ConflictError(
                f"evidence operation {operation.id} awaits approval; approve it before citing the evidence"
            )
        return operation.results[0], operation

    # -- helpers ------------------------------------------------------------

    def _operation(self, operation_id: str) -> OperationState:
        return self.state().operations[operation_id]

    @staticmethod
    def _build_operation(  # noqa: PLR0913
        *,
        tool: str,
        summary: str,
        changes: Sequence[Mapping[str, Any]],
        reasoning: str,
        confidence: float,
        evidence: Sequence[str],
        inputs: Mapping[str, Any] | None,
    ) -> OperationData:
        normalised = []
        for index, raw in enumerate(changes):
            change = parse_data_change(raw, index)
            if not _has_refs(change.data):
                change = change.model_copy(update={"data": dump_data(parse_data(RecordKind(change.kind), change.data))})
            normalised.append(change)
        try:
            return OperationData(
                tool=tool,
                summary=summary,
                inputs=dict(inputs or {}),
                evidence=list(evidence),
                reasoning=reasoning,
                confidence=confidence,
                changes=normalised,
            )
        except PydanticValidationError as exc:
            raise ValidationError(f"invalid operation: {format_validation_error(exc)}") from exc

    @staticmethod
    def _stage_decision(  # noqa: PLR0913
        staged: PendingAppend,
        *,
        op_record: Record,
        operation: OperationData,
        actor: str,
        verdict: str,
        note: str | None,
    ) -> None:
        decision = DecisionData(operation=op_record.id, verdict=verdict, note=note)  # type: ignore[arg-type]
        staged.add(RecordKind.DECISION, dump_data(decision), actor=actor)
        if verdict != "approve":
            return
        produced: list[str] = []
        for change in operation.changes:
            data = resolve_refs(change.data, produced)
            record = staged.add(RecordKind(change.kind), data, actor=op_record.actor, operation=op_record.id)
            produced.append(record.id)
        staged.add(RecordKind.APPLIED, {"operation": op_record.id, "results": produced}, actor=actor)

    @staticmethod
    def _check(state: BookState, records: Sequence[Record], message: str) -> None:
        problems = []
        for record in records:
            problems.extend(state.apply(record))
        if problems:
            details = "; ".join(issue.message for issue in problems)
            raise ValidationError(f"{message}: {details}")


def parse_data_change(raw: Mapping[str, Any], index: int) -> ChangeData:
    """Parse one proposed change.

    Args:
        raw: The change mapping.
        index: Its position, for error messages.

    Returns:
        The parsed :class:`ChangeData`.

    Raises:
        ValidationError: If the change is malformed.

    """
    try:
        return ChangeData.model_validate(raw)
    except PydanticValidationError as exc:
        raise ValidationError(f"change {index}: {format_validation_error(exc)}") from exc
