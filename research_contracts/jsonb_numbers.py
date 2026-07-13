"""Strict native numbers that preserve type and value through PostgreSQL JSONB."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any


def jsonb_stable_number(value: Any) -> int | float | None:
    """Return a finite native number only when JSONB preserves its identity.

    The research hashes distinguish ``int`` from ``float`` and preserve the
    sign of float zero. PostgreSQL JSONB normalizes ``-0.0`` to ``0.0`` and
    scientific integral floats such as ``1e20`` to JSON integer literals, so
    accepting either would make an artifact or replay hash change after storage.
    Oversized integers that cannot be checked as finite also fail closed.
    """
    if type(value) not in {int, float}:
        return None
    try:
        finite = math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not finite:
        return None
    if type(value) is float:
        if value == 0.0 and math.copysign(1.0, value) < 0:
            return None
        # A non-negative decimal exponent is emitted by PostgreSQL as an
        # integer literal. Python would then decode it as ``int``, changing the
        # typed canonical hash even though its mathematical value is equal.
        if Decimal(repr(value)).as_tuple().exponent >= 0:
            return None
    return value
