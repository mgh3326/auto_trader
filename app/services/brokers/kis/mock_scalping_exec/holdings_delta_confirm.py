"""ROB-341 — synchronous holdings/cash-delta fill confirmation for KIS mock.

Primary same-day fill signal is the baseline-vs-post **holdings delta** (load
bearing). **Cash delta** is corroboration plus the preferred fill-price source.
This module performs the broker-facing async snapshot reads and orchestration;
the delta -> verdict decision lives in the pure ``classify_fill_by_delta``
kernel (shared with the ROB-102 reconciler).

``inquire_daily_order_domestic`` (daily-ccld) is never consulted here: it can
return empty rows for same-day mock fills, so an empty same-day daily-ccld can
neither gate nor override this verdict. daily-ccld remains supplementary,
post-settlement evidence only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal


def derive_fill_price(
    *,
    side: Literal["buy", "sell"],
    filled_qty: Decimal,
    cash_baseline: Decimal | None,
    cash_observed: Decimal | None,
    limit_price: Decimal,
) -> tuple[Decimal, str]:
    """Derive a fill price for PnL telemetry.

    Prefer the cash delta (``|Δcash| / filled_qty``); fall back to the submitted
    limit price when cash is unavailable, unmoved, or qty is zero. Returns
    ``(price, source)`` where ``source`` is ``"cash_delta"`` or
    ``"limit_fallback"``.
    """
    if cash_baseline is not None and cash_observed is not None and filled_qty > 0:
        cash_delta = abs(cash_observed - cash_baseline)
        if cash_delta > 0:
            return cash_delta / filled_qty, "cash_delta"
    return limit_price, "limit_fallback"
