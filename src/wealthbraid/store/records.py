"""Record envelopes, canonical JSON, and content-derived identifiers.

Every fact in a book is a :class:`Record`: an immutable envelope carrying a
kind, the actor who caused it, the operation it belongs to (if any), and a
kind-specific ``data`` payload. Records form a single hash chain: each record's
``prev`` is the identifier of the record before it, and its ``id`` is derived
from the SHA-256 digest of its canonical JSON form (everything except ``id``).
Rewriting or reordering any earlier record therefore changes every later
identifier, which :func:`wealthbraid.book.verify.verify_book` detects.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

SCHEMA_VERSION = 1

# 128 bits of the digest: collision-safe for any plausible personal ledger while
# keeping identifiers short enough to type.
_ID_HEX_LENGTH = 32


class RecordKind(enum.Enum):
    """The kinds of record a book can contain.

    The enum value is the stable string stored on disk.
    """

    ACCOUNT_OPEN = "account.open"
    ACCOUNT_CLOSE = "account.close"
    EVIDENCE = "evidence"
    LINE = "line"
    ENTRY = "entry"
    CORRECTION = "correction"
    PRICE = "price"
    RECONCILIATION = "reconciliation"
    NOTE = "note"
    OPERATION = "operation"
    DECISION = "decision"
    APPLIED = "applied"

    @property
    def prefix(self) -> str:
        """Return the identifier prefix for this kind.

        Returns:
            A three-letter prefix such as ``"ent"``.

        """
        return _PREFIXES[self]


_PREFIXES: dict[RecordKind, str] = {
    RecordKind.ACCOUNT_OPEN: "acc",
    RecordKind.ACCOUNT_CLOSE: "acx",
    RecordKind.EVIDENCE: "evd",
    RecordKind.LINE: "lin",
    RecordKind.ENTRY: "ent",
    RecordKind.CORRECTION: "cor",
    RecordKind.PRICE: "prc",
    RecordKind.RECONCILIATION: "rec",
    RecordKind.NOTE: "not",
    RecordKind.OPERATION: "opr",
    RecordKind.DECISION: "dec",
    RecordKind.APPLIED: "app",
}


def canonical_json(value: Any) -> str:
    """Serialise a JSON-compatible value canonically.

    Keys are sorted, separators carry no whitespace, and non-ASCII text is kept
    verbatim, so equal values always produce byte-identical output.

    Args:
        value: A JSON-compatible value (no floats should appear in records).

    Returns:
        The canonical JSON text.

    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_id(kind: RecordKind, body: Mapping[str, Any]) -> str:
    """Derive a record identifier from its kind and envelope body.

    Args:
        kind: The record kind, which supplies the prefix.
        body: The envelope without its ``id`` field.

    Returns:
        An identifier such as ``"ent_3f2a…"``.

    """
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return f"{kind.prefix}_{digest[:_ID_HEX_LENGTH]}"


@dataclass(frozen=True)
class Record:
    """An immutable, content-addressed record in a book's log.

    Attributes:
        id: The content-derived identifier.
        seq: The 1-based position of the record in the log.
        prev: The identifier of the previous record, or ``None`` for the first.
        kind: The record kind.
        recorded_at: UTC timestamp (ISO-8601, seconds precision) of the write.
        actor: Who caused the record, e.g. ``"human:alice"`` or ``"agent:claude"``.
        operation: The operation this record resulted from, if any.
        data: The kind-specific payload.
        v: The record schema version.

    """

    id: str
    seq: int
    prev: str | None
    kind: RecordKind
    recorded_at: str
    actor: str
    operation: str | None
    data: Mapping[str, Any] = field(default_factory=dict)
    v: int = SCHEMA_VERSION

    def body(self) -> dict[str, Any]:
        """Return the hashed envelope: every field except ``id``.

        Returns:
            A JSON-compatible dictionary.

        """
        return {
            "v": self.v,
            "seq": self.seq,
            "prev": self.prev,
            "kind": self.kind.value,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "operation": self.operation,
            "data": self.data,
        }

    def to_json(self) -> dict[str, Any]:
        """Return the full on-disk representation, ``id`` first.

        Returns:
            A JSON-compatible dictionary.

        """
        return {"id": self.id, **self.body()}

    def to_line(self) -> str:
        """Serialise the record as one canonical JSON line (without newline).

        Returns:
            The canonical JSON text.

        """
        return canonical_json(self.to_json())

    def expected_id(self) -> str:
        """Recompute the identifier this record's content implies.

        Returns:
            The identifier derived from :meth:`body`.

        """
        return compute_id(self.kind, self.body())

    @classmethod
    def create(  # noqa: PLR0913 - keyword-only envelope fields
        cls,
        *,
        seq: int,
        prev: str | None,
        kind: RecordKind,
        recorded_at: str,
        actor: str,
        operation: str | None,
        data: Mapping[str, Any],
    ) -> Record:
        """Build a record and derive its identifier.

        Args:
            seq: The log position.
            prev: The previous record's identifier.
            kind: The record kind.
            recorded_at: The UTC write timestamp.
            actor: The actor string.
            operation: The originating operation, if any.
            data: The kind-specific payload.

        Returns:
            The new :class:`Record`.

        """
        draft = cls(
            id="",
            seq=seq,
            prev=prev,
            kind=kind,
            recorded_at=recorded_at,
            actor=actor,
            operation=operation,
            data=json.loads(canonical_json(data)),
        )
        return replace(draft, id=draft.expected_id())

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Record:
        """Parse a record from its on-disk representation.

        Args:
            raw: The decoded JSON object.

        Returns:
            The parsed :class:`Record` (its identifier is not verified here).

        Raises:
            ValueError: If required fields are missing or the kind is unknown.

        """
        try:
            return cls(
                id=str(raw["id"]),
                seq=int(raw["seq"]),
                prev=raw["prev"],
                kind=RecordKind(raw["kind"]),
                recorded_at=str(raw["recorded_at"]),
                actor=str(raw["actor"]),
                operation=raw.get("operation"),
                data=raw.get("data", {}),
                v=int(raw.get("v", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Malformed record: missing or invalid field {exc}") from exc
