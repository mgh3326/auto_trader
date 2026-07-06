"""Market-events snapshot collector (read-only).

Reads market events for a small window around today via
:class:`MarketEventsQueryService`. The query service is read-only by
design (ROB-128).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.market_events.query_service import MarketEventsQueryService

_logger = logging.getLogger(__name__)

_MARKET_TO_QUERY: dict[str, str | None] = {
    # Market events upstream keys: "kr" / "us" — there is no "crypto"
    # category at the moment so we surface only KR/US events when present
    # and an empty payload otherwise (still counts as fresh — no events is
    # a valid state).
    "kr": "kr",
    "us": "us",
    "crypto": None,
}

# ROB-366 B5 — per-market index symbol set populated into the ``indices`` key so
# MarketStage can read a real index quote (it was previously a silent no-op for
# every market). KR → KOSPI/KOSDAQ (Naver); US → S&P 500 / NASDAQ / Dow
# (yfinance). Crypto → CRYPTO total market cap regime index via CoinGecko
# (ROB-377).
_MARKET_TO_INDEX_SYMBOLS: dict[str, list[str]] = {
    "kr": ["KOSPI", "KOSDAQ"],
    "us": ["SPX", "NASDAQ", "DJI"],
    "crypto": ["CRYPTO"],
}

# A read-only callable: given index symbols, return one row per resolved index
# ({symbol, name, current, change_pct, ...}). Wired in registry.py over the
# deterministic fundamentals index source so this module imports no MCP tooling.
IndexQuoteFn = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]

# ROB-381 PR3 — read-only callable returning the Upbit altseason snapshot
# (UBAI/UBMI ratio + 24h alt-vs-BTC breadth) or ``None``. Wired in registry.py
# over ``fetch_upbit_altseason`` and only consulted for the crypto market, so the
# Hermes crypto market dimension gains an altseason signal. Best-effort.
AltseasonFn = Callable[[], Awaitable[dict[str, Any] | None]]


class MarketEventsSnapshotCollector:
    """Required-kind ``market`` collector backed by ``market_events``."""

    snapshot_kind: str = "market"

    def __init__(
        self,
        session: AsyncSession,
        *,
        query_service: MarketEventsQueryService | None = None,
        index_quote_fn: IndexQuoteFn | None = None,
        altseason_fn: AltseasonFn | None = None,
        lookback_days: int = 0,
        lookahead_days: int = 1,
    ) -> None:
        self._session = session
        self._query = query_service or MarketEventsQueryService(session)
        self._index_quote_fn = index_quote_fn
        self._altseason_fn = altseason_fn
        self._lookback = max(0, lookback_days)
        self._lookahead = max(0, lookahead_days)

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        market_key = _MARKET_TO_QUERY.get(request.market)

        today = now.date()
        from_date = today - dt.timedelta(days=self._lookback)
        to_date = today + dt.timedelta(days=self._lookahead)

        try:
            response = await self._query.list_for_range(
                from_date=from_date,
                to_date=to_date,
                market=market_key,
            )
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"market_events query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        events_payload: list[dict[str, Any]] = [
            event.model_dump(mode="json") for event in response.events
        ]
        payload: dict[str, Any] = {
            "market": request.market,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "event_count": len(events_payload),
            "events": events_payload,
        }
        indices = await self._collect_indices(request.market)
        if indices:
            payload["indices"] = indices
            if request.market == "kr" and request.market_session == "nxt":
                payload["index_session"] = "regular_closed"
                payload["index_session_note"] = (
                    "KRX 정규장 미개장, 전일 종가 기준(frozen)"
                )
        altseason = await self._collect_altseason(request.market)
        if altseason:
            payload["altseason"] = altseason
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={
                    "event_count": len(events_payload),
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "index_count": len(payload.get("indices", {})),
                    "has_altseason": bool(altseason),
                },
            )
        ]

    async def _collect_indices(self, market: str) -> dict[str, dict[str, Any]]:
        """Fetch market-conditioned index quotes and adapt to the stage's shape.

        Returns a ``{symbol: {change_percent, name, current, ...}}`` dict (with
        optional freshness metadata like quote_asof, data_state, etc. passed
        through when present) — the shape MarketStage reads. Fail-open: any fetch
        error (or absent source) yields ``{}`` so the events payload is still
        emitted (the stage then reports the market dimension as unavailable rather
        than the whole snapshot failing). An index whose ``change_pct`` is ``None``
        is omitted, never coerced to a fabricated 0.0%.
        """
        if self._index_quote_fn is None:
            return {}
        symbols = _MARKET_TO_INDEX_SYMBOLS.get(market, [])
        if not symbols:
            return {}
        try:
            rows = await self._index_quote_fn(symbols)
        except Exception as exc:  # noqa: BLE001 — index data is best-effort
            _logger.info("market index fetch failed for %s: %r", market, exc)
            return {}

        indices: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            change = row.get("change_pct")
            if not symbol or change is None:
                continue
            try:
                change_percent = float(change)
            except (TypeError, ValueError):
                continue
            adapted = {
                "change_percent": change_percent,
                "name": row.get("name"),
                "current": row.get("current"),
            }
            for key in (
                "quote_asof",
                "data_state",
                "data_state_reason",
                "quote_lag_seconds",
                "as_of",
            ):
                value = row.get(key)
                if value is not None:
                    adapted[key] = value
            indices[str(symbol)] = adapted
        return indices

    async def _collect_altseason(self, market: str) -> dict[str, Any] | None:
        """Attach the Upbit altseason snapshot to the crypto market dimension.

        Crypto-only and best-effort: returns ``None`` for non-crypto markets, when
        no altseason source is wired, or on any fetch error — the rest of the
        market snapshot is emitted regardless (never fabricated).
        """
        if market != "crypto" or self._altseason_fn is None:
            return None
        try:
            altseason = await self._altseason_fn()
        except Exception as exc:  # noqa: BLE001 — altseason is best-effort
            _logger.info("altseason fetch failed for %s: %r", market, exc)
            return None
        return altseason if isinstance(altseason, dict) else None
