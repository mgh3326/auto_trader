"""Broker-observed cumulative parking exposure (§163차).

The §163차 per-order cap exemption is only as safe as the number that replaces
it. This module produces that number and nothing else: the USD sum of the
market value of the allowlisted parking tickers on the one account the
allowlist admits (``kis_live`` / ``equity_us``).

Provenance -- and why it cannot be injected
-------------------------------------------
The sum is read from a fresh KIS overseas balance
(``KISClient.fetch_my_us_stocks`` -> ``inquire-balance`` with
``currency_code="USD"``), field ``ovrs_stck_evlu_amt`` (해외주식평가금액),
matched on ``ovrs_pdno``. That is the same client and the same balance surface
``order_validation._get_holdings_for_order`` already uses to source the
avg-cost loss guard, so this introduces no new credential, host or account.

Nothing a proposal, MCP session or preview can write reaches this figure. The
proposal contributes exactly one input to the whole §163차 path -- the symbol --
and that same symbol is what the order is submitted against, so claiming
``SGOV`` to obtain the exemption submits a real ``SGOV`` order. There is no
proposer-supplied notional, position, valuation or exposure field anywhere in
this computation.

Fail-closed
-----------
Every anomaly returns ``ParkingExposure.unavailable(...)``, and the classifier
turns that into a rejected auto-approval (a human card): a raised or timed-out
fetch, a payload that is not a list, a row that is not a mapping, an
allowlisted row whose symbol or evaluation amount cannot be read, an amount
that is not finite, and a negative amount.

Known residual (documented, not silently absorbed)
--------------------------------------------------
An allowlisted position that the broker does not return at all is
indistinguishable from one that is not held, and is therefore counted as zero.
That is the one direction in which this measurement can understate exposure.
It is bounded by the daily cap, which §163차 does not exempt. See
docs/runbooks/order-proposal-auto-approve-expand.md §8.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.parking_allowlist import (
    PARKING_ALLOWLIST_SYMBOLS,
    ParkingExposure,
    canonical_exposure_symbol,
    is_parking_allowlisted,
)

logger = logging.getLogger(__name__)

# The KIS overseas balance field carrying per-position market value in the
# balance's own currency (USD here). Not a proposal field.
_EVALUATION_AMOUNT_FIELD = "ovrs_stck_evlu_amt"
_SYMBOL_FIELD = "ovrs_pdno"


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def sum_parking_exposure(rows: Any) -> ParkingExposure:
    """Pure reducer over broker balance rows. Fails closed on any anomaly."""
    if not isinstance(rows, list):
        return ParkingExposure.unavailable("payload_not_a_list")
    total = Decimal("0")
    for row in rows:
        if not isinstance(row, Mapping):
            return ParkingExposure.unavailable("row_not_a_mapping")
        raw_symbol = row.get(_SYMBOL_FIELD)
        if raw_symbol is None:
            # A row without the symbol field cannot be proven non-parking.
            return ParkingExposure.unavailable("symbol_unreadable")
        symbol = canonical_exposure_symbol(raw_symbol)
        if symbol is None:
            return ParkingExposure.unavailable("symbol_unreadable")
        if symbol not in PARKING_ALLOWLIST_SYMBOLS:
            continue
        raw_amount = row.get(_EVALUATION_AMOUNT_FIELD)
        if raw_amount is None or (
            isinstance(raw_amount, str) and not raw_amount.strip()
        ):
            return ParkingExposure.unavailable("evaluation_amount_missing")
        amount = _decimal(raw_amount)
        if amount is None:
            return ParkingExposure.unavailable("evaluation_amount_invalid")
        if amount < 0:
            return ParkingExposure.unavailable("evaluation_amount_negative")
        total += amount
    return ParkingExposure.observed(total)


async def load_parking_exposure(
    *,
    account_mode: Any,
    market: Any,
    symbol: Any,
    fetch_us_holdings: Any = None,
) -> ParkingExposure:
    """Read cumulative parking exposure for an allowlisted ``kis_live`` US order.

    Returns ``unavailable("not_requested")`` when the group is not an
    allowlisted parking order, so a caller that asks unconditionally still
    cannot obtain an exemption for a non-parking symbol.
    """
    if not is_parking_allowlisted(
        symbol=symbol, account_mode=account_mode, market=market
    ):
        return ParkingExposure.unavailable("not_requested")

    fetcher = fetch_us_holdings
    if fetcher is None:
        # Imported lazily so this module stays importable (and the pure reducer
        # stays testable) without the broker client's settings surface.
        from app.services.brokers.kis.client import KISClient

        # kis_live only -- ``PARKING_ALLOWLIST_ACCOUNT_MARKETS`` admits no mock
        # or paper account mode, so there is no is_mock branch to get wrong.
        fetcher = KISClient().fetch_my_us_stocks

    try:
        rows = await fetcher()
    except Exception as exc:  # noqa: BLE001 - an unreadable balance is not a clearance
        logger.warning("parking exposure fetch failed: %s", type(exc).__name__)
        return ParkingExposure.unavailable("fetch_failed")
    return sum_parking_exposure(rows)


__all__ = ["load_parking_exposure", "sum_parking_exposure"]
