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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.services.brokers.kis.mock_scalping_exec.executor import Fill
from app.services.kis_mock_holdings_reconciler import classify_fill_by_delta

logger = logging.getLogger("rob341.kis_mock_holdings_delta")

# (symbol) -> (observed_holdings_qty, observed_cash | None)
PostFetch = Callable[[str], Awaitable[tuple[Decimal, Decimal | None]]]


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


@dataclass
class BaselineSnapshot:
    """Holdings + cash captured immediately before a mock submit.

    ``holdings_qty is None`` means the baseline read failed: confirmation then
    fails closed (we cannot prove a delta against an unknown baseline).
    """

    symbol: str
    side: Literal["buy", "sell"]
    ordered_qty: Decimal
    limit_price: Decimal
    holdings_qty: Decimal | None
    cash: Decimal | None


async def confirm_fill_from_holdings_delta(
    baseline: BaselineSnapshot,
    *,
    fetch_post: PostFetch,
) -> Fill | None:
    """Return a ``Fill`` only when the post-submit holdings delta unambiguously
    proves a (full or partial) fill in the order's direction.

    Every other outcome is fail-closed (``None``): missing baseline, snapshot
    read failure, or a zero / wrong-direction delta. Never fabricates a fill.
    """
    if baseline.holdings_qty is None:
        logger.info(
            "kis-mock holdings-delta confirm: baseline missing sym=%s -> fail closed",
            baseline.symbol,
        )
        return None
    try:
        observed_qty, observed_cash = await fetch_post(baseline.symbol)
    except Exception as exc:  # noqa: BLE001 - any read fault fails closed
        logger.info(
            "kis-mock holdings-delta confirm: post-snapshot error sym=%s: %s",
            baseline.symbol,
            exc,
        )
        return None

    decision = classify_fill_by_delta(
        side=baseline.side,
        ordered_qty=baseline.ordered_qty,
        baseline_qty=baseline.holdings_qty,
        observed_qty=observed_qty,
    )
    if decision.verdict == "none":
        logger.info(
            "kis-mock holdings-delta confirm: no fill delta sym=%s delta=%s -> fail closed",
            baseline.symbol,
            decision.delta,
        )
        return None

    price, price_source = derive_fill_price(
        side=baseline.side,
        filled_qty=decision.filled_qty,
        cash_baseline=baseline.cash,
        cash_observed=observed_cash,
        limit_price=baseline.limit_price,
    )
    logger.info(
        "kis-mock holdings-delta confirm: %s sym=%s qty=%s price=%s (%s)",
        decision.verdict,
        baseline.symbol,
        decision.filled_qty,
        price,
        price_source,
    )
    return Fill(price=price, quantity=decision.filled_qty)
