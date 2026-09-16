"""Deterministic wealth scenarios.

A scenario projects a starting amount forward month by month with
contributions, withdrawals, a nominal return, and inflation. Everything is
``Decimal`` arithmetic with fixed rules, so the same inputs and the same book
record always give the same numbers.

Monthly rates are the geometric equivalents of the annual rates
(``(1 + r) ** (1/12) - 1``), so twelve months of growth compound to exactly the
stated annual rate. Contributions are added at the end of each month (after that
month's growth); withdrawals likewise. Each projected year decomposes exactly:

    end = start + contributions - withdrawals + growth

and the result reports the identity's rounding ``residual`` and a ``reconciles``
flag (residual within 1e-12) so a reader can check it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from wealthbraid.book.schema import DecimalStr, format_validation_error
from wealthbraid.book.state import BookState
from wealthbraid.errors import ValidationError
from wealthbraid.services.reports import basis, net_worth
from wealthbraid.store.records import canonical_json

_CENT = Decimal("0.01")
_MONTHS = 12
# The identity end = start + contributions - withdrawals + growth holds exactly in
# real arithmetic; 34-digit Decimal sums leave rounding residuals far below this.
_IDENTITY_TOLERANCE = Decimal("1e-12")


class ScenarioAssumptions(BaseModel):
    """The tunable assumptions of a scenario. Rates are annual fractions (``"0.05"`` = 5%)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    starting_amount: DecimalStr | None = Field(
        default=None, description="Starting amount; omit to use the book's net worth on the start date."
    )
    monthly_contribution: DecimalStr = "0"
    contribution_growth: DecimalStr = Field(default="0", description="Annual growth of the contribution amount.")
    monthly_withdrawal: DecimalStr = "0"
    withdrawal_start_year: int = Field(default=1, ge=1, description="First projection year with withdrawals.")
    annual_return: DecimalStr = "0"
    annual_inflation: DecimalStr = "0"


class ScenarioSpec(BaseModel):
    """A scenario file: base assumptions plus named variants that override some of them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    start: dt.date
    years: int = Field(ge=1, le=100)
    currency: str | None = None
    assumptions: ScenarioAssumptions = ScenarioAssumptions()
    variants: dict[str, dict[str, Any]] = Field(default_factory=dict)


def parse_spec(raw: Any) -> ScenarioSpec:
    """Validate a scenario specification.

    Args:
        raw: The decoded TOML or JSON document.

    Returns:
        The parsed :class:`ScenarioSpec`.

    Raises:
        ValidationError: If the document is malformed.

    """
    try:
        spec = ScenarioSpec.model_validate(raw)
        for name, overrides in spec.variants.items():
            _variant_assumptions(spec, name, overrides)
    except PydanticValidationError as exc:
        raise ValidationError(f"invalid scenario: {format_validation_error(exc)}") from exc
    return spec


def _variant_assumptions(spec: ScenarioSpec, name: str, overrides: dict[str, Any]) -> ScenarioAssumptions:
    try:
        return ScenarioAssumptions.model_validate({**spec.assumptions.model_dump(), **overrides})
    except PydanticValidationError as exc:
        raise ValidationError(f"invalid variant {name!r}: {format_validation_error(exc)}") from exc


def monthly_rate(annual: Decimal) -> Decimal:
    """Convert an annual rate to its geometric monthly equivalent.

    Args:
        annual: The annual rate as a fraction.

    Returns:
        The monthly rate.

    Raises:
        ValidationError: If the rate is -100% or lower.

    """
    if annual <= -1:
        raise ValidationError("annual rates must be greater than -1 (-100%)")
    with localcontext() as context:
        context.prec = 34
        return (Decimal(1) + annual) ** (Decimal(1) / Decimal(_MONTHS)) - Decimal(1)


def project(assumptions: ScenarioAssumptions, *, start_amount: Decimal, years: int) -> list[dict[str, Any]]:
    """Project a balance year by year.

    Args:
        assumptions: The scenario assumptions.
        start_amount: The starting balance.
        years: The horizon in years.

    Returns:
        One row per year with the start, contributions, withdrawals, growth, end
        (nominal), end in start-date money (real), and the decomposition check.

    """
    growth_rate = monthly_rate(Decimal(assumptions.annual_return))
    inflation = Decimal(assumptions.annual_inflation)
    if inflation <= -1:
        raise ValidationError("annual inflation must be greater than -1 (-100%)")
    contribution_growth = Decimal(assumptions.contribution_growth)
    withdrawal = Decimal(assumptions.monthly_withdrawal)

    rows = []
    balance = start_amount
    contribution = Decimal(assumptions.monthly_contribution)
    deflator = Decimal(1)
    with localcontext() as context:
        context.prec = 34
        for year in range(1, years + 1):
            year_start = balance
            contributed = withdrawn = growth = Decimal(0)
            for _ in range(_MONTHS):
                month_growth = balance * growth_rate
                balance += month_growth
                growth += month_growth
                balance += contribution
                contributed += contribution
                if year >= assumptions.withdrawal_start_year:
                    balance -= withdrawal
                    withdrawn += withdrawal
            deflator *= Decimal(1) + inflation
            rows.append(
                {
                    "year": year,
                    "start": _money(year_start),
                    "contributions": _money(contributed),
                    "withdrawals": _money(withdrawn),
                    "growth": _money(growth),
                    "end": _money(balance),
                    "end_real": _money(balance / deflator),
                    "residual": str(balance - (year_start + contributed - withdrawn + growth)),
                    "reconciles": abs(balance - (year_start + contributed - withdrawn + growth)) <= _IDENTITY_TOLERANCE,
                }
            )
            contribution *= Decimal(1) + contribution_growth
    return rows


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_EVEN))


def run_scenario(state: BookState, spec: ScenarioSpec, *, default_currency: str) -> dict[str, Any]:
    """Run a scenario and each of its variants.

    Args:
        state: The book state (used for the starting net worth when not given).
        spec: The scenario specification.
        default_currency: The currency when the spec names none.

    Returns:
        Per-variant yearly projections and summaries, the starting point, the
        inputs digest, and the record basis.

    """
    currency = spec.currency or default_currency
    worth = net_worth(state, as_of=spec.start, currency=currency)
    runs = {"base": spec.assumptions}
    for name, overrides in spec.variants.items():
        runs[name] = _variant_assumptions(spec, name, overrides)

    results = {}
    for name, assumptions in runs.items():
        from_book = assumptions.starting_amount is None
        start_amount = Decimal(worth["net_worth"]) if from_book else Decimal(assumptions.starting_amount)
        rows = project(assumptions, start_amount=start_amount, years=spec.years)
        final = rows[-1]
        results[name] = {
            "assumptions": assumptions.model_dump(mode="json"),
            "starting_amount": str(start_amount),
            "starting_amount_source": "book net worth" if from_book else "specified",
            "years": rows,
            "summary": {
                "end": final["end"],
                "end_real": final["end_real"],
                "total_contributions": _money(sum((Decimal(r["contributions"]) for r in rows), Decimal(0))),
                "total_withdrawals": _money(sum((Decimal(r["withdrawals"]) for r in rows), Decimal(0))),
                "total_growth": _money(sum((Decimal(r["growth"]) for r in rows), Decimal(0))),
                "reconciles": all(r["reconciles"] for r in rows),
            },
        }
    spec_json = spec.model_dump(mode="json")
    return {
        "scenario": spec.name,
        "basis": basis(state, start=spec.start, currency=currency),
        "inputs_sha256": hashlib.sha256(canonical_json(spec_json).encode("utf-8")).hexdigest(),
        "spec": spec_json,
        "currency": currency,
        "book_net_worth": {"as_of": spec.start.isoformat(), "value": worth["net_worth"], "unvalued": worth["unvalued"]},
        "variants": results,
    }


SCENARIO_TEMPLATE = """\
# A wealthbraid scenario. Rates are annual fractions: "0.05" means 5%.
name = "retire-at-55"
start = "2026-10-01"
years = 30
# currency = "EUR"   # defaults to the book currency

[assumptions]
# starting_amount = "50000"   # omit to start from the book's net worth on `start`
monthly_contribution = "1500"
contribution_growth = "0.02"
annual_return = "0.05"
annual_inflation = "0.02"
monthly_withdrawal = "0"
withdrawal_start_year = 1

[variants.cautious]
annual_return = "0.03"

[variants.early-retirement]
monthly_withdrawal = "3000"
withdrawal_start_year = 20
"""
