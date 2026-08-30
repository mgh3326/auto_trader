"""Broker-observed + durable cumulative parking exposure (§163차).

§163차/§170차 bound a parking rung twice: the per-order cap, RAISED for
parking but never removed, and behind it a native-currency cumulative parking
exposure cap. This module produces only the number that *second* boundary
compares against, selecting the existing KIS US or domestic balance surface
from the immutable symbol×account×market scope.

🔴 This is the second line, not the only one. Its measurement has known
residual gaps (runbook §8.7 — BL-37 account labelling, BL-38 pre-submit
reservation, BL-39 cross-day window), and what keeps every one of them bounded
is the per-order cap that still runs above it.

Two halves, and both are required
---------------------------------
1. **Broker balance** -- US uses ``KISClient.fetch_my_us_stocks()`` with
   ``currency_code="USD"``, summing ``ovrs_stck_evlu_amt`` over ``ovrs_pdno``;
   KR uses ``KISClient.fetch_my_stocks()``, summing domestic ``evlu_amt`` over
   ``pdno``. Both are existing KIS balance surfaces already used for holdings
   checks, so this introduces no new credential, host, or account.

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
circuit breaker. A parking buy auto-approved before that boundary and still
unfilled and still absent from the balance is not counted.

🔴 That boundary does NOT align with the US trading session, and an earlier
version of this comment justified it with ROB-671 ("US day orders do not
survive the session"). That justification is **withdrawn**: it does not
describe what this window does. KST midnight is 15:00 UTC, while XNYS regular
hours are 13:30-20:00 UTC (EDT) / 14:30-21:00 UTC (EST) -- so **KST midnight
falls in the middle of the US session**, and a parking buy auto-approved at
23:00 KST is excluded from this sum an hour later at 00:01 KST while the same
XNYS session is still open and the order is still live.

This is a deliberate choice to reuse the daily circuit breaker's already-vetted
window, not a property the market calendar gives us. It is BL-39 (backlog, not
fixed here). What contains it is the per-order cap, which bounds one automation
error at ``PARKING_PER_ORDER_CAP_USD`` per order regardless.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.parking_allowlist import (
    ParkingExposure,
    ParkingScope,
    canonical_exposure_symbol,
    is_parking_exposure_symbol,
    parking_scope,
)

logger = logging.getLogger(__name__)

# A US read pins ``TR_CRCY_CD=USD``; domestic KIS balance is a KRW surface.
# The selected ParkingScope holds the corresponding native KIS fields and cap
# currency. No branch converts, combines, or substitutes currencies.
_CURRENCY_FIELDS = ("tr_crcy_cd", "crcy_cd", "currency_code", "curr_cd")


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def _row_currency_mismatch(row: Mapping[str, Any], *, expected_currency: str) -> bool:
    """True when the row names a currency other than the scope's native one.

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
        if stripped.upper() != expected_currency:
            return True
    return False


def _currency_mismatch_reason(scope: ParkingScope) -> str:
    return "currency_not_usd" if scope.currency == "USD" else "currency_not_krw"


def sum_parking_exposure(rows: Any, *, scope: ParkingScope) -> ParkingExposure:
    """Pure reducer over broker balance rows. Fails closed on any anomaly."""
    if not isinstance(rows, list):
        return ParkingExposure.unavailable("payload_not_a_list")
    total = Decimal("0")
    for row in rows:
        if not isinstance(row, Mapping):
            return ParkingExposure.unavailable("row_not_a_mapping")
        raw_symbol = row.get(scope.balance_symbol_field)
        # A row whose symbol cannot be normalized at all cannot be *proven*
        # non-parking, so it fails closed rather than being skipped. Only a
        # cleanly readable, definitely-not-parking symbol is passed over.
        if raw_symbol is None or canonical_exposure_symbol(raw_symbol) is None:
            return ParkingExposure.unavailable("symbol_unreadable")
        if not is_parking_exposure_symbol(
            raw_symbol, account_mode=scope.account_mode, market=scope.market
        ):
            continue
        if _row_currency_mismatch(row, expected_currency=scope.currency):
            return ParkingExposure.unavailable(_currency_mismatch_reason(scope))
        raw_amount = row.get(scope.balance_evaluation_field)
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
    fetch_kr_holdings: Any = None,
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
    scope = parking_scope(symbol=symbol, account_mode=account_mode, market=market)
    if scope is None:
        return ParkingExposure.unavailable("not_requested")

    if durable_notional_fn is None:
        return ParkingExposure.unavailable("durable_reader_missing")

    fetcher = fetch_us_holdings if scope.market == "equity_us" else fetch_kr_holdings
    if fetcher is None:
        # Imported lazily so this module stays importable (and the pure reducer
        # stays testable) without the broker client's settings surface.
        from app.services.brokers.kis.client import KISClient

        # Every scope currently admits kis_live only, so no mock/paper branch
        # can be selected. The market-bound reader preserves native currency.
        fetcher = (
            KISClient().fetch_my_us_stocks
            if scope.market == "equity_us"
            else KISClient().fetch_my_stocks
        )

    try:
        rows = await fetcher()
    except Exception as exc:  # noqa: BLE001 - an unreadable balance is not a clearance
        logger.warning("parking exposure fetch failed: %s", type(exc).__name__)
        return ParkingExposure.unavailable("fetch_failed")
    held = sum_parking_exposure(rows, scope=scope)
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
