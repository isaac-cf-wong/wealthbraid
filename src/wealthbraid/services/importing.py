"""CSV statement import: evidence in, statement lines out.

Importing never creates accounting entries. It stores the file as evidence and
proposes one ``line`` record per row: an observation of what the bank reported.
Lines do not change balances, so policy can apply them without review; turning
them into entries is the job of categorization, which does need approval.

Each line carries a fingerprint so re-importing the same (or an overlapping)
statement skips rows already recorded. The fingerprint uses the bank's own
reference when the file has one; otherwise it uses the account, date, amount,
commodity, and normalised description, plus the row's occurrence number among
identical rows in the same file, so two genuine identical purchases on one day
are both kept.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from wealthbraid.book.book import Book
from wealthbraid.book.config import ImportProfile
from wealthbraid.book.state import OperationState
from wealthbraid.errors import NotFoundError, UsageError, ValidationError


@dataclass(frozen=True)
class ParsedRow:
    """One statement row parsed from a CSV file."""

    row: int
    date: dt.date
    amount: Decimal
    description: str
    payee: str | None
    external_id: str | None


@dataclass
class ImportResult:
    """The outcome of an import.

    Attributes:
        evidence: The evidence record id of the imported file.
        rows: Rows parsed from the file.
        new_lines: Rows proposed as new statement lines.
        duplicates: Rows skipped because they were already recorded.
        operation: The line-import operation, if any rows were new.

    """

    evidence: str
    rows: int
    new_lines: int
    duplicates: list[int] = field(default_factory=list)
    operation: OperationState | None = None


def _parse_decimal(text: str, *, decimal_comma: bool, row: int, column: str) -> Decimal:
    cleaned = text.strip().replace(" ", "").replace("\u00a0", "")
    cleaned = cleaned.replace(".", "").replace(",", ".") if decimal_comma else cleaned.replace(",", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValidationError(f"row {row}: invalid amount {text!r} in column {column!r}") from exc
    return -value if negative else value


def parse_csv(content: bytes, profile: ImportProfile) -> list[ParsedRow]:
    """Parse a CSV statement with a column mapping.

    Args:
        content: The raw file bytes (UTF-8, optionally with a BOM).
        profile: The column mapping.

    Returns:
        The parsed rows in file order.

    Raises:
        ValidationError: If the file cannot be decoded, a column is missing, or a cell is malformed.
        UsageError: If the profile names neither an amount column nor debit/credit columns.

    """
    if profile.amount is None and not (profile.debit or profile.credit):
        raise UsageError("import profile needs an amount column or debit/credit columns")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"statement is not valid UTF-8: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text), delimiter=profile.delimiter)
    headers = reader.fieldnames or []
    required = [profile.date, *(c for c in (profile.amount, profile.debit, profile.credit) if c)]
    optional = [c for c in (profile.description, profile.payee, profile.external_id) if c]
    missing = [column for column in [*required, *optional] if column not in headers]
    if missing:
        raise ValidationError(f"statement is missing column(s): {', '.join(missing)} (found: {', '.join(headers)})")

    rows = []
    for number, raw in enumerate(reader, start=2):
        if not any((value or "").strip() for value in raw.values()):
            continue
        date_text = (raw.get(profile.date) or "").strip()
        try:
            if profile.date_format:
                date = dt.datetime.strptime(date_text, profile.date_format).date()  # noqa: DTZ007
            else:
                date = dt.date.fromisoformat(date_text)
        except ValueError as exc:
            raise ValidationError(f"row {number}: invalid date {date_text!r}") from exc
        if profile.amount:
            amount = _parse_decimal(
                raw.get(profile.amount) or "", decimal_comma=profile.decimal_comma, row=number, column=profile.amount
            )
        else:
            amount = Decimal(0)
            if profile.credit and (raw.get(profile.credit) or "").strip():
                amount += _parse_decimal(
                    raw[profile.credit], decimal_comma=profile.decimal_comma, row=number, column=profile.credit
                )
            if profile.debit and (raw.get(profile.debit) or "").strip():
                amount -= abs(
                    _parse_decimal(
                        raw[profile.debit], decimal_comma=profile.decimal_comma, row=number, column=profile.debit
                    )
                )
        rows.append(
            ParsedRow(
                row=number,
                date=date,
                amount=amount,
                description=" ".join((raw.get(profile.description) or "").split()) if profile.description else "",
                payee=((raw.get(profile.payee) or "").strip() or None) if profile.payee else None,
                external_id=((raw.get(profile.external_id) or "").strip() or None) if profile.external_id else None,
            )
        )
    return rows


def fingerprint_rows(rows: list[ParsedRow], *, account: str, commodity: str) -> list[str]:
    """Compute a deduplication fingerprint for every row.

    Args:
        rows: The parsed rows in file order.
        account: The statement account.
        commodity: The statement commodity.

    Returns:
        One hex fingerprint per row.

    """
    seen: dict[str, int] = {}
    fingerprints = []
    for row in rows:
        if row.external_id:
            basis = f"ref|{account}|{row.external_id}"
        else:
            content = (
                f"row|{account}|{row.date.isoformat()}|{row.amount.normalize()}|{commodity}|{row.description.lower()}"
            )
            occurrence = seen.get(content, 0)
            seen[content] = occurrence + 1
            basis = f"{content}|{occurrence}"
        fingerprints.append(hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32])
    return fingerprints


def import_csv(  # noqa: PLR0913 - keyword-only options
    book: Book,
    path: Path,
    *,
    actor: str,
    profile: ImportProfile,
    account: str | None = None,
    commodity: str | None = None,
    source: str | None = None,
) -> ImportResult:
    """Import a CSV statement as evidence plus proposed statement lines.

    Args:
        book: The book to import into.
        path: The CSV file.
        actor: Who is importing.
        profile: The column mapping.
        account: The statement account (overrides the profile's).
        commodity: The statement commodity (overrides the profile's; defaults to the book currency).
        source: Where the statement came from.

    Returns:
        The :class:`ImportResult`.

    Raises:
        NotFoundError: If the file does not exist.
        UsageError: If no statement account is given.

    """
    if not path.is_file():
        raise NotFoundError(f"file not found: {path}")
    statement_account = account or profile.account
    if not statement_account:
        raise UsageError("give the statement account with --account or in the import profile")
    statement_commodity = commodity or profile.commodity or book.config.currency
    effective = replace(profile, account=statement_account, commodity=statement_commodity)

    content = path.read_bytes()
    rows = parse_csv(content, effective)
    fingerprints = fingerprint_rows(rows, account=statement_account, commodity=statement_commodity)
    if statement_account not in book.state().accounts:
        raise ValidationError(f"account not opened: {statement_account}")

    evidence_id, _ = book.add_evidence(content, filename=path.name, actor=actor, source=source)
    known = book.state().line_fingerprints
    result = ImportResult(evidence=evidence_id, rows=len(rows), new_lines=0)
    changes = []
    for row, fingerprint in zip(rows, fingerprints, strict=True):
        if fingerprint in known:
            result.duplicates.append(row.row)
            continue
        data = {
            "evidence": evidence_id,
            "account": statement_account,
            "date": row.date.isoformat(),
            "amount": str(row.amount),
            "commodity": statement_commodity,
            "description": row.description,
            "fingerprint": fingerprint,
            "row": row.row,
        }
        if row.payee:
            data["payee"] = row.payee
        if row.external_id:
            data["external_id"] = row.external_id
        changes.append({"kind": "line", "data": data})
    result.new_lines = len(changes)
    if changes:
        result.operation = book.propose(
            actor=actor,
            tool="import.csv",
            summary=f"Import {len(changes)} statement lines from {path.name} into {statement_account}",
            changes=changes,
            reasoning=(
                f"Parsed {len(rows)} rows with profile {profile.name!r}; "
                f"{len(result.duplicates)} already recorded were skipped."
            ),
            confidence=1.0,
            evidence=[evidence_id],
            inputs={"file": path.name, "profile": effective.to_json()},
        )
    return result
