"""Operator-set ``user_settings.parking_exclusion`` — parsing and write guard (#883).

The cash-sweep playbook (operator #879) reads this key through the typed,
read-only ``get_parking_exclusion`` MCP tool on the kr/us execution lanes to
leave a declared amount of cash unparked per currency. The value is fail-closed
by construction:

* the accepted shape is a flat object whose keys are a subset of
  ``PARKING_EXCLUSION_CURRENCIES`` and whose values are non-negative, finite
  JSON numbers or decimal strings, e.g. ``{"KRW": 500000, "USD": "100.50"}``;
* *any* deviation — a non-object value, an unknown currency key, a bool, a
  nested object/list, a negative or non-finite amount — parses to ``None``,
  which the reader surfaces as the closed ``"unknown"`` status. A malformed
  value is never interpreted as zero: an unknown exclusion parks nothing;
* a currency simply absent from a well-formed value keeps its default of 0
  (park everything for that currency), matching the documented absent-row
  default.

Writing stays operator-only: there is no settings UI, so the only write path
is ``set_user_setting("parking_exclusion", ...)`` on the default profile
(which no execution lane exposes). ``normalize_generic_parking_exclusion_write``
validates the same closed shape on that path and canonicalizes amounts to
plain decimal strings so a typo cannot be stored and silently read as
``"unknown"`` later.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

PARKING_EXCLUSION_KEY = "parking_exclusion"

# Closed currency set — the sweep only ever parks KRW (kr lane) and USD
# (us lane). An unrecognized key makes the whole value malformed.
PARKING_EXCLUSION_CURRENCIES: tuple[str, ...] = ("KRW", "USD")


class ParkingExclusionValidationError(ValueError):
    """The submitted parking_exclusion value is not the closed shape."""


def _parse_amount(raw: Any) -> Decimal | None:
    """Strict non-negative finite amount; bool/dict/list/str-garbage → None."""

    if raw is None or isinstance(raw, (bool, dict, list)):
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def parse_parking_exclusion_value(value: Any) -> dict[str, Decimal] | None:
    """Parse a stored ``parking_exclusion`` JSON value.

    Returns the per-currency mapping for the currencies present in the value,
    or ``None`` for *any* malformation — one bad key or amount poisons the
    whole read, so a partially-valid object cannot be silently applied.
    """

    if not isinstance(value, dict):
        return None
    parsed: dict[str, Decimal] = {}
    for currency, raw in value.items():
        if currency not in PARKING_EXCLUSION_CURRENCIES:
            return None
        amount = _parse_amount(raw)
        if amount is None:
            return None
        parsed[currency] = amount
    return parsed


def normalize_generic_parking_exclusion_write(value: Any) -> dict[str, Any]:
    """Guard ``set_user_setting("parking_exclusion", …)`` — the operator write path.

    Raises ``ParkingExclusionValidationError`` on any malformation and returns
    the canonical form ``{<currency>: <plain decimal string>}`` otherwise.
    """

    parsed = parse_parking_exclusion_value(value)
    if parsed is None:
        raise ParkingExclusionValidationError(
            'parking_exclusion must be an object like {"KRW": 0, "USD": "0"} '
            f"with keys a subset of {list(PARKING_EXCLUSION_CURRENCIES)} and "
            "non-negative finite number or decimal-string amounts"
        )
    return {
        currency: format(amount, "f") for currency, amount in sorted(parsed.items())
    }


__all__ = [
    "PARKING_EXCLUSION_CURRENCIES",
    "PARKING_EXCLUSION_KEY",
    "ParkingExclusionValidationError",
    "normalize_generic_parking_exclusion_write",
    "parse_parking_exclusion_value",
]
