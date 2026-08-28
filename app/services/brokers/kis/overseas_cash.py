"""Availability rules for KIS overseas USD cash fields.

``TTTC2101R`` reports the cash balance and general orderable amount as
separate fields.  This module deliberately does not infer why KIS can report
zero cash alongside a positive general orderable amount.  In that state this
application cannot establish deployable USD cash, so callers must surface it
as unavailable instead of selecting one field silently.
"""

from __future__ import annotations

import math
from typing import Final

KIS_OVERSEAS_USD_BALANCE_ORDERABLE_MISMATCH: Final = (
    "kis_overseas_usd_balance_orderable_mismatch"
)


class KISOverseasUsdCashUnavailable(RuntimeError):
    """Raised when a live KIS USD buy precheck has no safe cash value."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"KIS overseas USD cash unavailable: {reason}")


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def overseas_usd_cash_unavailable_reason(
    *, balance: object, orderable: object
) -> str | None:
    """Return a stable unavailable reason for a known unsafe KIS USD shape.

    ``frcr_dncl_amt1`` and ``frcr_gnrl_ord_psbl_amt`` are independently
    reported KIS values, not aliases.  A positive orderable value does not
    establish usable cash when the reported cash balance is explicitly zero.
    Keep the condition deliberately narrow: missing/malformed values retain
    their existing read-failure handling rather than being relabelled here.
    """

    parsed_balance = _finite_float(balance)
    parsed_orderable = _finite_float(orderable)
    if parsed_balance == 0.0 and parsed_orderable is not None and parsed_orderable > 0:
        return KIS_OVERSEAS_USD_BALANCE_ORDERABLE_MISMATCH
    return None


__all__ = [
    "KIS_OVERSEAS_USD_BALANCE_ORDERABLE_MISMATCH",
    "KISOverseasUsdCashUnavailable",
    "overseas_usd_cash_unavailable_reason",
]
