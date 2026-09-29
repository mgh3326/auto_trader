"""#969 — KRX after-market evidence for the Toss order-tool NXT preflight.

Both preflight callers (``orders_toss_variants._nxt_preflight_context`` and
``account_routing_tools.suggest_order_account_impl``) go through
``evaluate_nxt_preflight_with_krx_after`` so they return the same verdict.

No second rule lives here. The window is ``kr_krx_after_session_for`` (Toss
integrated afterMarket clipped to 16:00-20:00 KST) and the capability is the
#925 approval-window resolver ``resolve_krx_after_capability`` — the same two
functions ``approval_window._resolve_kr_session`` uses.
"""

from __future__ import annotations

import datetime as dt
import logging

from app.services.brokers.toss.market_calendar import (
    KrTossSession,
    get_toss_market_calendar,
    kr_krx_after_session_for,
)
from app.services.nxt_preflight import (
    KrxAfterEvidence,
    NxtPreflightVerdict,
    NxtTradability,
    evaluate_nxt_preflight,
    needs_krx_after_evidence,
)
from app.services.order_proposals.approval_window import (
    resolve_krx_after_capability,
)

logger = logging.getLogger(__name__)

_KST = dt.timezone(dt.timedelta(hours=9))

UNKNOWN_NXT_TRADABILITY = NxtTradability(
    nxt_eligible=False, nxt_trading_suspended=None, asof=None
)

_OUTSIDE_WINDOW = KrxAfterEvidence(
    in_window=False, allow=False, detail="outside_krx_after_window"
)


async def resolve_krx_after_evidence(
    symbol: str, *, now: dt.datetime
) -> KrxAfterEvidence:
    """Window + capability for one symbol. Never raises, never allows unknown.

    A calendar that cannot be read yields ``in_window=False``, which leaves
    the NXT block in place (fail-closed). Inside the window the capability
    comes from the #925 resolver, which turns lookup errors into a
    not-allowed detail itself.
    """
    local = now.astimezone(_KST) if now.tzinfo is not None else now.replace(tzinfo=_KST)
    try:
        calendar = await get_toss_market_calendar("kr", local.date())
        in_window = (
            calendar is not None
            and kr_krx_after_session_for(local, calendar=calendar) is not None
        )
    except Exception as exc:  # noqa: BLE001 - unknown window keeps the NXT block
        logger.warning(
            "KRX after-market window unavailable for %s (keeps NXT block): %s",
            symbol,
            exc,
        )
        return _OUTSIDE_WINDOW
    if not in_window:
        return _OUTSIDE_WINDOW
    try:
        allow, detail = await resolve_krx_after_capability(symbol, now=local)
    except Exception:  # noqa: BLE001 - must not reach a caller's fail-open except
        return KrxAfterEvidence(
            in_window=True, allow=False, detail="krx_after_capability_lookup_failed"
        )
    return KrxAfterEvidence(in_window=True, allow=allow is True, detail=detail)


async def evaluate_nxt_preflight_with_krx_after(
    symbol: str,
    session: KrTossSession | None,
    tradability: NxtTradability | None,
    *,
    now: dt.datetime,
) -> NxtPreflightVerdict:
    """The one preflight verdict for a KR symbol, shared by every caller.

    ``tradability`` None (symbol absent from the active universe) reads as not
    NXT-tradable, exactly as the Toss order tools always treated it.
    """
    if tradability is None:
        tradability = UNKNOWN_NXT_TRADABILITY
    krx_after: KrxAfterEvidence | None = None
    if needs_krx_after_evidence(session, tradability):
        krx_after = await resolve_krx_after_evidence(symbol, now=now)
    return evaluate_nxt_preflight(session, tradability, krx_after)


__all__ = [
    "UNKNOWN_NXT_TRADABILITY",
    "evaluate_nxt_preflight_with_krx_after",
    "resolve_krx_after_evidence",
]
