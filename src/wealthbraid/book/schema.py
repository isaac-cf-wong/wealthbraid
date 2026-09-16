"""Typed payload schemas for every record kind and for operation proposals.

The schemas validate the ``data`` of each record at the boundary: when an agent
or a human proposes changes, and again when a book is loaded. Money is always a
decimal *string* (``"-12.50"``); floats are rejected so rounding error cannot
enter the ledger. :func:`json_schema` exposes the proposal format to agents.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from wealthbraid.engine.account import parse_account_name
from wealthbraid.engine.errors import EngineError
from wealthbraid.engine.money import Commodity
from wealthbraid.errors import UsageError, ValidationError
from wealthbraid.store.records import RecordKind


def _decimal_string(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('amounts must be decimal strings, e.g. "-12.50" (floats are not accepted)')  # noqa: TRY004
    try:
        quantity = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal number: {value!r}") from exc
    if not quantity.is_finite():
        raise ValueError(f"amount must be finite: {value!r}")
    return str(quantity)


def _account_name(value: str) -> str:
    try:
        parse_account_name(value)
    except EngineError as exc:
        raise ValueError(str(exc)) from exc
    return value


def _commodity_code(value: str) -> str:
    try:
        Commodity(value)
    except EngineError as exc:
        raise ValueError(str(exc)) from exc
    return value


DecimalStr = Annotated[str, BeforeValidator(_decimal_string)]
AccountName = Annotated[str, AfterValidator(_account_name)]
CommodityCode = Annotated[str, AfterValidator(_commodity_code)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountOpenData(_Model):
    """Declare an account from a date onwards."""

    account: AccountName
    date: dt.date
    commodities: list[CommodityCode] = Field(default_factory=list, description="Allowed commodities; empty = any.")
    description: str | None = None


class AccountCloseData(_Model):
    """Close an account; postings after the date are rejected."""

    account: AccountName
    date: dt.date


class EvidenceData(_Model):
    """An imported source document, stored immutably by digest."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str
    media_type: str = "application/octet-stream"
    size: int = Field(ge=0)
    source: str | None = Field(default=None, description="Where the document came from, e.g. a bank name.")
    description: str | None = None


class LineData(_Model):
    """One observed statement line extracted from evidence (not yet accounted for)."""

    evidence: str = Field(pattern=r"^evd_")
    account: AccountName
    date: dt.date
    amount: DecimalStr
    commodity: CommodityCode
    description: str = ""
    payee: str | None = None
    external_id: str | None = None
    fingerprint: str
    row: int = Field(ge=1)


class PostingData(_Model):
    """A single posting with an explicit amount."""

    account: AccountName
    amount: DecimalStr
    commodity: CommodityCode


class EntryData(_Model):
    """A balanced double-entry accounting transaction."""

    date: dt.date
    payee: str | None = None
    narration: str | None = None
    postings: list[PostingData] = Field(min_length=2)
    tags: list[str] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list, description="Statement line ids this entry accounts for.")
    evidence: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class CorrectionData(_Model):
    """Supersede the current version of a record with a replacement, or void it.

    A correction can target an entry, a statement line, an ``account.open``, or an
    ``account.close``. The replacement must be a payload of the target's kind; it
    is validated against that kind when the correction is applied.
    """

    target: str = Field(description="The current version id of the entry, line, or account record to correct.")
    replacement: dict[str, Any] | None = Field(
        default=None, description="The corrected payload, of the same kind as the target; omit to void."
    )
    reason: str = Field(min_length=1)


class PriceData(_Model):
    """An exchange rate: one unit of ``base`` is worth ``rate`` units of ``quote``."""

    date: dt.date
    base: CommodityCode
    quote: CommodityCode
    rate: DecimalStr
    source: str | None = None


class ReconciliationData(_Model):
    """A statement balance checked against the ledger as of a date."""

    account: AccountName
    date: dt.date
    commodity: CommodityCode
    statement_balance: DecimalStr
    ledger_balance: DecimalStr
    difference: DecimalStr
    unmatched_lines: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    note: str | None = None


class NoteData(_Model):
    """An explanation or annotation attached to one or more records."""

    subjects: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ChangeData(_Model):
    """One proposed record. String values ``"$N"`` refer to the id produced by change N."""

    kind: Literal[
        "account.open", "account.close", "evidence", "line", "entry", "correction", "price", "reconciliation", "note"
    ]
    data: dict[str, Any]
    rationale: str | None = None


class OperationData(_Model):
    """A proposed set of changes together with its provenance."""

    tool: str = Field(min_length=1, description="The operation that produced the proposal, e.g. 'categorize'.")
    summary: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    reasoning: str = Field(min_length=1, description="A short summary of why these changes are proposed.")
    confidence: float = Field(ge=0, le=1)
    changes: list[ChangeData] = Field(min_length=1)
    base: str | None = Field(default=None, description="The head record id the proposal was made against.")


class DecisionData(_Model):
    """A human (or policy) verdict on an operation."""

    operation: str = Field(pattern=r"^opr_")
    verdict: Literal["approve", "reject"]
    note: str | None = None


class AppliedData(_Model):
    """The records an approved operation produced."""

    operation: str = Field(pattern=r"^opr_")
    results: list[str]


DATA_MODELS: dict[RecordKind, type[_Model]] = {
    RecordKind.ACCOUNT_OPEN: AccountOpenData,
    RecordKind.ACCOUNT_CLOSE: AccountCloseData,
    RecordKind.EVIDENCE: EvidenceData,
    RecordKind.LINE: LineData,
    RecordKind.ENTRY: EntryData,
    RecordKind.CORRECTION: CorrectionData,
    RecordKind.PRICE: PriceData,
    RecordKind.RECONCILIATION: ReconciliationData,
    RecordKind.NOTE: NoteData,
    RecordKind.OPERATION: OperationData,
    RecordKind.DECISION: DecisionData,
    RecordKind.APPLIED: AppliedData,
}


def format_validation_error(error: PydanticValidationError) -> str:
    """Render a Pydantic validation error as one readable line.

    Args:
        error: The Pydantic error.

    Returns:
        A ``field: message; field: message`` summary.

    """
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def parse_data(kind: RecordKind, data: Any) -> Any:
    """Validate and parse a record payload.

    Args:
        kind: The record kind.
        data: The raw payload.

    Returns:
        The parsed model instance.

    Raises:
        ValidationError: If the payload does not satisfy the kind's schema.

    """
    try:
        return DATA_MODELS[kind].model_validate(data)
    except PydanticValidationError as exc:
        raise ValidationError(f"invalid {kind.value} data: {format_validation_error(exc)}") from exc


def dump_data(model: BaseModel) -> dict[str, Any]:
    """Serialise a payload model to canonical JSON-compatible data.

    Args:
        model: The payload model.

    Returns:
        A JSON-compatible dictionary with dates as ISO strings and no nulls or
        empty collections that carry defaults.

    """
    return model.model_dump(mode="json", exclude_defaults=True)


def json_schema(kind: str = "operation") -> dict[str, Any]:
    """Return the JSON Schema for a record kind's payload.

    Args:
        kind: A record kind value, e.g. ``"operation"`` or ``"entry"``.

    Returns:
        The JSON Schema dictionary.

    Raises:
        UsageError: If the kind is unknown.

    """
    try:
        record_kind = RecordKind(kind)
    except ValueError as exc:
        valid = ", ".join(k.value for k in RecordKind)
        raise UsageError(f"unknown record kind {kind!r}; expected one of: {valid}") from exc
    return DATA_MODELS[record_kind].model_json_schema()
