"""
USD <-> micro-units conversion for the GoDaddy Auctions API.

GoDaddy's bid endpoint takes bid amounts in USD micro-units:
    $1.00 = 1,000,000 micro-units

This module is the ONLY place in the codebase that performs that conversion,
and every conversion is guarded with hard sanity checks. The intent is that
even if other code has bugs, a malformed amount cannot reach the API.

Critical rules enforced here:
    1. Negative or zero amounts are rejected.
    2. Amounts above MAX_REASONABLE_DOLLARS are rejected outright.
    3. Cents-precision only (we round to 2 decimal places, no fractions of a cent).
    4. The output is always a positive int, never a float.
    5. All conversions are logged via the audit hook (set by the caller).

This is the file that prevents the "$26,000 instead of $2,600" mistake.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Callable, Optional, Union

# Conversion factor. $1 = 1,000,000 micro-units. Confirmed via official Swagger spec.
MICROS_PER_DOLLAR: int = 1_000_000

# Hard ceiling on any single conversion. This is intentionally well above any
# reasonable single-domain bid; anything higher is treated as a bug. The
# per-transaction safety governor should reject far below this number; this
# constant is the absolute floor of last resort.
MAX_REASONABLE_DOLLARS: Decimal = Decimal("1000000.00")  # $1,000,000

# Audit hook. Caller can set this to capture every conversion for the audit log.
# Default no-op so the module is self-sufficient.
_audit_hook: Optional[Callable[[str, Decimal, int], None]] = None


def set_audit_hook(hook: Callable[[str, Decimal, int], None]) -> None:
    """Register an audit callback. Called with (operation, dollars, micros) on every conversion."""
    global _audit_hook
    _audit_hook = hook


class MicroUnitsError(ValueError):
    """Raised when a conversion would produce an invalid or unsafe amount."""


def _to_decimal(amount: Union[int, float, str, Decimal]) -> Decimal:
    """Coerce input to Decimal with strict validation. Floats are converted via str()
    to avoid binary-float precision errors (0.1 + 0.2 != 0.3 territory)."""
    if isinstance(amount, Decimal):
        return amount
    if isinstance(amount, int):
        return Decimal(amount)
    if isinstance(amount, float):
        # Route floats through str to preserve user-intended decimal digits.
        return Decimal(str(amount))
    if isinstance(amount, str):
        try:
            return Decimal(amount.strip().replace("$", "").replace(",", ""))
        except InvalidOperation as e:
            raise MicroUnitsError(f"Could not parse dollar amount: {amount!r}") from e
    raise MicroUnitsError(
        f"Unsupported dollar input type: {type(amount).__name__} (value={amount!r})"
    )


def dollars_to_micros(amount: Union[int, float, str, Decimal]) -> int:
    """Convert a USD dollar amount to GoDaddy micro-units (int).

    The conversion rounds to the nearest cent first, then multiplies. This means
    $1.005 becomes $1.01 (rounded), which becomes 1_010_000 micro-units.

    Raises MicroUnitsError if the amount is:
      - non-numeric or unparseable
      - zero or negative
      - above MAX_REASONABLE_DOLLARS
      - not finite (NaN, Infinity)

    Returns: positive int representing the amount in micro-units.

    Examples:
        >>> dollars_to_micros(1)
        1000000
        >>> dollars_to_micros("100")
        100000000
        >>> dollars_to_micros(34.50)
        34500000
        >>> dollars_to_micros("$2,600.00")
        2600000000
    """
    dollars = _to_decimal(amount)

    if not dollars.is_finite():
        raise MicroUnitsError(f"Dollar amount must be finite, got {amount!r}")

    if dollars <= 0:
        raise MicroUnitsError(
            f"Dollar amount must be positive, got {dollars} (from input {amount!r})"
        )

    if dollars > MAX_REASONABLE_DOLLARS:
        raise MicroUnitsError(
            f"Dollar amount ${dollars:,.2f} exceeds absolute safety ceiling "
            f"${MAX_REASONABLE_DOLLARS:,.2f}. If this is intentional, raise the "
            f"ceiling explicitly with code review."
        )

    # Round to cents (banker's rounding would be unusual for money; use ROUND_HALF_UP).
    cents = dollars.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    micros = int(cents * MICROS_PER_DOLLAR)

    if _audit_hook is not None:
        _audit_hook("dollars_to_micros", cents, micros)

    return micros


def micros_to_dollars(micros: int) -> Decimal:
    """Convert a micro-units integer back to a USD Decimal (cents precision).

    Used for reading bid amounts back from API responses and the audit log,
    and for display. Inverse of dollars_to_micros (up to rounding).

    Examples:
        >>> micros_to_dollars(1000000)
        Decimal('1.00')
        >>> micros_to_dollars(34500000)
        Decimal('34.50')
    """
    if not isinstance(micros, int):
        raise MicroUnitsError(
            f"Micro-units must be an int, got {type(micros).__name__} (value={micros!r})"
        )
    if micros < 0:
        raise MicroUnitsError(f"Micro-units must be non-negative, got {micros}")

    dollars = (Decimal(micros) / MICROS_PER_DOLLAR).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    if _audit_hook is not None:
        _audit_hook("micros_to_dollars", dollars, micros)

    return dollars


def assert_within_transaction_cap(dollars: Union[int, float, str, Decimal], cap: Decimal) -> None:
    """Raise MicroUnitsError if the amount exceeds the configured per-transaction cap.

    This is a defensive check called by the API client before any bid/buy fires.
    Even though the safety governors module enforces the same rule, repeating it
    here means a missed governor invocation still can't get past the API layer.
    """
    dollars = _to_decimal(dollars)
    if dollars > cap:
        raise MicroUnitsError(
            f"Amount ${dollars:,.2f} exceeds per-transaction cap ${cap:,.2f}. "
            f"Rejected before API call."
        )
