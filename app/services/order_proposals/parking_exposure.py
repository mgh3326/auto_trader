"""Broker-observed + durable cumulative parking exposure (§163차).

§163차/§170차 bound a parking rung twice: the per-order cap, RAISED for
parking but never removed, and behind it a native-currency cumulative parking
exposure cap. This module produces only the number that *second* boundary
compares against, selecting the existing KIS or Toss holdings surface from the
immutable symbol×account×market scope.

🔴 This is the second line, not the only one. Its measurement has known
residual gaps (runbook §8.7 — BL-37 account labelling, BL-38 pre-submit
reservation, BL-39 cross-day window), and what keeps every one of them bounded
is the per-order cap that still runs above it.

Two halves, and both are required
---------------------------------
1. **Broker balance** -- KIS US uses ``KISClient.fetch_my_us_stocks()`` with
   ``currency_code="USD"``, summing ``ovrs_stck_evlu_amt`` over ``ovrs_pdno``;
   KIS KR uses ``KISClient.fetch_my_stocks()``, summing domestic ``evlu_amt``
   over ``pdno``. Toss KR and US both use the existing read-only
   ``TossReadClient.holdings()`` surface; the typed rows are projected to
   ``symbol`` and ``market_value.amount`` and filtered by their bound currency
   and ``market_country``. No new credential, host, or account is introduced.

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
from collections.abc import Awaitable, Callable, Mapping
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
# The selected ParkingScope holds the corresponding native fields and cap
# currency. No branch converts, combines, or substitutes currencies.
_CURRENCY_FIELDS = ("tr_crcy_cd", "crcy_cd", "currency_code", "curr_cd")

# The provider is a load-bearing part of the immutable parking scope.  Do not
# select a balance client from ``market``: both KIS and Toss have an
# ``equity_kr`` surface, but their accounts must never meter one another.
_PROVIDER_BINDINGS: dict[str, tuple[str, str, str]] = {
    "kis_us_holdings": ("kis_live", "equity_us", "USD"),
    "kis_kr_holdings": ("kis_live", "equity_kr", "KRW"),
    "toss_kr_holdings": ("toss_live", "equity_kr", "KRW"),
    "toss_us_holdings": ("toss_live", "equity_us", "USD"),
}
_TOSS_HOLDINGS_PROVIDERS = frozenset({"toss_kr_holdings", "toss_us_holdings"})


def _field_value(row: Mapping[str, Any], field: str) -> Any:
    value: Any = row
    for component in field.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(component)
    return value


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def _row_currency_mismatch(row: Mapping[str, Any], *, scope: ParkingScope) -> bool:
    """True when the row names a currency other than the scope's native one.

    A row that declares nothing is left alone: the request pinned USD, and
    rejecting every row that omits an optional field would fail closed on
    normal traffic rather than on a real anomaly.
    """
    if scope.balance_currency_field is not None:
        declared = _field_value(row, scope.balance_currency_field)
        return (
            not isinstance(declared, str) or declared.strip().upper() != scope.currency
        )

    for field in _CURRENCY_FIELDS:
        declared = row.get(field)
        if declared is None:
            continue
        if not isinstance(declared, str):
            return True
        stripped = declared.strip()
        if not stripped:
            continue
        if stripped.upper() != scope.currency:
            return True
    return False


def _currency_mismatch_reason(scope: ParkingScope) -> str:
    return "currency_not_usd" if scope.currency == "USD" else "currency_not_krw"


def _row_market_mismatch(row: Mapping[str, Any], *, scope: ParkingScope) -> bool:
    if scope.balance_market_field is None:
        return False
    declared = _field_value(row, scope.balance_market_field)
    return (
        not isinstance(declared, str)
        or declared.strip().upper() != scope.balance_market_value
    )


def _market_mismatch_reason(scope: ParkingScope) -> str:
    return "market_not_kr" if scope.balance_market_value == "KR" else "market_not_us"


def sum_parking_exposure(rows: Any, *, scope: ParkingScope) -> ParkingExposure:
    """Pure reducer over broker balance rows. Fails closed on any anomaly."""
    if not isinstance(rows, list):
        return ParkingExposure.unavailable("payload_not_a_list")
    total = Decimal("0")
    for row in rows:
        if not isinstance(row, Mapping):
            return ParkingExposure.unavailable("row_not_a_mapping")
        raw_symbol = _field_value(row, scope.balance_symbol_field)
        # A row whose symbol cannot be normalized at all cannot be *proven*
        # non-parking, so it fails closed rather than being skipped. Only a
        # cleanly readable, definitely-not-parking symbol is passed over.
        if raw_symbol is None or canonical_exposure_symbol(raw_symbol) is None:
            return ParkingExposure.unavailable("symbol_unreadable")
        if not is_parking_exposure_symbol(
            raw_symbol, account_mode=scope.account_mode, market=scope.market
        ):
            continue
        if _row_currency_mismatch(row, scope=scope):
            return ParkingExposure.unavailable(_currency_mismatch_reason(scope))
        if _row_market_mismatch(row, scope=scope):
            return ParkingExposure.unavailable(_market_mismatch_reason(scope))
        raw_amount = _field_value(row, scope.balance_evaluation_field)
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


def _toss_account_identity_matches(broker_account_id: Any) -> bool:
    """Prove the proposal's durable scope names the settings-selected account.

    ``TossReadClient.from_settings()`` always sends its configured account
    sequence (or auto-resolves one); it does not consume proposal metadata.
    The parking meter is therefore unavailable unless the proposal records the
    one exact, canonical sequence selected by settings.  This intentionally
    rejects null, whitespace, leading-zero, and opaque account labels instead
    of guessing an account-number-to-sequence mapping.
    """
    from app.core.config import settings

    configured = settings.toss_api_account_seq
    return (
        type(configured) is int
        and configured > 0
        and type(broker_account_id) is str
        and broker_account_id == str(configured)
    )


def _select_balance_fetcher(
    scope: ParkingScope,
    *,
    fetch_us_holdings: Any,
    fetch_kr_holdings: Any,
    fetch_toss_holdings: Any,
) -> Callable[[], Awaitable[Any]]:
    """Return only the reader cryptographically named by the parking scope.

    Assertions are intentional mutation alarms: if a future edit detaches a
    provider from its account×market×currency tuple, tests must go red rather
    than silently selecting an equally named but wrong broker surface.
    """
    assert _PROVIDER_BINDINGS.get(scope.balance_provider) == (
        scope.account_mode,
        scope.market,
        scope.currency,
    )

    if scope.balance_provider == "kis_us_holdings":
        if fetch_us_holdings is not None:
            return fetch_us_holdings
        from app.services.brokers.kis.client import KISClient

        return KISClient().fetch_my_us_stocks
    if scope.balance_provider == "kis_kr_holdings":
        if fetch_kr_holdings is not None:
            return fetch_kr_holdings
        from app.services.brokers.kis.client import KISClient

        return KISClient().fetch_my_stocks
    if scope.balance_provider in _TOSS_HOLDINGS_PROVIDERS:
        if fetch_toss_holdings is not None:
            return fetch_toss_holdings

        async def _read_toss_holdings() -> Any:
            # This path calls only TossReadClient.holdings(), a GET read
            # surface. It neither imports nor invokes place/modify/cancel.
            from app.services.brokers.toss.client import TossReadClient

            client = TossReadClient.from_settings()
            try:
                return await client.holdings()
            finally:
                await client.aclose()

        return _read_toss_holdings
    raise AssertionError(
        f"unsupported parking balance provider: {scope.balance_provider}"
    )


def _toss_holdings_rows(payload: Any) -> list[dict[str, Any]] | None:
    """Project the typed Toss read DTO into the scope's declared field names."""
    from app.services.brokers.toss.dto import TossHoldingItem, TossHoldings

    if not isinstance(payload, TossHoldings):
        return None
    rows: list[dict[str, Any]] = []
    for item in payload.items:
        if not isinstance(item, TossHoldingItem):
            return None
        rows.append(
            {
                "symbol": item.symbol,
                "currency": item.currency,
                "market_country": item.market_country,
                "market_value": item.market_value,
            }
        )
    return rows


async def load_parking_exposure(
    *,
    account_mode: Any,
    market: Any,
    symbol: Any,
    broker_account_id: Any = None,
    fetch_us_holdings: Any = None,
    fetch_kr_holdings: Any = None,
    fetch_toss_holdings: Any = None,
    durable_notional_fn: Any = None,
) -> ParkingExposure:
    """Cumulative exposure for an allowlisted order's bound broker account.

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

    if (
        scope.balance_provider in _TOSS_HOLDINGS_PROVIDERS
        and not _toss_account_identity_matches(broker_account_id)
    ):
        # No broker read before the account identity is proven.  In particular,
        # do not auto-resolve a Toss account to authorise an opaque proposal
        # account label.
        return ParkingExposure.unavailable("account_identity_unavailable")

    if durable_notional_fn is None:
        return ParkingExposure.unavailable("durable_reader_missing")

    fetcher = _select_balance_fetcher(
        scope,
        fetch_us_holdings=fetch_us_holdings,
        fetch_kr_holdings=fetch_kr_holdings,
        fetch_toss_holdings=fetch_toss_holdings,
    )

    try:
        payload = await fetcher()
    except Exception as exc:  # noqa: BLE001 - an unreadable balance is not a clearance
        logger.warning("parking exposure fetch failed: %s", type(exc).__name__)
        return ParkingExposure.unavailable("fetch_failed")
    rows = (
        _toss_holdings_rows(payload)
        if scope.balance_provider in _TOSS_HOLDINGS_PROVIDERS
        else payload
    )
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
