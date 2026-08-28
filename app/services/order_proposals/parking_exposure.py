"""Broker-observed + durable cumulative parking exposure (§163차).

§163차 bounds a parking rung twice: the per-order cap, RAISED for parking but
never removed (``PARKING_PER_ORDER_CAP_USD``), and behind it a cumulative
parking exposure cap. This module produces the number the *second* boundary
compares against, and nothing else: the USD cumulative parking exposure on the
one account the allowlist admits (``kis_live`` / ``equity_us``).

🔴 This is the second line, not the only one. Its measurement has known
residual gaps (runbook §8.7 — BL-37 account labelling, BL-38 pre-submit
reservation, BL-39 cross-day window), and what keeps every one of them bounded
is the per-order cap that still runs above it.

Two halves, and both are required
---------------------------------
1. **Broker balance** -- ``KISClient.fetch_my_us_stocks()`` ->
   ``inquire-balance`` with ``currency_code="USD"``, summing
   ``ovrs_stck_evlu_amt`` (해외주식평가금액) over rows matched on
   ``ovrs_pdno``. Same client and balance surface
   ``order_validation._get_holdings_for_order`` already uses for the avg-cost
   loss guard, so this introduces no new credential, host or account.

2. **Durable same-day auto-approved parking buys** -- 🔴 the balance alone is
   NOT a cumulative boundary. ``KISAccount._filter_nonzero_holdings`` keeps
   only rows with ``ovrs_cblc_qty > 0``, so an order that was auto-approved and
   sent but has **not filled yet** has no balance row at all. A balance-only
   cap therefore re-meters the very next proposal from zero and approves the
   same amount again -- two separate USD 10,000 proposals both clear, for USD
   20,000 of automation. The durable half closes exactly that window, reusing
   the KST-day window, advisory lock and row filter of the already-vetted
   ``auto_approved_daily_notional``.

Double counting is deliberate
-----------------------------
Once a parking buy fills, it appears in the balance AND is still inside the
durable same-day window, so it is counted twice until the KST day rolls over.
That is the chosen direction. De-duplicating would require matching fills back
to the orders that produced them, and any error in *that* matching restores the
under-count this module exists to prevent. Over-counting only tightens the cap
-- it can refuse an auto-approval that would have been allowed, which costs a
Telegram tap. Under-counting submits money that was never authorized.

Provenance -- and why it cannot be injected
-------------------------------------------
Nothing a proposal, MCP session or preview can write reaches this figure. The
proposal contributes exactly one input to the whole §163차 path -- the symbol --
and that same symbol is what the order is submitted against, so claiming
``SGOV`` to obtain the parking treatment submits a real ``SGOV`` order. There is no
proposer-supplied notional, position, valuation or exposure field anywhere in
this computation. The durable half reads the same vetted per-rung cap measure
the daily circuit breaker reads (booked ``limit_price × quantity``, or the
stronger recorded observation), never a proposer-supplied advisory number.

Fail-closed
-----------
Every anomaly returns ``ParkingExposure.unavailable(...)``, and the classifier
turns that into a rejected auto-approval (a human card): a raised or timed-out
balance fetch, a payload that is not a list, a row that is not a mapping, an
allowlisted row whose symbol or evaluation amount cannot be read, an amount
that is not finite, a negative amount, a row that declares a currency other
than USD, a missing durable reader, and a durable read that raises or returns
an unusable value.

Known residual (documented, not silently absorbed)
--------------------------------------------------
See docs/runbooks/order-proposal-auto-approve-expand.md §8.7. The load-bearing
one: the durable half is scoped to the **current KST day**, matching the daily
circuit breaker. A parking buy auto-approved on an earlier day that is still
unfilled and still absent from the balance is not counted. US day orders do not
survive the session (ROB-671), which is what makes the same-day window the
right one here -- but it is a window, not a proof.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.parking_allowlist import (
    ParkingExposure,
    canonical_exposure_symbol,
    is_parking_allowlisted,
    is_parking_exposure_symbol,
)

logger = logging.getLogger(__name__)

# The KIS overseas balance field carrying per-position market value in the
# balance's own currency. Not a proposal field.
_EVALUATION_AMOUNT_FIELD = "ovrs_stck_evlu_amt"
_SYMBOL_FIELD = "ovrs_pdno"

# The request pins ``TR_CRCY_CD=USD``, so the amounts are expected in USD --
# the same currency as ``per_order_cap.us`` / ``daily_cap.us``, which is what
# makes the comparison FX-free. If a row *declares* its own currency and that
# currency is not USD, the assumption is broken and summing the number anyway
# would compare a non-USD amount against a USD cap. Whether KIS actually emits
# any of these keys on an overseas balance row is UNVERIFIED here; this is a
# guard against the assumption being wrong, not a claim that it is.
_CURRENCY_FIELDS = ("tr_crcy_cd", "crcy_cd", "currency_code", "curr_cd")
_EXPECTED_CURRENCY = "USD"


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def _row_currency_mismatch(row: Mapping[str, Any]) -> bool:
    """True when the row names a currency and it is not USD.

    A row that declares nothing is left alone: the request pinned USD, and
    rejecting every row that omits an optional field would fail closed on
    normal traffic rather than on a real anomaly.
    """
    for field in _CURRENCY_FIELDS:
        declared = row.get(field)
        if declared is None:
            continue
        if not isinstance(declared, str):
            return True
        stripped = declared.strip()
        if not stripped:
            continue
        if stripped.upper() != _EXPECTED_CURRENCY:
            return True
    return False


def sum_parking_exposure(rows: Any) -> ParkingExposure:
    """Pure reducer over broker balance rows. Fails closed on any anomaly."""
    if not isinstance(rows, list):
        return ParkingExposure.unavailable("payload_not_a_list")
    total = Decimal("0")
    for row in rows:
        if not isinstance(row, Mapping):
            return ParkingExposure.unavailable("row_not_a_mapping")
        raw_symbol = row.get(_SYMBOL_FIELD)
        # A row whose symbol cannot be normalized at all cannot be *proven*
        # non-parking, so it fails closed rather than being skipped. Only a
        # cleanly readable, definitely-not-parking symbol is passed over.
        if raw_symbol is None or canonical_exposure_symbol(raw_symbol) is None:
            return ParkingExposure.unavailable("symbol_unreadable")
        if not is_parking_exposure_symbol(raw_symbol):
            continue
        if _row_currency_mismatch(row):
            return ParkingExposure.unavailable("currency_not_usd")
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
    durable_notional_fn: Any = None,
) -> ParkingExposure:
    """Cumulative parking exposure for an allowlisted ``kis_live`` US order.

    ``durable_notional_fn`` is an awaitable returning the account's same-day
    auto-approved parking buy notional. It is **required** for a parking group:
    without it the measurement is balance-only, which is exactly the gap this
    exists to close, so its absence fails closed rather than silently
    degrading to the broken measure.

    Returns ``unavailable("not_requested")`` when the group is not an
    allowlisted parking order, so a caller that asks unconditionally still
    cannot obtain parking treatment for a non-parking symbol.
    """
    if not is_parking_allowlisted(
        symbol=symbol, account_mode=account_mode, market=market
    ):
        return ParkingExposure.unavailable("not_requested")

    if durable_notional_fn is None:
        return ParkingExposure.unavailable("durable_reader_missing")

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
    held = sum_parking_exposure(rows)
    if not held.available or held.exposure is None:
        return held

    try:
        pending_raw = await durable_notional_fn()
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger is not a clearance
        logger.warning("parking durable read failed: %s", type(exc).__name__)
        return ParkingExposure.unavailable("durable_read_failed")
    pending = _decimal(pending_raw)
    if pending is None or pending < 0:
        return ParkingExposure.unavailable("durable_notional_invalid")

    # Deliberate sum, not a merge -- see the module docstring. A filled
    # same-day buy is counted in both halves until the KST day rolls over, and
    # that over-count is the safe direction.
    return ParkingExposure.observed(held.exposure + pending)


__all__ = ["load_parking_exposure", "sum_parking_exposure"]
