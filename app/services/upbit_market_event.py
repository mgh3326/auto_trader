"""Closed Upbit market-alert state per market (hk 6026).

Upbit deleted ``market_warning`` from ``GET /v1/market/all`` on 2024-11-20
(developer-centre notice, "종목 코드 조회 (/v1/market/all)의 market_warning 필드
삭제") and the response now carries::

    "market_event": {
        "warning": false,
        "caution": {
            "PRICE_FLUCTUATIONS": false,
            "TRADING_VOLUME_SOARING": false,
            "DEPOSIT_AMOUNT_SOARING": false,
            "GLOBAL_PRICE_DIFFERENCES": false,
            "CONCENTRATION_OF_SMALL_ACCOUNTS": false,
        },
    }

``warning`` is 유의 종목 and ``caution`` is 주의 종목 (one boolean per alert
type). The 2026-01-29 changelog pinned the caution semantics: a type is ``true``
when any alert level (주의/경고/위험) is in force. Source:
https://docs.upbit.com/kr/reference/list-trading-pairs and
https://docs.upbit.com/kr/changelog/market_event (read 2026-11-22).

This module turns one stored ``market_event`` observation into exactly one
closed state for readers:

    none | warning | caution:<TYPE,TYPE,...> | unknown | stale

``unknown`` means Upbit did not say (the field was absent or unreadable at the
observation time). ``stale`` means the stored observation is older than
:data:`MARKET_EVENT_STALE_AFTER` — judged by when Upbit was observed, never by
the row's ``updated_at``. Never turn a missing statement into ``none``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upbit_symbol_universe import UpbitSymbolUniverse

logger = logging.getLogger(__name__)

STATE_NONE = "none"
STATE_WARNING = "warning"
STATE_UNKNOWN = "unknown"
STATE_STALE = "stale"
CAUTION_STATE_PREFIX = "caution:"

#: The universe sync runs daily (Dagu ``at-batch2-upbit-symbol-universe``,
#: 21:15 UTC = 06:15 KST). One missed run plus 12 h of slack. An observation
#: older than this is ``stale`` regardless of what it says.
MARKET_EVENT_STALE_AFTER = timedelta(hours=36)

#: Closed reason codes reported beside the state.
REASON_ABSENT = "market_event_absent"
REASON_STALE = "market_event_stale"
REASON_WARNING = "market_event_warning"
REASON_CAUTION = "market_event_caution"
REASON_NONE = "market_event_none"

#: States that must not back a new buy or add-buy. ``caution`` stays visible
#: with its types and is not excluded by this rule.
BUY_BLOCKING_STATES = frozenset({STATE_WARNING, STATE_UNKNOWN, STATE_STALE})


@dataclass(frozen=True)
class MarketEventState:
    """One closed market-alert state for a single market."""

    market: str
    state: str
    warning: bool | None
    caution_types: tuple[str, ...]
    observed_at: datetime | None
    reason: str
    #: The normalized ``{"warning": bool, "caution": {...}}`` value as stored
    #: (``None`` when unreadable). Kept for readers that echo the raw flags.
    market_event: dict[str, Any] | None = None

    @property
    def blocks_new_buys(self) -> bool:
        return self.state in BUY_BLOCKING_STATES

    @property
    def caution_active(self) -> bool:
        return self.state.startswith(CAUTION_STATE_PREFIX)


def caution_state(types: tuple[str, ...]) -> str:
    return CAUTION_STATE_PREFIX + ",".join(types)


def parse_market_event(payload: object) -> dict[str, Any] | None:
    """Normalize a raw ``market_event`` object, or ``None`` when unreadable.

    Strict by design: ``warning`` must be a JSON boolean and ``caution`` must be
    an object of booleans. A missing object, a missing ``caution`` object or any
    non-boolean value makes the whole statement unknown instead of guessing.
    Caution type names are kept exactly as Upbit sends them (Upbit may add
    types), and only booleans survive.
    """
    if not isinstance(payload, dict):
        return None
    warning = payload.get("warning")
    caution = payload.get("caution")
    if not isinstance(warning, bool) or not isinstance(caution, dict):
        return None
    normalized: dict[str, bool] = {}
    for key, value in caution.items():
        if not isinstance(key, str) or not isinstance(value, bool):
            return None
        normalized[key] = value
    return {"warning": warning, "caution": normalized}


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def classify_market_event(
    *,
    market: str,
    market_event: object,
    observed_at: object,
    now: datetime,
    stale_after: timedelta = MARKET_EVENT_STALE_AFTER,
) -> MarketEventState:
    """Fold one stored observation into the closed state vocabulary."""
    normalized_market = str(market or "").strip().upper()
    observed = _as_utc(observed_at)
    parsed = parse_market_event(market_event)
    if parsed is None:
        return MarketEventState(
            market=normalized_market,
            state=STATE_UNKNOWN,
            warning=None,
            caution_types=(),
            observed_at=observed,
            reason=REASON_ABSENT,
        )

    warning = bool(parsed["warning"])
    caution_types = tuple(sorted(k for k, v in parsed["caution"].items() if v))

    if observed is None:
        state, reason = STATE_UNKNOWN, REASON_ABSENT
    elif now - observed > stale_after:
        state, reason = STATE_STALE, REASON_STALE
    elif warning:
        state, reason = STATE_WARNING, REASON_WARNING
    elif caution_types:
        state, reason = caution_state(caution_types), REASON_CAUTION
    else:
        state, reason = STATE_NONE, REASON_NONE

    return MarketEventState(
        market=normalized_market,
        state=state,
        warning=warning,
        caution_types=caution_types,
        observed_at=observed,
        reason=reason,
        market_event=parsed,
    )


async def load_market_event_states(
    db: AsyncSession,
    *,
    quote_currency: str | None = None,
    now: datetime | None = None,
    stale_after: timedelta = MARKET_EVENT_STALE_AFTER,
) -> dict[str, MarketEventState]:
    """Closed state per active market, read from ``upbit_symbol_universe``."""
    normalized_quote = str(quote_currency or "").strip().upper()
    stmt = select(
        UpbitSymbolUniverse.market,
        UpbitSymbolUniverse.market_event,
        UpbitSymbolUniverse.market_event_observed_at,
    ).where(UpbitSymbolUniverse.is_active.is_(True))
    if normalized_quote:
        stmt = stmt.where(UpbitSymbolUniverse.quote_currency == normalized_quote)
    stmt = stmt.order_by(UpbitSymbolUniverse.market.asc())

    reference_now = now or datetime.now(UTC)
    states: dict[str, MarketEventState] = {}
    for market, market_event, observed_at in (await db.execute(stmt)).all():
        state = classify_market_event(
            market=market,
            market_event=market_event,
            observed_at=observed_at,
            now=reference_now,
            stale_after=stale_after,
        )
        if state.market:
            states[state.market] = state
    return states


__all__ = [
    "BUY_BLOCKING_STATES",
    "CAUTION_STATE_PREFIX",
    "MARKET_EVENT_STALE_AFTER",
    "MarketEventState",
    "REASON_ABSENT",
    "REASON_CAUTION",
    "REASON_NONE",
    "REASON_STALE",
    "REASON_WARNING",
    "STATE_NONE",
    "STATE_STALE",
    "STATE_UNKNOWN",
    "STATE_WARNING",
    "caution_state",
    "classify_market_event",
    "load_market_event_states",
    "parse_market_event",
]
