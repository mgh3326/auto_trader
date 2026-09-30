"""#1067 -- KIS quote fallback for a Toss preview that came back without a price.

The auto-approve classifier reads ``current_price`` from the fresh Toss
preview.  That value is sometimes momentarily empty (a transient
``client.prices`` failure; #1053 stores the preview's own diagnostic), and
before #1067 the proposal was then demoted to a human card as
``price_or_quantity_missing`` even though every gate would have passed.

This module supplies a *substitute input*, never a relaxed gate:

* ``classify_kis_quote`` is pure.  A quote is usable only when it is the
  ``get_quote`` payload for the **same symbol and market**, sourced from KIS,
  ``is_stale_price is False`` (strict identity: an absent flag is not fresh),
  ``data_state == "fresh"`` and a finite positive price.  Freshness rule, as
  implemented by ``get_quote``: a KRX regular-session quote is fresh when its
  candle date is today's KST trading date *and* the KRX regular session is
  trading right now (``kr_market_data_state``); during an NXT session
  (NXT premarket included) the quote is the NXT orderbook overlay and is
  fresh only if that orderbook is at most 5 minutes old
  (``ORDERBOOK_ASOF_MAX_AGE_S148_N5``).  Anything else -- a KRX-only
  premarket base quote, after close without a fresh NXT overlay, holiday, a
  prior-day candle -- is rejected.  The KRX regular-session proof is
  date-level (a fresh read of today's candle), not an intraday age proof.
* ``fetch_kis_quote_fallback`` bounds the read with a timeout and turns every
  failure into a closed reason code.  Only ``equity_kr`` is supported: the US
  ``get_quote`` path carries no ``is_stale_price`` flag and can fall back to
  Yahoo, so it can never satisfy the freshness contract above.

The classifier runs every existing gate unchanged on the substituted price;
see ``evaluate_auto_approve_eligibility``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

PRICE_SOURCE_TOSS_PREVIEW = "toss_preview"
PRICE_SOURCE_KIS_QUOTE_FALLBACK = "kis_quote_fallback"
PRICE_SOURCES = frozenset({PRICE_SOURCE_TOSS_PREVIEW, PRICE_SOURCE_KIS_QUOTE_FALLBACK})

# Closed failure vocabulary; the audit projector allowlists exactly this set.
PRICE_FALLBACK_FAILURE_REASONS = frozenset(
    {
        "market_unsupported",
        "quote_timeout",
        "quote_unavailable",
        "symbol_mismatch",
        "market_mismatch",
        "source_mismatch",
        "quote_stale",
        "freshness_unavailable",
        "session_not_live",
        "price_invalid",
    }
)

PRICE_FALLBACK_TIMEOUT_SECONDS = 5.0

QuoteFn = Callable[[str, str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PriceFallback:
    """Outcome of one fallback read: a usable price or a closed reason."""

    price: Decimal | None
    failure_reason: str | None

    @classmethod
    def observed(cls, price: Decimal) -> PriceFallback:
        return cls(price=price, failure_reason=None)

    @classmethod
    def failed(cls, reason: str) -> PriceFallback:
        if reason not in PRICE_FALLBACK_FAILURE_REASONS:
            raise ValueError("unknown price fallback failure reason")
        return cls(price=None, failure_reason=reason)


def preview_current_price_absent(preview: Any) -> bool:
    """True only when the preview omitted ``current_price`` or sent null/blank.

    A present but malformed or non-positive value is *not* absent: it still
    rejects as ``price_or_quantity_missing`` exactly as before #1067, because
    a garbage broker price is not the transient-empty case this fallback is
    for.
    """
    if not isinstance(preview, Mapping):
        return False
    value = preview.get("current_price")
    return value is None or (isinstance(value, str) and not value.strip())


def classify_kis_quote(quote: Any, *, symbol: str, market: str) -> PriceFallback:
    """Accept a ``get_quote`` payload only if it is fresh and for this rung."""
    if not isinstance(quote, Mapping) or "error" in quote:
        return PriceFallback.failed("quote_unavailable")
    if quote.get("symbol") != symbol:
        return PriceFallback.failed("symbol_mismatch")
    if quote.get("instrument_type") != market:
        return PriceFallback.failed("market_mismatch")
    if quote.get("source") != "kis":
        return PriceFallback.failed("source_mismatch")
    stale = quote.get("is_stale_price")
    if stale is True:
        return PriceFallback.failed("quote_stale")
    if stale is not False:
        return PriceFallback.failed("freshness_unavailable")
    if quote.get("data_state") != "fresh":
        return PriceFallback.failed("session_not_live")
    raw_price = quote.get("price")
    if isinstance(raw_price, bool):
        return PriceFallback.failed("price_invalid")
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, TypeError, ValueError):
        return PriceFallback.failed("price_invalid")
    if not price.is_finite() or price <= 0:
        return PriceFallback.failed("price_invalid")
    return PriceFallback.observed(price)


async def _default_quote_fn(symbol: str, market: str) -> Any:
    # Lazy: the MCP tooling module pulls in broker clients at import time.
    from app.mcp_server.tooling.market_data_quotes import _get_quote_impl

    return await _get_quote_impl(symbol, market)


async def fetch_kis_quote_fallback(
    *,
    symbol: Any,
    market: Any,
    quote_fn: QuoteFn | None = None,
    timeout_seconds: float = PRICE_FALLBACK_TIMEOUT_SECONDS,
) -> PriceFallback:
    """Read one KIS quote through the ``get_quote`` path; never raises."""
    if market != "equity_kr":
        return PriceFallback.failed("market_unsupported")
    if not isinstance(symbol, str) or not symbol:
        return PriceFallback.failed("symbol_mismatch")
    fetch = quote_fn or _default_quote_fn
    try:
        quote = await asyncio.wait_for(fetch(symbol, market), timeout_seconds)
    except TimeoutError:
        return PriceFallback.failed("quote_timeout")
    except Exception:  # noqa: BLE001 - a failed read is a closed reason
        return PriceFallback.failed("quote_unavailable")
    return classify_kis_quote(quote, symbol=symbol, market=market)


__all__ = [
    "PRICE_FALLBACK_FAILURE_REASONS",
    "PRICE_FALLBACK_TIMEOUT_SECONDS",
    "PRICE_SOURCES",
    "PRICE_SOURCE_KIS_QUOTE_FALLBACK",
    "PRICE_SOURCE_TOSS_PREVIEW",
    "PriceFallback",
    "classify_kis_quote",
    "fetch_kis_quote_fallback",
    "preview_current_price_absent",
]
