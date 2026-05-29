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

**Attribution boundary (ROB-341 §4, known limitation).** The delta attributes
*any* directional holdings change in the poll window to *this* order — it cannot
distinguish this order's fill from a concurrent same-symbol fill. Two mitigations
bound the risk: (1) the scalping ws_bridge serializes per-symbol entries via an
in-flight set + global semaphore, so two concurrent scalps on the same symbol do
not overlap; (2) a wrong-direction or zero delta already fails closed. The
residual, un-disambiguated case is external/manual same-symbol account activity
during the bounded poll; holdings alone cannot resolve it, and a future
ledger/open-order cross-check is the deeper fix (tracked as follow-up). Keep the
confirmed smoke to idle symbols with no concurrent manual activity.
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
    """Return a ``Fill`` only when the post-submit holdings delta proves a
    **full** fill in the order's direction.

    A partial delta is treated as not-yet-filled and returns ``None`` so the
    executor's bounded poll either reaches a full fill or times out into an
    ``entry_unfilled`` / ``exit_unconfirmed`` anomaly — the executor has no
    partial-fill handling, so confirming a partial would strand the residual
    position and fabricate full-size PnL. (The periodic ROB-102 reconciler
    keeps its own partial semantics via the shared kernel.)

    Every non-full outcome is fail-closed (``None``): missing baseline, snapshot
    read failure, a zero / wrong-direction delta, or a partial. Never fabricates
    a fill.
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
    if decision.verdict != "filled":
        # 'none' (no/wrong-direction delta) or 'partial' (not yet fully filled)
        # both fail closed; on a partial the bounded poll keeps trying.
        logger.info(
            "kis-mock holdings-delta confirm: verdict=%s (not full) sym=%s "
            "delta=%s filled=%s/%s -> fail closed",
            decision.verdict,
            baseline.symbol,
            decision.delta,
            decision.filled_qty,
            baseline.ordered_qty,
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
