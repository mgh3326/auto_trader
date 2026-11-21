"""Read-only Upbit market-alert read model (hk 6026).

Reads the ``market_event`` observations stored by the universe sync and turns
each one into exactly one closed state (``none`` / ``warning`` /
``caution:<TYPE,...>`` / ``unknown`` / ``stale``) via
:mod:`app.services.upbit_market_event`.

🔴 Freshness is judged by the observation time stored next to the value, never
by the row's ``updated_at``. A block whose last observation is older than
:data:`~app.services.upbit_market_event.MARKET_EVENT_STALE_AFTER` reports
``stale`` and ``fetchedAt`` is that observation time — old data can no longer
read as fresh, and a market Upbit did not speak about is ``unknown``, never
``none``. This tier makes no HTTP call: the values come from the sync.
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.upbit_market_event import (
    CAUTION_STATE_PREFIX,
    MARKET_EVENT_STALE_AFTER,
    STATE_NONE,
    STATE_WARNING,
    MarketEventState,
    load_market_event_states,
)
from app.services.upbit_public_read_model.types import (
    WARNINGS_TTL_SECONDS,
    UpbitBlockMeta,
    UpbitMarketWarningEntry,
    UpbitMarketWarningsBlock,
    _now_utc,
)


class MarketWarningsProvider(Protocol):
    def __call__(
        self, markets: list[str] | None = None
    ) -> Awaitable[UpbitMarketWarningsBlock]: ...


def _legacy_warning(state: str) -> str:
    """Legacy 유의 projection of the closed state."""
    if state == STATE_WARNING:
        return "CAUTION"
    if state == STATE_NONE or state.startswith(CAUTION_STATE_PREFIX):
        return "NONE"
    return "UNKNOWN"


def _entry_from_state(
    state: MarketEventState, *, include_event_detail: bool
) -> UpbitMarketWarningEntry:
    return UpbitMarketWarningEntry(
        market=state.market,
        warning=_legacy_warning(state.state),
        state=state.state,
        caution_types=list(state.caution_types),
        observed_at=state.observed_at,
        event=state.market_event if include_event_detail else None,
    )


class MarketWarningsService:
    def __init__(
        self,
        *,
        db_session_factory=AsyncSessionLocal,
        now_provider=_now_utc,
    ) -> None:
        self._db_session_factory = db_session_factory
        self._now_provider = now_provider

    async def get(
        self,
        markets: list[str] | None = None,
        *,
        include_event_detail: bool = False,
        db: AsyncSession | None = None,
    ) -> UpbitMarketWarningsBlock:
        requested = {m.upper() for m in markets} if markets else None
        now = self._now_provider()
        states = await self._load_states(db=db, now=now)
        selected = {
            market: state
            for market, state in states.items()
            if requested is None or market in requested
        }
        entries = {
            market: _entry_from_state(state, include_event_detail=include_event_detail)
            for market, state in selected.items()
        }
        observed = [
            state.observed_at for state in selected.values() if state.observed_at
        ]
        latest: datetime | None = max(observed, default=None)
        if not entries:
            state_name = "missing"
        elif latest is None or now - latest > MARKET_EVENT_STALE_AFTER:
            state_name = "stale"
        else:
            state_name = "fresh"

        return UpbitMarketWarningsBlock(
            meta=UpbitBlockMeta(
                source="upbit_market_warnings",
                state=state_name,
                label="Upbit market alerts (market_event)",
                fetchedAt=latest,
                ttlSeconds=WARNINGS_TTL_SECONDS,
            ),
            entries=entries,
        )

    async def _load_states(
        self, *, db: AsyncSession | None, now: datetime
    ) -> dict[str, MarketEventState]:
        if db is not None:
            return await load_market_event_states(db, quote_currency="KRW", now=now)
        async with self._db_session_factory() as session:
            return await load_market_event_states(
                session, quote_currency="KRW", now=now
            )


_default_service = MarketWarningsService()
get_market_warnings = _default_service.get
db_universe_warnings_provider = _default_service.get

__all__ = [
    "MarketWarningsProvider",
    "MarketWarningsService",
    "db_universe_warnings_provider",
    "get_market_warnings",
]
