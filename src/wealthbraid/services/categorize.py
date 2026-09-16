"""Categorization: proposing entries for unmatched statement lines.

Each unmatched statement line becomes a proposed two-posting entry: the line's
amount on the statement account, balanced by a category (counter) account. The
category comes from one of two sources, and both produce an operation that
waits for human approval:

* the book's ordered rules (``[[rules]]`` in ``wealthbraid.toml``), or
* explicit assignments supplied by an agent or human, each with an optional
  rationale and confidence.

The operation records the rules or assignments used, the evidence behind every
line, and the lowest per-line confidence as its overall confidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from wealthbraid.book.book import Book
from wealthbraid.book.config import CategorizeRule
from wealthbraid.book.schema import LineData
from wealthbraid.book.state import BookState, OperationState
from wealthbraid.errors import UsageError, ValidationError


@dataclass(frozen=True)
class Assignment:
    """A category chosen for one statement line.

    Attributes:
        line: The statement line id.
        account: The counter account.
        confidence: Confidence in this assignment.
        rationale: Why this account was chosen.

    """

    line: str
    account: str
    confidence: float
    rationale: str


def line_text(line: LineData) -> str:
    """Return the text rules match against.

    Args:
        line: The statement line.

    Returns:
        Payee and description joined by a space.

    """
    return " ".join(part for part in (line.payee, line.description) if part)


def suggest_by_rules(state: BookState, rules: Sequence[CategorizeRule], lines: Sequence[str]) -> list[Assignment]:
    """Match statement lines against rules; the first matching rule wins.

    Args:
        state: The book state.
        rules: The ordered rules.
        lines: Candidate line ids.

    Returns:
        Assignments for the lines some rule matched.

    """
    assignments = []
    for line_id in lines:
        line = state.lines[line_id]
        text = line_text(line)
        for index, rule in enumerate(rules):
            if rule.matches(text, line.account):
                assignments.append(
                    Assignment(
                        line=line_id,
                        account=rule.account,
                        confidence=rule.confidence,
                        rationale=f"rule {index + 1} /{rule.pattern}/ matched {text!r}",
                    )
                )
                break
    return assignments


def parse_assignments(raw: Sequence[Mapping[str, Any]]) -> list[Assignment]:
    """Parse assignments supplied as JSON.

    Args:
        raw: Objects with ``line``, ``account``, and optional ``confidence`` and ``rationale``.

    Returns:
        The parsed assignments.

    Raises:
        UsageError: If an item is malformed.

    """
    assignments = []
    for index, item in enumerate(raw):
        try:
            confidence = float(item.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                raise ValueError("confidence must be between 0 and 1")
            assignments.append(
                Assignment(
                    line=str(item["line"]),
                    account=str(item["account"]),
                    confidence=confidence,
                    rationale=str(item.get("rationale") or "assigned explicitly"),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise UsageError(f"assignment {index}: {exc}") from exc
    return assignments


def entry_for(line_id: str, line: LineData, account: str) -> dict[str, Any]:
    """Build the entry payload that accounts for a statement line.

    Args:
        line_id: The statement line id.
        line: The statement line.
        account: The counter account.

    Returns:
        An entry payload matching the line.

    """
    amount = Decimal(line.amount)
    data: dict[str, Any] = {
        "date": line.date.isoformat(),
        "postings": [
            {"account": line.account, "amount": str(amount), "commodity": line.commodity},
            {"account": account, "amount": str(-amount), "commodity": line.commodity},
        ],
        "lines": [line_id],
        "evidence": [line.evidence],
    }
    if line.payee:
        data["payee"] = line.payee
    if line.description:
        data["narration"] = line.description
    return data


def categorize(
    book: Book,
    *,
    actor: str,
    assignments: Sequence[Assignment] | None = None,
    reasoning: str | None = None,
    limit: int | None = None,
) -> tuple[OperationState | None, list[str]]:
    """Propose entries for unmatched statement lines.

    Rule-based categorization skips lines a pending proposal already covers, so
    running it twice does not queue duplicates. Explicit assignments may still
    cover such lines, for example to offer the reviewer an alternative.

    Args:
        book: The book.
        actor: The proposer.
        assignments: Explicit assignments; when omitted, the book's rules are used.
        reasoning: The proposer's reasoning summary (required with explicit assignments).
        limit: Categorize at most this many lines.

    Returns:
        The proposed operation (``None`` if nothing matched) and the unmatched line ids that are
        neither in this proposal nor in another pending proposal.

    Raises:
        UsageError: If explicit assignments come without reasoning or name duplicate lines.
        ValidationError: If an assignment names a line that is unknown or already matched.

    """
    state = book.state()
    unmatched = state.unmatched_lines()
    already_proposed = state.pending_line_proposals()
    if assignments is None:
        candidates = [line_id for line_id in unmatched if line_id not in already_proposed]
        chosen = suggest_by_rules(state, book.config.rules, candidates)
        inputs: dict[str, Any] = {"source": "rules", "rules": [rule.to_json() for rule in book.config.rules]}
        reasoning = reasoning or "Statement lines matched categorization rules from the book settings."
    else:
        if not reasoning:
            raise UsageError("explicit assignments need a reasoning summary")
        chosen = list(assignments)
        open_lines = set(unmatched)
        seen = set()
        for assignment in chosen:
            if assignment.line not in state.lines:
                raise ValidationError(f"unknown statement line {assignment.line}")
            if assignment.line not in open_lines:
                raise ValidationError(f"statement line {assignment.line} is already matched")
            if assignment.line in seen:
                raise UsageError(f"statement line {assignment.line} is assigned twice")
            seen.add(assignment.line)
        inputs = {
            "source": "assignments",
            "assignments": [
                {"line": a.line, "account": a.account, "confidence": a.confidence, "rationale": a.rationale}
                for a in chosen
            ],
        }
    if limit is not None:
        chosen = chosen[:limit]
    covered = {assignment.line for assignment in chosen}
    remaining = [line_id for line_id in unmatched if line_id not in covered and line_id not in already_proposed]
    if not chosen:
        return None, remaining

    changes = [
        {
            "kind": "entry",
            "data": entry_for(a.line, state.lines[a.line], a.account),
            "rationale": f"{a.rationale} (confidence {a.confidence:.2f})",
        }
        for a in chosen
    ]
    evidence = sorted({state.lines[a.line].evidence for a in chosen})
    operation = book.propose(
        actor=actor,
        tool="categorize",
        summary=f"Categorize {len(chosen)} statement line{'s' if len(chosen) != 1 else ''}",
        changes=changes,
        reasoning=reasoning,
        confidence=min(a.confidence for a in chosen),
        evidence=evidence,
        inputs=inputs,
    )
    return operation, remaining
