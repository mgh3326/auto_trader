"""AT-3846: keep derived values inside their ``Numeric(p, s)`` column.

PostgreSQL rejects a value that does not fit ``NUMERIC(p, s)`` ("numeric field
overflow"), and one such value aborts the whole multi-row upsert chunk. A derived
figure that cannot fit (e.g. ``payout_ratio`` for a tiny positive net income) is
excluded instead: the field becomes ``None`` — the same fail-closed treatment a
missing figure gets — the row is still written, and the caller gets a per-field count.

Limits are read from the model's column types, so there is no second copy of the
precision to drift from the schema.
"""

from __future__ import annotations

import decimal
from collections import Counter
from decimal import Decimal

from sqlalchemy import Numeric

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsUpsert,
)

# (precision, scale) of every Numeric column of the snapshot table.
_NUMERIC_COLUMNS: dict[str, tuple[int, int]] = {
    col.name: (col.type.precision, col.type.scale)
    for col in FinancialFundamentalsSnapshot.__table__.columns
    if isinstance(col.type, Numeric)
    and col.type.precision is not None
    and col.type.scale is not None
}

# Enough digits for quantize() on the widest column (NUMERIC(30, 2)); the default
# 28-digit context would raise InvalidOperation for those.
_CTX = decimal.Context(prec=80)


def column_limit(field: str) -> Decimal:
    """Exclusive absolute bound: a value fits iff ``|round(value, scale)| < limit``."""
    precision, scale = _NUMERIC_COLUMNS[field]
    return Decimal(10) ** (precision - scale)


def fits_column(field: str, value: Decimal) -> bool:
    _, scale = _NUMERIC_COLUMNS[field]
    if not value.is_finite():
        return False
    # Beyond the bound already (rounding never moves a value below an exactly
    # representable limit). Checked first: quantize() raises InvalidOperation for a
    # value like 1E+100 that needs more digits than the context allows.
    if abs(value) >= column_limit(field):
        return False
    # PostgreSQL rounds to the column scale first, so 9999.9999996 → 10000.000000.
    rounded = value.quantize(Decimal(1).scaleb(-scale), context=_CTX)
    return abs(rounded) < column_limit(field)


def bound_numeric_fields(
    row: FinancialFundamentalsUpsert,
) -> tuple[FinancialFundamentalsUpsert, dict[str, int]]:
    """Return ``row`` with non-fitting Numeric fields set to ``None`` + ``{field: 1}``."""
    overflowed: dict[str, int] = {}
    update: dict[str, None] = {}
    for field in _NUMERIC_COLUMNS:
        value = getattr(row, field, None)
        if value is not None and not fits_column(field, value):
            update[field] = None
            overflowed[field] = 1
    if not update:
        return row, {}
    return row.model_copy(update=update), overflowed


def bound_rows(
    rows: list[FinancialFundamentalsUpsert],
) -> tuple[list[FinancialFundamentalsUpsert], dict[str, int], list[str]]:
    """Bound every row. Returns (rows, per-field counts, per-occurrence details)."""
    out: list[FinancialFundamentalsUpsert] = []
    counts: Counter[str] = Counter()
    details: list[str] = []
    for row in rows:
        fixed, overflowed = bound_numeric_fields(row)
        for field in overflowed:
            details.append(
                f"{row.symbol} {row.fiscal_period} {field}="
                f"{getattr(row, field)} does not fit NUMERIC; set to None"
            )
        counts.update(overflowed)
        out.append(fixed)
    return out, dict(counts), details


def overflow_warnings(counts: dict[str, int]) -> list[str]:
    """One counted warning per field."""
    return [
        f"numeric overflow: field={field} count={count} (excluded, set to None)"
        for field, count in sorted(counts.items())
    ]
