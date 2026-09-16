"""Monetary value types: :class:`Commodity` and :class:`Amount`.

All monetary quantities are :class:`decimal.Decimal`. Floats are rejected at
construction so floating-point error can never enter the accounting engine. An
:class:`Amount` pairs an exact quantity with a commodity; arithmetic between
amounts of different commodities is an error rather than a silent coercion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from wealthbraid.engine.errors import (
    CommodityMismatchError,
    InvalidAmountError,
    InvalidCommodityError,
)

# A commodity code: an uppercase letter followed by letters (any case), digits,
# or the separators ``.``, ``_``, ``-``, ``'`` (for example USD, EUR, BTC, AAPL,
# VANGUARD-500, USTBillOct). The leading character must be uppercase so a code is
# never confused with a lowercase metadata key or account component.
_COMMODITY_RE = re.compile(r"[A-Z][A-Za-z0-9._'-]*")


@dataclass(frozen=True)
class Commodity:
    """A unit in which value is measured: a currency, security, or token.

    Attributes:
        code: The commodity's canonical code, for example ``"USD"``.

    """

    code: str

    def __post_init__(self) -> None:
        """Validate the commodity code against the commodity grammar.

        Raises:
            InvalidCommodityError: If the code is empty or malformed.

        """
        if not _COMMODITY_RE.fullmatch(self.code):
            raise InvalidCommodityError(f"Invalid commodity code: {self.code!r}")

    def __str__(self) -> str:
        """Return the commodity code.

        Returns:
            The commodity code.

        """
        return self.code


@dataclass(frozen=True)
class Amount:
    """An exact quantity of a single commodity.

    The quantity is always a :class:`~decimal.Decimal`; passing a ``float`` is an
    error. The Decimal's own exponent carries the amount's precision, which the
    balancing rules use to infer tolerances.

    Attributes:
        quantity: The exact signed quantity.
        commodity: The commodity the quantity is denominated in.

    """

    quantity: Decimal
    commodity: Commodity

    _COERCIBLE: ClassVar[tuple[type, ...]] = (Decimal, int, str)

    def __post_init__(self) -> None:
        """Coerce and validate the quantity.

        ``int`` and ``str`` quantities are coerced to :class:`~decimal.Decimal`.
        ``float`` (and ``bool``) are rejected to prevent floating-point error
        from entering the engine.

        Raises:
            InvalidAmountError: If the quantity is a float/bool or otherwise not
                convertible to a finite Decimal.

        """
        quantity = self.quantity
        if isinstance(quantity, bool) or not isinstance(quantity, self._COERCIBLE):
            raise InvalidAmountError(
                f"Amount quantity must be Decimal, int, or str, not {type(quantity).__name__} "
                "(floats are rejected to avoid rounding error)"
            )
        if not isinstance(quantity, Decimal):
            try:
                quantity = Decimal(quantity)
            except Exception as exc:
                raise InvalidAmountError(f"Cannot parse quantity {quantity!r} as a decimal") from exc
        if not quantity.is_finite():
            raise InvalidAmountError(f"Amount quantity must be finite, got {quantity!r}")
        object.__setattr__(self, "quantity", quantity)

    @classmethod
    def of(cls, quantity: Decimal | int | str, code: str) -> Amount:
        """Construct an amount from a quantity and a commodity code.

        Args:
            quantity: The quantity, as a Decimal, int, or decimal string.
            code: The commodity code.

        Returns:
            The constructed :class:`Amount`.

        """
        return cls(quantity=quantity, commodity=Commodity(code))  # type: ignore[arg-type]

    @property
    def fractional_digits(self) -> int:
        """Return the number of digits after the decimal point.

        Returns:
            The count of fractional digits (0 for integers).

        """
        exponent = self.quantity.as_tuple().exponent
        if not isinstance(exponent, int):  # NaN/Inf excluded at construction
            return 0
        return max(0, -exponent)

    def _check_same_commodity(self, other: Amount) -> None:
        """Ensure ``other`` shares this amount's commodity.

        Args:
            other: The amount to compare against.

        Raises:
            CommodityMismatchError: If the commodities differ.

        """
        if self.commodity != other.commodity:
            raise CommodityMismatchError(f"Cannot combine {self.commodity} with {other.commodity}")

    def __add__(self, other: Amount) -> Amount:
        """Add two amounts of the same commodity.

        Args:
            other: The amount to add.

        Returns:
            The sum as a new :class:`Amount`.

        """
        self._check_same_commodity(other)
        return Amount(self.quantity + other.quantity, self.commodity)

    def __sub__(self, other: Amount) -> Amount:
        """Subtract an amount of the same commodity.

        Args:
            other: The amount to subtract.

        Returns:
            The difference as a new :class:`Amount`.

        """
        self._check_same_commodity(other)
        return Amount(self.quantity - other.quantity, self.commodity)

    def __neg__(self) -> Amount:
        """Return the negated amount.

        Returns:
            A new :class:`Amount` with the sign flipped.

        """
        return Amount(-self.quantity, self.commodity)

    def __mul__(self, factor: Decimal | int | str) -> Amount:
        """Scale the amount by a scalar.

        Args:
            factor: The scalar multiplier (Decimal, int, or decimal string).

        Returns:
            The scaled amount as a new :class:`Amount`.

        Raises:
            InvalidAmountError: If ``factor`` is a float or not numeric.

        """
        if isinstance(factor, bool) or not isinstance(factor, self._COERCIBLE):
            raise InvalidAmountError(f"Cannot scale an amount by {type(factor).__name__}")
        return Amount(self.quantity * Decimal(factor), self.commodity)

    def is_zero(self, tolerance: Decimal | None = None) -> bool:
        """Report whether the amount is zero within an optional tolerance.

        Args:
            tolerance: Optional non-negative tolerance; exact zero if omitted.

        Returns:
            ``True`` if the absolute quantity does not exceed the tolerance.

        """
        if tolerance is None:
            return self.quantity == 0
        return abs(self.quantity) <= tolerance

    def __str__(self) -> str:
        """Render the amount as ``<quantity> <commodity>``.

        Returns:
            A human-readable string such as ``"10.00 USD"``.

        """
        return f"{self.quantity} {self.commodity}"
